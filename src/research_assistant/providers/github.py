"""GitHub provider（自写 HTTP，ADR-0010）。

两条能力（api-contract.md §2.7）：
    github file <url>   取仓库内单个文件的原文（blob / raw 形态 URL）
    github repo <url>   取仓库元数据 + README 原文（**恰好 2 次 API 调用**）

配置（DEC-028：两项**都可缺省**，零配置即可用）：
    [[provider]]
    type = "github"
    base_url = "https://api.github.com"   # 可省；GitHub Enterprise 填到 .../api/v3（原样使用，不自动补）
    api_key  = "ghp_..."                  # 可省；省略 = 匿名（60 次/小时），配置后 5000 次/小时

取文件的两条通道（公开仓库走 raw 是**零配额且无需凭据**，实测 3 次调用配额不减少）：
    1) raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}   ← 首选
    2) api.github.com/repos/{o}/{r}/contents/{path}?ref=       ← 私有仓库 / GHE / 无 raw 时
私有仓库只有通道 2 可用（raw 不支持私有仓库鉴权，会 404）。

关键实测事实（2026-09-13，官方 api-versions 页 + 实查）：
    - 不带 X-GitHub-Api-Version 头会默认到 2022-11-28；此处显式钉在当前版本。
    - 匿名 core 限额 60/h；401 = 凭据无效（"Bad credentials"）；403 且 X-RateLimit-Remaining: 0 = 配额耗尽。
    - open_issues_count **把 issues 与 PR 合并计数**；watchers_count 是 stars 的历史别名，
      真正的 watch 数是 subscribers_count。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .. import http as http_mod
from ..config import Config, ProviderConfig
from ..errors import ArgsError, ProviderError
from ..github_urls import GithubRef, parse_github_url, slice_lines
from ..proxy import resolve_proxy
from .base import ArgSpec, Capability, Provider
from .registry import register

DEFAULT_API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"

# 官方 api-versions 页核实（2026-09-13）：当前版本 2026-03-10；不带此头则默认 2022-11-28。
_API_VERSION = "2026-03-10"
# 取原文的媒体类型（contents / readme 均支持，实测返回纯文本而非 JSON 包裹）。
_RAW_ACCEPT = "application/vnd.github.raw+json"
_JSON_ACCEPT = "application/vnd.github+json"

logger = logging.getLogger("research_assistant.providers.github")

# 常见扩展名 → markdown 代码块语言（认不出则不加语言标记）
_LANG_BY_EXT: dict[str, str] = {
    "py": "python", "pyi": "python", "js": "javascript", "mjs": "javascript",
    "cjs": "javascript", "ts": "typescript", "tsx": "tsx", "jsx": "jsx",
    "rs": "rust", "go": "go", "java": "java", "kt": "kotlin", "rb": "ruby",
    "php": "php", "c": "c", "h": "c", "cpp": "cpp", "cc": "cpp", "hpp": "cpp",
    "cs": "csharp", "swift": "swift", "scala": "scala", "sh": "bash",
    "bash": "bash", "ps1": "powershell", "sql": "sql", "json": "json",
    "yaml": "yaml", "yml": "yaml", "toml": "toml", "ini": "ini", "cfg": "ini",
    "md": "markdown", "rst": "rst", "html": "html", "htm": "html",
    "css": "css", "scss": "scss", "xml": "xml", "lua": "lua", "r": "r",
    "dart": "dart", "ex": "elixir", "exs": "elixir", "erl": "erlang",
    "hs": "haskell", "clj": "clojure", "proto": "protobuf", "tf": "hcl",
    "dockerfile": "dockerfile", "makefile": "makefile", "gradle": "groovy",
}


def _resolve_base(base_url: str) -> str:
    """归一化 API base：空 = 官方 api.github.com；给了就**原样**用（不猜前缀）。

    GitHub Enterprise 的 REST base 是 `https://HOST/api/v3`，要求调用方显式写全 ——
    不自动补 `/api/v3`：网关/镜像根与 GHE 裸域名在 URL 上无法区分，自动补只会把网关路径拼错。
    """
    base = (base_url or "").strip().rstrip("/")
    return base or DEFAULT_API_BASE


def _raw_base_for(base: str) -> str:
    """只有官方 API 才推导 raw 通道；GHE / 网关一律返回空串（走 contents API）。"""
    return RAW_BASE if urlparse(base).netloc.lower() == "api.github.com" else ""


def _lang_of(path: str) -> str:
    name = (path.rsplit("/", 1)[-1] or "").lower()
    if name in ("dockerfile", "makefile"):
        return _LANG_BY_EXT.get(name, "")
    return _LANG_BY_EXT.get(name.rsplit(".", 1)[-1] if "." in name else "", "")


def _fence(text: str) -> str:
    """按正文内容选足够长的围栏，避免正文里的 ``` 提前闭合代码块。"""
    fence = "```"
    while fence in text:
        fence += "`"
    return fence


def _quote_path(path: str) -> str:
    """逐段 quote（保留 / 分隔符），处理文件名里的空格 / 非 ASCII。"""
    return "/".join(quote(seg, safe="") for seg in path.split("/") if seg)


def _rate_limit_reset(resp: httpx.Response) -> str:
    """把 X-RateLimit-Reset（epoch 秒）转成本地可读时间；缺失返回空串。"""
    raw = resp.headers.get("X-RateLimit-Reset")
    if not raw or not raw.isdigit():
        return ""
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, OSError, OverflowError):  # pragma: no cover - 异常头值
        return ""


def _error_for(resp: httpx.Response) -> ProviderError:
    """GitHub 错误分流（401 凭据 / 403 配额 / 404 不可见 / 其余通用）。

    与 http.handle_http_error 的区别：**403 要区分「配额耗尽」与「无权限」** ——
    前者带 X-RateLimit-Remaining: 0 与 reset 时间，笼统报「请检查 API key」会误导排查方向。
    """
    status = resp.status_code
    detail: dict[str, Any] = {"status_code": status}
    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining is not None:
        detail["rate_limit_remaining"] = remaining
    if status == 401:
        return ProviderError(
            "github: 认证失败（401），api_key 无效或已过期",
            provider="github", details=detail,
        )
    if status == 403 and remaining == "0":
        reset = _rate_limit_reset(resp)
        detail["rate_limit_reset"] = reset
        hint = f"，约 {reset} 恢复" if reset else ""
        return ProviderError(
            f"github: API 配额耗尽（403）{hint}；匿名限额 60 次/小时，配置 api_key 可提升到 5000 次/小时",
            provider="github", details=detail,
        )
    if status == 404:
        return ProviderError(
            "github: 资源不存在，或为私有仓库（私有仓库与 GHE 需配置 api_key）",
            provider="github", details=detail,
        )
    body = ""
    try:
        body = resp.text[:500]
    except Exception:  # pragma: no cover - 读取失败不影响主流程
        pass
    if body:
        detail["body"] = body
    return ProviderError(f"github: 非 2xx 响应（{status}）", provider="github", details=detail)


@register
class GithubProvider(Provider):
    type = "github"
    command = "github"
    aliases = ["gh"]
    help = "GitHub file raw content and repository metadata + README (api_key optional)."

    def capabilities(self) -> list[Capability]:
        return [
            Capability(
                name="file",
                help="Fetch a single file's raw content from a GitHub repo URL.",
                args=[
                    ArgSpec(["url"], kind="positional", metavar="URL",
                            help="github.com/<owner>/<repo>/blob/<ref>/<path> or raw.githubusercontent.com/..."),
                ],
                handler=self.file,
            ),
            Capability(
                name="repo",
                help="Fetch repository metadata + README (2 API calls).",
                args=[
                    ArgSpec(["url"], kind="positional", metavar="URL",
                            help="github.com/<owner>/<repo>"),
                ],
                handler=self.repo,
            ),
        ]

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def _cfg(self) -> ProviderConfig:
        """解析 github 配置。**缺 config、缺 base_url、缺 api_key 都不报错**（DEC-028）。"""
        cfg = self.config.provider("github") if self.config is not None else None
        if cfg is None:
            cfg = ProviderConfig(type="github")
        cfg.base_url = _resolve_base(cfg.base_url)
        if not cfg.api_key:
            logger.debug("github: 未配置 api_key，按匿名模式调用（60 次/小时）")
        return cfg

    # ------------------------------------------------------------------
    # HTTP 基础设施（自写；复用 make_client 的代理/UA/超时横切）
    # ------------------------------------------------------------------

    def _client(self, cfg: ProviderConfig) -> httpx.AsyncClient:
        proxy = resolve_proxy(self.config.proxy.url if self.config is not None else "")
        return http_mod.make_client(proxy_url=proxy, timeout=cfg.timeout)

    @staticmethod
    def _headers(cfg: ProviderConfig, accept: str, *, auth: bool) -> dict[str, str]:
        """auth=False 时**不带** Authorization —— 避免把 token 发给 raw.githubusercontent.com。"""
        headers = {"X-GitHub-Api-Version": _API_VERSION, "Accept": accept}
        if auth and cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        return headers

    async def _request(
        self, client: httpx.AsyncClient, cfg: ProviderConfig, method: str, url: str, *,
        accept: str, params: dict[str, Any] | None = None, auth: bool = True,
    ) -> httpx.Response:
        try:
            resp = await client.request(method, url, headers=self._headers(cfg, accept, auth=auth), params=params)
        except httpx.HTTPError as e:
            from ..errors import wrap_provider_http_error

            raise wrap_provider_http_error("github", e) from e
        if resp.status_code >= 400:
            raise _error_for(resp)
        return resp

    async def _json(self, client: httpx.AsyncClient, cfg: ProviderConfig, url: str, **kw: Any) -> Any:
        resp = await self._request(client, cfg, "GET", url, accept=_JSON_ACCEPT, **kw)
        try:
            return resp.json()
        except ValueError as e:
            from ..errors import NetworkError

            raise NetworkError(f"github: 响应非 JSON ({e})", provider="github") from e

    # ------------------------------------------------------------------
    # 取正文：raw 优先（零配额）→ contents API（私有 / GHE）
    # ------------------------------------------------------------------

    async def _fetch_file(
        self, client: httpx.AsyncClient, cfg: ProviderConfig, ref: GithubRef,
    ) -> tuple[str, str, str, str] | None:
        """返回 (text, source, ref, path)；所有候选都取不到返回 None。

        source ∈ {"raw", "api"}。ref 候选先短后长，覆盖分支名含斜杠的情况。
        """
        raw_base = _raw_base_for(cfg.base_url)
        base = cfg.base_url
        for ref_name, path in ref.candidates():
            if raw_base:
                text = await self._try_raw(client, cfg, raw_base, ref, ref_name, path)
                if text is not None:
                    return text, "raw", ref_name, path
            text = await self._try_contents(client, cfg, base, ref, ref_name, path)
            if text is not None:
                return text, "api", ref_name, path
        return None

    async def _try_raw(
        self, client: httpx.AsyncClient, cfg: ProviderConfig, raw_base: str,
        ref: GithubRef, ref_name: str, path: str,
    ) -> str | None:
        """raw 通道：公开仓库零配额、无需凭据；404（私有/不存在/ref 猜错）→ None。"""
        url = f"{raw_base}/{quote(ref.owner, safe='')}/{quote(ref.repo, safe='')}/{_quote_path(ref_name)}/{_quote_path(path)}"
        try:
            resp = await self._request(client, cfg, "GET", url, accept="text/plain", auth=False)
        except ProviderError as e:
            if (e.details or {}).get("status_code") == 404:
                return None
            raise
        return resp.text

    async def _try_contents(
        self, client: httpx.AsyncClient, cfg: ProviderConfig, base: str,
        ref: GithubRef, ref_name: str, path: str,
    ) -> str | None:
        """contents 通道（私有仓库 / GHE / 无 raw）；404 → None。"""
        url = f"{base}/repos/{quote(ref.owner, safe='')}/{quote(ref.repo, safe='')}/contents/{_quote_path(path)}"
        try:
            resp = await self._request(client, cfg, "GET", url, accept=_RAW_ACCEPT, params={"ref": ref_name})
        except ProviderError as e:
            if (e.details or {}).get("status_code") == 404:
                return None
            raise
        return resp.text

    async def _fetch_repo(
        self, client: httpx.AsyncClient, cfg: ProviderConfig, owner: str, repo: str,
    ) -> tuple[dict[str, Any], str | None]:
        """恰好 2 次调用：/repos（必需）+ /readme（best-effort，无 README 不影响）。"""
        o, r = quote(owner, safe=""), quote(repo, safe="")
        meta = await self._json(client, cfg, f"{cfg.base_url}/repos/{o}/{r}")
        readme: str | None = None
        try:
            resp = await self._request(
                client, cfg, "GET", f"{cfg.base_url}/repos/{o}/{r}/readme", accept=_RAW_ACCEPT
            )
            readme = resp.text
        except ProviderError as e:
            if (e.details or {}).get("status_code") != 404:  # 404 = 该仓库没有 README，合法
                raise
        return meta, readme

    # ------------------------------------------------------------------
    # 渲染（fetch 自动接管路径需要字符串）
    # ------------------------------------------------------------------

    @staticmethod
    def _render_file(text: str, path: str, fmt: str) -> str:
        if fmt in ("text", "html"):
            return text  # html 对源码文件无对应物，与 text 同义（文档已说明）
        lang = _lang_of(path)
        fence = _fence(text)
        body = text if text.endswith("\n") else text + "\n"
        return f"{fence}{lang}\n{body}{fence}\n"

    @staticmethod
    def _render_repo(meta: dict[str, Any], readme: str | None, fmt: str) -> str:
        """紧凑 markdown（省 token）：标题 + 关键元数据 + README 全文。html 降级 markdown。"""
        full = meta.get("full_name") or ""
        lines: list[str] = [f"# {full}", ""]
        if meta.get("description"):
            lines.append(str(meta["description"]))
            lines.append("")
        bits: list[str] = []
        if meta.get("stargazers_count") is not None:
            bits.append(f"stars {meta['stargazers_count']}")
        if meta.get("forks_count") is not None:
            bits.append(f"forks {meta['forks_count']}")
        if meta.get("subscribers_count") is not None:
            bits.append(f"watchers {meta['subscribers_count']}")
        if meta.get("open_issues_count") is not None:
            bits.append(f"open_issues+PR {meta['open_issues_count']}")
        if bits:
            lines.append("- " + " · ".join(bits))
        if meta.get("language"):
            lines.append(f"- language: {meta['language']}")
        lic = meta.get("license") or {}
        if isinstance(lic, dict) and lic.get("spdx_id"):
            lines.append(f"- license: {lic['spdx_id']}")
        if meta.get("default_branch"):
            lines.append(f"- default_branch: {meta['default_branch']}")
        if meta.get("pushed_at"):
            lines.append(f"- pushed_at: {meta['pushed_at']}")
        topics = meta.get("topics") or []
        if topics:
            lines.append("- topics: " + ", ".join(str(t) for t in topics))
        if meta.get("archived"):
            lines.append("- archived: true")
        if meta.get("homepage"):
            lines.append(f"- homepage: {meta['homepage']}")
        if readme:
            lines += ["", "## README", "", readme]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # 能力 handler（显式命令）
    # ------------------------------------------------------------------

    async def file(self, args: Any) -> dict[str, Any]:
        ref = parse_github_url(args.url)
        if ref is None or ref.kind != "file":
            raise ArgsError(
                "github file: 需要 GitHub 文件 URL"
                "（.../<owner>/<repo>/blob/<ref>/<path>、/raw/<ref>/<path> 或 raw.githubusercontent.com/...）",
                provider="github",
            )
        cfg = self._cfg()
        async with self._client(cfg) as client:
            got = await self._fetch_file(client, cfg, ref)
        if got is None:
            raise ProviderError(
                "github file: 取不到文件内容（路径/ref 不存在，或私有仓库缺少 api_key）",
                provider="github",
                details={"owner": ref.owner, "repo": ref.repo, "ref_hint": ref.ref_hint,
                         "path_hint": ref.path_hint, "candidates_tried": len(ref.candidates())},
            )
        text, source, ref_name, path = got
        text = slice_lines(text, ref.line_start, ref.line_end)
        out: dict[str, Any] = {
            "kind": "file",
            "owner": ref.owner,
            "repo": ref.repo,
            "ref": ref_name,
            "path": path,
            "source": source,
            "bytes": len(text.encode("utf-8")),
            "contents": [
                {
                    "text": text,
                    "source_url": f"https://github.com/{ref.owner}/{ref.repo}/blob/{ref_name}/{path}",
                }
            ],
        }
        if ref.line_start is not None:
            out["line_start"] = ref.line_start
            out["line_end"] = ref.line_end
        return out

    async def repo(self, args: Any) -> dict[str, Any]:
        ref = parse_github_url(args.url)
        if ref is None or ref.kind != "repo":
            raise ArgsError(
                "github repo: 需要仓库 URL（https://github.com/<owner>/<repo>）", provider="github"
            )
        cfg = self._cfg()
        async with self._client(cfg) as client:
            meta, readme = await self._fetch_repo(client, cfg, ref.owner, ref.repo)
        if not isinstance(meta, dict):  # pragma: no cover - API 契约保证为对象
            raise ProviderError("github repo: /repos 响应非对象", provider="github")
        lic = meta.get("license") or {}
        out: dict[str, Any] = {
            "kind": "repo",
            "full_name": meta.get("full_name"),
            "description": meta.get("description"),
            "visibility": meta.get("visibility"),
            "default_branch": meta.get("default_branch"),
            # 原生字段名（DEC-017）：open_issues_count 含 PR（实测：页面把 issues 与 PR 分开显示）；
            # watchers_count 是 stars 的历史别名，真正的 watch 数用 subscribers_count。
            "stargazers_count": meta.get("stargazers_count"),
            "forks_count": meta.get("forks_count"),
            "subscribers_count": meta.get("subscribers_count"),
            "open_issues_count": meta.get("open_issues_count"),
            "language": meta.get("language"),
            "license": (lic.get("spdx_id") if isinstance(lic, dict) else None),
            "topics": meta.get("topics") or [],
            "size_kb": meta.get("size"),
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "pushed_at": meta.get("pushed_at"),
            "archived": meta.get("archived"),
            "fork": meta.get("fork"),
            "homepage": meta.get("homepage"),
            "html_url": meta.get("html_url"),
        }
        if readme:
            out["contents"] = [
                {"text": readme, "source_url": f"https://github.com/{ref.owner}/{ref.repo}#readme"}
            ]
        return out


# ---------------------------------------------------------------------------
# fetch 自动接管入口（DEC-028）：best-effort，失败返回 None 由调用方落回原链路
# ---------------------------------------------------------------------------


async def fetch_github_text(config: Config, url: str, fmt: str = "markdown") -> str | None:
    """把 GitHub 文件 / 仓库 URL 转成正文；不是可识别 URL 或取不到则返回 None。

    与 tavily/firecrawl 分支同性质（best-effort）：**任何失败都返回 None**，
    绝不抛异常打断 fetch 的既有回退链（DEC-016）。真实原因写 stderr 日志（DEC-018）。
    """
    ref = parse_github_url(url)
    if ref is None:
        return None
    provider = GithubProvider(config)
    cfg = provider._cfg()
    try:
        async with provider._client(cfg) as client:
            if ref.kind == "repo":
                meta, readme = await provider._fetch_repo(client, cfg, ref.owner, ref.repo)
                if not isinstance(meta, dict):  # pragma: no cover
                    return None
                return provider._render_repo(meta, readme, fmt)
            got = await provider._fetch_file(client, cfg, ref)
            if got is None:
                logger.warning("github 接管未取到内容（%s），落回原链路", url)
                return None
            text, _source, _ref_name, path = got
            return provider._render_file(slice_lines(text, ref.line_start, ref.line_end), path, fmt)
    except Exception as e:
        logger.warning("github 接管失败（%s），落回原链路: %s", url, e)
        return None
