"""GitHub URL 识别（纯解析，无 I/O、无网络）。

只认明确形状，**认不出就返回 None**，让调用方落回既有链路（不猜、不贪）：

    github.com/{owner}/{repo}                              → repo（仅此两段形状才算仓库页）
    github.com/{owner}/{repo}/blob/{ref...}/{path}         → file
    github.com/{owner}/{repo}/raw/{ref...}/{path}          → file
    raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}  → file

显式不认（一律落回原链路，避免误命中）：
    单段路径（用户主页 /stars /topics /marketplace /orgs/...）、/tree/（目录页）、
    /issues /pulls /actions /releases /blame /archive /commits /settings ...
    gist.github.com（另一套 API）、非 http(s) scheme。

片段（#L10 / #L10-L20）在此解析为行号范围 —— 内容已在本地，切行是零成本。

**ref 歧义**：`blob/{ref}/{path}` 里的 ref 自身可能含斜杠（如 `feature/foo`），
从 URL 文本上无法无歧义切分。此处不拍板，而是保留全部路径段，由
`GithubRef.candidates()` 给出「先短后长」的候选（ref 取 1 段、2 段、…），
调用方逐个试，绝大多数情况第一段即命中。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

# github.com 的顶层路由：这些首段不是 owner（避免把 /topics 之类当成 owner/repo）
_RESERVED_TOP: frozenset[str] = frozenset({
    "about", "account", "apps", "collections", "codespaces", "contact", "dashboard",
    "enterprise", "explore", "features", "issues", "login", "logout", "marketplace",
    "new", "notifications", "organizations", "orgs", "pricing", "pulls", "search",
    "security", "settings", "signup", "site", "sponsors", "stars", "topics",
    "trending", "users", "watching",
})

# 只有这两个中间段表示「文件」；tree/（目录）等一概不认
_FILE_KINDS: frozenset[str] = frozenset({"blob", "raw"})

_GITHUB_HOSTS: frozenset[str] = frozenset({"github.com", "www.github.com"})
_RAW_HOST = "raw.githubusercontent.com"

# #L10 | #L10-L20 | #L10-20
_LINE_RE = re.compile(r"^L(\d+)(?:-L?(\d+))?$")


@dataclass(frozen=True)
class GithubRef:
    """识别结果。kind="repo" 时 rest 为空；kind="file" 时 rest=(ref, *path_segments)。"""

    kind: str  # "repo" | "file"
    owner: str
    repo: str
    rest: tuple[str, ...] = ()
    line_start: int | None = None
    line_end: int | None = None
    host: str = "github.com"
    raw: bool = False  # True = 原始 URL 即 raw 内容地址（无需再推导）

    @property
    def ref_hint(self) -> str:
        """展示用 ref 猜法（第 1 段；真值以 candidates() 试出来的为准）。"""
        return self.rest[0] if self.rest else ""

    @property
    def path_hint(self) -> str:
        return "/".join(self.rest[1:]) if len(self.rest) > 1 else ""

    def candidates(self) -> list[tuple[str, str]]:
        """返回 [(ref, path)] 候选，**先短后长**（ref 取 1 段优先，再 2 段、3 段…）。

        覆盖 ref 含斜杠的场景（blob/feature/foo/a.ts：第 1 段 "feature" 会失败，
        第 2 段 "feature/foo" 命中）。最坏情况多试几次，由调用方逐候选短路。
        """
        if self.kind != "file" or len(self.rest) < 2:
            return []
        return [
            ("/".join(self.rest[:k]), "/".join(self.rest[k:]))
            for k in range(1, len(self.rest))
        ]


def _parse_lines(fragment: str) -> tuple[int | None, int | None]:
    """解析 #L10 / #L10-L20 片段 → (start, end)。认不出返回 (None, None)。"""
    frag = (fragment or "").strip()
    if not frag:
        return None, None
    m = _LINE_RE.match(frag)
    if not m:
        return None, None
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    if b < a:
        a, b = b, a
    return a, b


def _strip_dot_git(name: str) -> str:
    return name[: -len(".git")] if name.endswith(".git") else name


def parse_github_url(url: str) -> GithubRef | None:
    """把 URL 解析为 GithubRef；不是可识别的 GitHub 文件/仓库页则返回 None。"""
    if not url or not isinstance(url, str):
        return None
    try:
        p = urlparse(url.strip())
    except ValueError:
        return None
    if p.scheme and p.scheme not in ("http", "https"):
        return None
    host = (p.hostname or "").lower()
    segs = [unquote(s) for s in (p.path or "").split("/") if s]
    line_start, line_end = _parse_lines(p.fragment)

    # raw.githubusercontent.com/{owner}/{repo}/{ref}/{path...}
    if host == _RAW_HOST:
        if len(segs) < 4:
            return None
        return GithubRef(
            kind="file", owner=segs[0], repo=segs[1], rest=tuple(segs[2:]),
            line_start=line_start, line_end=line_end, host=host, raw=True,
        )

    if host not in _GITHUB_HOSTS:
        return None
    if len(segs) < 2:
        return None  # 用户主页等单段路径
    if segs[0].lower() in _RESERVED_TOP:
        return None

    owner, repo = segs[0], _strip_dot_git(segs[1])
    if not owner or not repo:
        return None
    if len(segs) == 2:
        return GithubRef(kind="repo", owner=owner, repo=repo, host=host)
    if segs[2].lower() not in _FILE_KINDS:
        return None  # tree//issues/pulls/... 一律不认
    rest = tuple(segs[3:])
    if len(rest) < 2:
        return None  # blob/{ref} 没有文件路径，不是文件页
    return GithubRef(
        kind="file", owner=owner, repo=repo, rest=rest,
        line_start=line_start, line_end=line_end, host=host,
    )


def is_github_url(url: str) -> bool:
    """是否为可识别的 GitHub 文件 / 仓库 URL（fetch 自动接管的判据）。"""
    return parse_github_url(url) is not None


def slice_lines(text: str, start: int | None, end: int | None) -> str:
    """按 #L10-L20 片段切行（1-based 闭区间）。无行号则原样返回。

    越界不报错：按实际范围裁剪（片段行号可能指向更长的历史版本）。
    """
    if start is None:
        return text
    lines = text.splitlines()
    lo = max(1, start)
    hi = end if end is not None else start
    if lo > len(lines):
        return ""
    return "\n".join(lines[lo - 1 : hi])
