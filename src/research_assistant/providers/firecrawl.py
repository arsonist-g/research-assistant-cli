"""Firecrawl provider（自写 HTTP，ADR-0010）。

API（find-docs 查证 /firecrawl/firecrawl-docs，v2）：
    POST https://api.firecrawl.dev/v2/scrape   鉴权 Authorization: Bearer <key>
        body: {url, formats:["markdown"|"html"], onlyMainContent, waitFor(ms)}
        resp: {success, data:{markdown, html?, metadata{title, sourceURL,...}, warning?}}
    POST /v2/search
        body: {query, limit, scrapeOptions{formats:[{type:"markdown"}]}, sources}
        resp: {success, data:[{markdown,url,title}]} (scraped) 或 {data:{web:[{url,title,description}]}}
    POST /v2/map
        body: {url, limit, includeSubdomains, search}
        resp: {success, links:[{url,title,description?}]}
    POST /v2/crawl   异步：提交返回 {id}，轮询 GET /v2/crawl/{id} 到 completed/failed/cancelled
        body: {url, limit, maxDiscoveryDepth?, includePaths[]?, excludePaths[]?,
              allowSubdomains?, allowExternalLinks?, sitemap?, prompt?, scrapeOptions{formats}}
        resp(POST): {success, id, url}; GET: {status, total, completed, data[{url,markdown}]}
    POST /v2/extract 异步：提交返回 {id}，轮询 GET /v2/extract/{id} 到 completed/failed/cancelled
        body: {urls[] (含 /* 通配), prompt?, schema?, enableWebSearch?, agent{model:"FIRE-1"}?}
        resp(POST): {success, id}; GET: {status, data, expiresAt?}
    POST /v2/scrape/{scrapeId}/interact   有状态浏览器会话：在已有 scrape 会话里执行 prompt 或 code
        body: {prompt} 或 {code, language("node"|"python"|"bash"), timeout(sec,1-300,默认30)}
        resp: {success, output?, stdout?, result?, stderr?, exitCode?, killed?, cdpUrl?, liveViewUrl?}
        scrapeId 来自 POST /v2/scrape 的 data.metadata.scrapeId；DELETE 同路径停止会话
    POST /v2/agent   异步：提交返回 {id}，轮询 GET /v2/agent/{id} 到 completed/failed（无 cancelled）
        body: {prompt(必填,≤10000), urls[]?, schema?, maxCredits?(默认2500), model("spark-1-mini"|"spark-1-pro"), strictConstrainToURLs?}
        resp(POST): {success, id}; GET: {status(processing|completed|failed), data?, model?, error?, creditsUsed?, expiresAt?}
    GET /v2/crawl/active   列出当前团队所有进行中的 crawl 任务（同步，无 body）
        resp: {success, crawls:[{id, teamId, url, status, options}]}（OpenAPI required 误写 data，实际字段是 crawls）
    POST /v2/parse   multipart/form-data 上传本地文件，转 markdown/json/html 等（同步）
        body: file(binary,必填) + options(json: formats,onlyMainContent,includeTags,excludeTags,timeout(ms),parsers[{type:"pdf",mode,maxPages}])
        resp: {success, data:{markdown, html?, ...}}（ScrapeResponse 结构，markdown 映射到输出 content）

对齐 api-contract.md §2.1/§3 的 firecrawl 命令与响应（links → data[]）。
开发期无 key，实现按已查证 API；待申请 key 后实测。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..config import ProviderConfig
from .. import http as http_mod
from ..proxy import resolve_proxy
from .base import ArgSpec, Capability, Provider
from .registry import register

DEFAULT_BASE_URL = "https://api.firecrawl.dev/v2"


def _keyless_flags() -> list[ArgSpec]:
    """keyless 命令（scrape/search/interact/parse）共享的 key 策略 flags。

    默认 auto：配了 key 先走免 key，失败（限流/IP/suspicious）再带 key 重试一次。
    --use-key 强制带 key（跳过免 key）；--keyless 强制免 key（失败不 fallback）。二者互斥。
    """
    return [
        ArgSpec(["--use-key"], action="store_true",
                help="Force the keyed tier; skip the keyless attempt (needs api_key)."),
        ArgSpec(["--keyless"], action="store_true",
                help="Force the keyless tier; do not fall back to key on failure."),
    ]


@register
class FirecrawlProvider(Provider):
    type = "firecrawl"
    command = "firecrawl"
    aliases = ["fc"]
    help = "Firecrawl scrape/search/interact/parse (keyless) + map/crawl/extract/agent/monitor (need key)."

    def capabilities(self) -> list[Capability]:
        return [
            Capability(
                name="scrape",
                help="Scrape one or more URLs to markdown/html (keyless).",
                args=[
                    ArgSpec(["urls"], kind="positional", nargs="+", metavar="URL", help="URL(s) to scrape."),
                    ArgSpec(["--format"], choices=["markdown", "html"], default="markdown", help="Output format."),
                    ArgSpec(["--only-main-content"], action="store_true", help="Drop nav/footer boilerplate."),
                    ArgSpec(["--wait-for"], type=int, metavar="MS", help="Wait N ms before scraping."),
                ] + _keyless_flags(),
                handler=self.scrape,
            ),
            Capability(
                name="search",
                help="Web search (keyless); with --scrape returns full markdown per result.",
                args=[
                    ArgSpec(["query"], kind="positional", help="Search query."),
                    ArgSpec(["--limit"], type=int, default=10, help="Max results (1-100)."),
                    ArgSpec(["--sources"], nargs="+", choices=["web", "news"], help="Result sources."),
                    ArgSpec(["--scrape"], action="store_true", help="Scrape full content for each result."),
                ] + _keyless_flags(),
                handler=self.search,
            ),
            Capability(
                name="map",
                help="Map a site's URLs (requires an API key, not in the keyless tier).",
                args=[
                    ArgSpec(["url"], kind="positional", help="Base URL to map."),
                    ArgSpec(["--limit"], type=int, default=100, help="Max links."),
                    ArgSpec(["--include-subdomains"], action="store_true", help="Include subdomains."),
                ],
                handler=self.map,
            ),
            Capability(
                name="crawl",
                help="Recursively crawl a site; blocks until done or --poll-timeout (needs a key).",
                args=[
                    ArgSpec(["url"], kind="positional", help="Starting URL to crawl."),
                    ArgSpec(["--limit"], type=int, default=100, help="Max pages to crawl (default 100)."),
                    ArgSpec(["--max-depth"], type=int, help="Max discovery depth from the root."),
                    ArgSpec(["--include-paths"], nargs="+", metavar="REGEX", help="Pathname regexes to include."),
                    ArgSpec(["--exclude-paths"], nargs="+", metavar="REGEX", help="Pathname regexes to exclude."),
                    ArgSpec(["--allow-subdomains"], action="store_true", help="Follow links to subdomains."),
                    ArgSpec(["--allow-external"], action="store_true", help="Follow links to external sites."),
                    ArgSpec(["--sitemap"], choices=["include", "skip", "only"], help="Sitemap handling (default include)."),
                    ArgSpec(["--prompt"], metavar="TEXT", help="Natural-language prompt to generate crawl options."),
                    ArgSpec(["--poll-timeout"], type=int, default=120, help="Seconds to wait for completion (default 120)."),
                    ArgSpec(["--async-submit"], action="store_true", help="Submit only; return the job id without waiting."),
                ],
                handler=self.crawl,
            ),
            Capability(
                name="extract",
                help="Extract structured data from URLs via LLM; blocks until done or --poll-timeout (needs a key).",
                args=[
                    ArgSpec(["urls"], kind="positional", nargs="+", metavar="URL", help="URL(s); supports /* wildcard for a whole domain."),
                    ArgSpec(["--prompt"], metavar="TEXT", help="Describe the data to extract (required if no --schema)."),
                    ArgSpec(["--schema"], metavar="JSON", help="JSON Schema string for a rigid output shape (required if no --prompt)."),
                    ArgSpec(["--enable-web-search"], action="store_true", help="Follow links beyond the given URLs."),
                    ArgSpec(["--agent"], action="store_true", help="Use the FIRE-1 agent for navigation/interaction."),
                    ArgSpec(["--poll-timeout"], type=int, default=120, help="Seconds to wait for completion (default 120)."),
                    ArgSpec(["--async-submit"], action="store_true", help="Submit only; return the job id without waiting."),
                ],
                handler=self.extract,
            ),
            Capability(
                name="interact",
                help="Run a prompt or code in a live browser session bound to a scrape (keyless).",
                args=[
                    ArgSpec(["prompt"], kind="positional", nargs="?", metavar="TEXT",
                            help="AI prompt to run in the browser session (mode 1; exclusive with --code)."),
                    ArgSpec(["--scrape-id"], metavar="ID",
                            help="Reuse an existing scrape session id (transparent handle, no local state)."),
                    ArgSpec(["--url"], metavar="URL",
                            help="Create a new scrape session from this URL when no --scrape-id is given."),
                    ArgSpec(["--code"], metavar="TEXT",
                            help="Code to execute (mode 2; exclusive with prompt)."),
                    ArgSpec(["--language"], choices=["node", "python", "bash"], default="node",
                            help="Code language (code mode only, default node)."),
                    ArgSpec(["--timeout"], type=int, default=30, metavar="SEC",
                            help="Execution timeout seconds (1-300, default 30)."),
                    ArgSpec(["--stop"], action="store_true",
                            help="Stop and tear down the session (requires --scrape-id)."),
                ] + _keyless_flags(),
                handler=self.interact,
            ),
            Capability(
                name="agent",
                help="Agentic data extraction (spark model); blocks until done or --poll-timeout (needs a key).",
                args=[
                    ArgSpec(["prompt"], kind="positional", metavar="TEXT",
                            help="What data to extract (required, max 10000 chars)."),
                    ArgSpec(["--model"], choices=["spark-1-mini", "spark-1-pro"], default="spark-1-mini",
                            help="Agent model (default spark-1-mini; spark-1-pro for complex tasks)."),
                    ArgSpec(["--urls"], nargs="+", metavar="URL",
                            help="URLs to constrain the agent."),
                    ArgSpec(["--schema"], metavar="JSON",
                            help="JSON Schema string to structure the extracted output."),
                    ArgSpec(["--max-credits"], type=int, metavar="N",
                            help="Max credits to spend (default 2500; above 2500 bills as paid)."),
                    ArgSpec(["--strict"], action="store_true",
                            help="Only visit URLs listed in --urls."),
                    ArgSpec(["--poll-timeout"], type=int, default=120,
                            help="Seconds to wait for completion (default 120)."),
                    ArgSpec(["--async-submit"], action="store_true",
                            help="Submit only; return the job id without waiting."),
                ],
                handler=self.agent,
            ),
            Capability(
                name="monitor",
                help="List active crawl jobs for the authenticated team (needs a key).",
                args=[],
                handler=self.monitor,
            ),
            Capability(
                name="parse",
                help="Upload a local document (.pdf/.docx/.html/...) and parse to markdown/json (keyless).",
                args=[
                    ArgSpec(["file"], kind="positional", metavar="PATH",
                            help="Local file path (.html/.htm/.pdf/.docx/.doc/.odt/.rtf/.xlsx/.xls)."),
                    ArgSpec(["--format"], nargs="+",
                            choices=["markdown", "html", "rawHtml", "links", "images", "summary", "json"],
                            default=["markdown"],
                            help="Output formats (default markdown)."),
                    ArgSpec(["--only-main-content"], action="store_true",
                            help="Drop nav/footer boilerplate."),
                    ArgSpec(["--include-tags"], nargs="+", metavar="TAG",
                            help="HTML tags to include."),
                    ArgSpec(["--exclude-tags"], nargs="+", metavar="TAG",
                            help="HTML tags to exclude."),
                    ArgSpec(["--timeout"], type=int, default=30000, metavar="MS",
                            help="Parse timeout ms (default 30000, max 300000)."),
                    ArgSpec(["--pdf-mode"], choices=["fast", "auto", "ocr"],
                            help="PDF parser mode (sets options.parsers[0].mode)."),
                    ArgSpec(["--max-pages"], type=int, metavar="N",
                            help="Max PDF pages to parse (1-10000, needs --pdf-mode)."),
                ] + _keyless_flags(),
                handler=self.parse,
            ),
        ]

    def _cfg(self):
        """firecrawl 免 key 层：scrape/search 无需 key 即可用。

        未配置时返回默认（默认 base_url、无 key）；有 key 才带 Authorization（更高配额 + 解锁 map）。
        """
        cfg = self.config.provider("firecrawl")
        if cfg is None:
            cfg = ProviderConfig(type="firecrawl", base_url=DEFAULT_BASE_URL, api_key="", timeout=30)
        if not cfg.base_url:
            cfg.base_url = DEFAULT_BASE_URL
        return cfg

    def _headers(self, cfg) -> dict[str, str]:
        # 免 key 层不带 Authorization；配了 key 才带（解锁更高配额与 map）
        return {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}

    @staticmethod
    def _check(data: dict[str, Any], *, endpoint: str) -> None:
        """firecrawl 对不支持的调用返回 200 + success:false（如免 key 调 map）→ 抛清晰错误。"""
        if isinstance(data, dict) and data.get("success") is False:
            from ..errors import ProviderError

            msg = data.get("error") or "firecrawl 返回 success:false"
            raise ProviderError(f"firecrawl {endpoint}: {msg}", provider="firecrawl", details=data)

    def _require_keyed(self, cfg: ProviderConfig, endpoint: str) -> None:
        """keyed 命令（map/crawl/extract/agent/monitor）必须配 key，否则前置失败，不发请求。"""
        if not cfg.api_key:
            from ..errors import ArgsError
            raise ArgsError(
                f"firecrawl {endpoint}: 此命令需要 api_key（免 key 层仅支持 scrape/search/interact/parse）",
                provider="firecrawl",
            )

    def _resolve_mode(self, args: Any, cfg: ProviderConfig) -> str:
        """解析 keyless 命令的 key 模式：keyless | use_key | auto。"""
        from ..errors import ArgsError
        if getattr(args, "keyless", False) and getattr(args, "use_key", False):
            raise ArgsError("firecrawl: --keyless 与 --use-key 互斥", provider="firecrawl")
        if getattr(args, "keyless", False):
            return "keyless"
        if getattr(args, "use_key", False):
            if not cfg.api_key:
                raise ArgsError("firecrawl: --use-key 需要 api_key，但未配置", provider="firecrawl")
            return "use_key"
        return "auto"  # 默认：有 key 先免 key 失败再带 key，无 key 只免 key

    @staticmethod
    def _is_keyless_retryable(err: Exception) -> bool:
        """免 key 失败是否值得带 key 重试（限流/IP/suspicious/quota 类；网络错误不算）。"""
        from ..errors import ProviderError
        if not isinstance(err, ProviderError):
            return False
        details = getattr(err, "details", None) or {}
        status = details.get("status_code")
        if status in (403, 429):
            return True
        msg = (getattr(err, "message", "") or "").lower()
        keywords = ("rate limit", "ratelimit", "too many", "suspicious", "quota", "limit reached", "ip ")
        return any(k in msg for k in keywords)

    async def _do_request(
        self, client: Any, method: str, path: str, *,
        json_body: Any = None, files: Any = None,
        headers: dict[str, str] | None, endpoint: str,
    ) -> dict[str, Any]:
        """单次 firecrawl 请求（JSON 或 multipart），按 headers 决定是否带 key；统一 _check。"""
        if files is not None:
            import httpx
            from ..errors import NetworkError, wrap_provider_http_error
            try:
                resp = await client.request(method, path, files=files, headers=headers)
            except httpx.HTTPError as e:
                raise wrap_provider_http_error("firecrawl", e) from e
            if resp.status_code >= 400:
                raise http_mod.handle_http_error("firecrawl", httpx.HTTPStatusError(
                    f"firecrawl HTTP {resp.status_code}", request=resp.request, response=resp
                ))
            try:
                data = resp.json()
            except ValueError as e:
                raise NetworkError(f"firecrawl: 响应非 JSON ({e})", provider="firecrawl") from e
        else:
            data = await http_mod.request_json(
                client, method, path, provider="firecrawl", json_body=json_body, headers=headers
            )
        self._check(data, endpoint=endpoint)
        return data

    async def _request_keyless(
        self, client: Any, method: str, path: str, *,
        cfg: ProviderConfig, mode: str,
        json_body: Any = None, files: Any = None, endpoint: str,
    ) -> dict[str, Any]:
        """keyless 命令请求调度：按 mode 决定是否带 key；auto 下免 key 失败（可重试）带 key 重试一次。

        - keyless：永不带 key，失败即抛。
        - use_key：必带 key（无 key 在 _resolve_mode 已挡）。
        - auto：先免 key；失败且可重试（限流/IP/suspicious）且有 key → 带 key 重试一次。
        """
        from ..errors import ProviderError
        key_headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else None

        if mode == "use_key":
            return await self._do_request(client, method, path, json_body=json_body, files=files,
                                          headers=key_headers, endpoint=endpoint)

        # keyless 或 auto：先免 key
        try:
            return await self._do_request(client, method, path, json_body=json_body, files=files,
                                          headers=None, endpoint=endpoint)
        except ProviderError as e:
            if mode == "keyless" or not cfg.api_key or not self._is_keyless_retryable(e):
                raise
            # auto + 有 key + 可重试：带 key 再来一次
            return await self._do_request(client, method, path, json_body=json_body, files=files,
                                          headers=key_headers, endpoint=endpoint)

    async def _scrape_one(self, client: Any, url: str, fmt: str, args: Any, cfg: ProviderConfig, mode: str) -> dict[str, Any]:
        body: dict[str, Any] = {"url": url, "formats": [fmt]}
        if getattr(args, "only_main_content", False):
            body["onlyMainContent"] = True
        if getattr(args, "wait_for", None):
            body["waitFor"] = args.wait_for
        data = await self._request_keyless(client, "POST", "scrape", cfg=cfg, mode=mode, json_body=body, endpoint="scrape")
        inner = data.get("data") or {}
        out: dict[str, Any] = {}
        if fmt == "markdown":
            out["markdown"] = inner.get("markdown")
        else:
            out["html"] = inner.get("html")
        meta = inner.get("metadata") or {}
        out["metadata"] = {"title": meta.get("title"), "sourceURL": meta.get("sourceURL") or url}
        if inner.get("warning"):
            out["warning"] = inner.get("warning")
        return out

    async def scrape(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        mode = self._resolve_mode(args, cfg)
        urls = args.urls
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
        ) as client:
            if len(urls) == 1:
                return {"data": await self._scrape_one(client, urls[0], args.format, args, cfg, mode)}
            # 批量并发
            items = await asyncio.gather(*[self._scrape_one(client, u, args.format, args, cfg, mode) for u in urls])
            return {"results": [{"url": u, "data": d} for u, d in zip(urls, items)]}

    async def search(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        mode = self._resolve_mode(args, cfg)
        body: dict[str, Any] = {"query": args.query, "limit": args.limit}
        if args.scrape:
            body["scrapeOptions"] = {"formats": [{"type": "markdown"}]}
        if args.sources:
            body["sources"] = args.sources
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
        ) as client:
            data = await self._request_keyless(client, "POST", "search", cfg=cfg, mode=mode, json_body=body, endpoint="search")
        out: dict[str, Any] = {}
        if args.scrape:
            arr = data.get("data") or []
            out["data"] = [
                {"markdown": item.get("markdown"), "url": item.get("url"), "title": item.get("title")}
                for item in arr
            ]
        else:
            web = (data.get("data") or {}).get("web") if isinstance(data.get("data"), dict) else data.get("data")
            out["data"] = [
                {"url": item.get("url"), "title": item.get("title"), "description": item.get("description")}
                for item in (web or [])
            ]
        if data.get("warning"):
            out["warning"] = data.get("warning")
        return out

    async def map(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        self._require_keyed(cfg, "map")
        body: dict[str, Any] = {"url": args.url, "limit": args.limit}
        if args.include_subdomains:
            body["includeSubdomains"] = True
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
            headers=self._headers(cfg),
        ) as client:
            data = await http_mod.request_json(client, "POST", "map", provider="firecrawl", json_body=body)
        self._check(data, endpoint="map")
        links = data.get("links") or []
        return {"data": [{"url": l.get("url"), "title": l.get("title"), "description": l.get("description")} for l in links]}

    async def _poll_job(self, client: Any, path: str, *, poll_timeout: int) -> dict[str, Any]:
        """轮询 firecrawl 异步 job（crawl/extract）直到终态或超时。"""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + poll_timeout
        while True:
            data = await http_mod.request_json(client, "GET", path, provider="firecrawl")
            self._check(data, endpoint=path)
            status = data.get("status")
            if status in ("completed", "failed", "cancelled"):
                return data
            if loop.time() >= deadline:
                return {"status": "timeout", "partial": data.get("data")}
            await asyncio.sleep(2)

    async def crawl(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        self._require_keyed(cfg, "crawl")
        body: dict[str, Any] = {
            "url": args.url,
            "limit": args.limit,
            "scrapeOptions": {"formats": ["markdown"]},
        }
        if args.max_depth:
            body["maxDiscoveryDepth"] = args.max_depth
        if args.include_paths:
            body["includePaths"] = args.include_paths
        if args.exclude_paths:
            body["excludePaths"] = args.exclude_paths
        if args.allow_subdomains:
            body["allowSubdomains"] = True
        if args.allow_external:
            body["allowExternalLinks"] = True
        if args.sitemap:
            body["sitemap"] = args.sitemap
        if args.prompt:
            body["prompt"] = args.prompt

        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
            headers=self._headers(cfg),
        ) as client:
            submit = await http_mod.request_json(client, "POST", "crawl", provider="firecrawl", json_body=body)
            self._check(submit, endpoint="crawl")
            job_id = submit.get("id")
            if args.async_submit:
                return {"id": job_id, "url": submit.get("url"), "status": "submitted"}
            data = await self._poll_job(client, f"crawl/{job_id}", poll_timeout=args.poll_timeout)
        status = data.get("status")
        pages: list[dict[str, Any]] = []
        for p in data.get("data", []) or []:
            item: dict[str, Any] = {"url": p.get("url")}
            md = p.get("markdown")
            if md:
                item["content"] = md
            pages.append(item)
        out: dict[str, Any] = {"status": status, "results": pages}
        if data.get("total") is not None:
            out["total"] = data.get("total")
        if data.get("completed") is not None:
            out["completed"] = data.get("completed")
        if status != "completed":
            out["note"] = "job not completed; partial or timeout. Resubmit with --async-submit and poll crawl/<id>."
        return out

    async def extract(self, args: Any) -> dict[str, Any]:
        from ..errors import ArgsError

        cfg = self._cfg()
        self._require_keyed(cfg, "extract")
        body: dict[str, Any] = {"urls": args.urls}
        if args.prompt:
            body["prompt"] = args.prompt
        if args.schema:
            try:
                body["schema"] = json.loads(args.schema)
            except (ValueError, json.JSONDecodeError) as e:
                raise ArgsError(f"firecrawl extract: --schema 不是合法 JSON ({e})", provider="firecrawl") from e
        if not args.prompt and not args.schema:
            raise ArgsError("firecrawl extract: 需要 --prompt 或 --schema 之一", provider="firecrawl")
        if args.enable_web_search:
            body["enableWebSearch"] = True
        if args.agent:
            body["agent"] = {"model": "FIRE-1"}

        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
            headers=self._headers(cfg),
        ) as client:
            submit = await http_mod.request_json(client, "POST", "extract", provider="firecrawl", json_body=body)
            self._check(submit, endpoint="extract")
            job_id = submit.get("id")
            if args.async_submit:
                return {"id": job_id, "status": "submitted"}
            data = await self._poll_job(client, f"extract/{job_id}", poll_timeout=args.poll_timeout)
        out: dict[str, Any] = {"status": data.get("status"), "data": data.get("data")}
        if data.get("expiresAt"):
            out["expiresAt"] = data.get("expiresAt")
        if data.get("status") != "completed":
            out["note"] = "job not completed; partial or timeout. Resubmit with --async-submit and poll extract/<id>."
        return out

    async def interact(self, args: Any) -> dict[str, Any]:
        """有状态浏览器会话：在已有 scrape 会话里执行 prompt 或 code。

        CLI 跨进程无状态，scrape_id 由调用方透传：--scrape-id 复用，或 --url 新建一次 scrape 拿 scrapeId。
        """
        from ..errors import ArgsError, ProviderError

        cfg = self._cfg()
        mode = self._resolve_mode(args, cfg)
        # --stop：配合 --scrape-id，DELETE 会话
        if args.stop:
            if not args.scrape_id:
                raise ArgsError("firecrawl interact: --stop 需要配合 --scrape-id", provider="firecrawl")
            async with http_mod.make_client(
                proxy_url=resolve_proxy(self.config.proxy.url),
                timeout=cfg.timeout,
                base_url=cfg.base_url,
            ) as client:
                await self._request_keyless(
                    client, "DELETE", f"scrape/{args.scrape_id}/interact",
                    cfg=cfg, mode=mode, endpoint="interact",
                )
            return {"stopped": True, "scrapeId": args.scrape_id}

        # 模式校验：prompt 与 --code 二选一
        if args.prompt and args.code:
            raise ArgsError("firecrawl interact: prompt 与 --code 二选一", provider="firecrawl")
        if not args.prompt and not args.code:
            raise ArgsError("firecrawl interact: 需要 prompt 或 --code", provider="firecrawl")

        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
        ) as client:
            # 解析会话句柄：--scrape-id 优先；否则用 --url 发起 scrape 拿 metadata.scrapeId
            scrape_id = args.scrape_id
            if not scrape_id:
                if not args.url:
                    raise ArgsError(
                        "firecrawl interact: 需要 --scrape-id 或 --url（用于新建会话）", provider="firecrawl"
                    )
                seed = await self._request_keyless(
                    client, "POST", "scrape", cfg=cfg, mode=mode,
                    json_body={"url": args.url, "formats": ["markdown"]}, endpoint="scrape",
                )
                seed_inner = seed.get("data") or {}
                meta = seed_inner.get("metadata") or {}
                scrape_id = meta.get("scrapeId")
                if not scrape_id:
                    raise ProviderError(
                        "firecrawl interact: scrape 响应未包含 metadata.scrapeId",
                        provider="firecrawl", details=seed,
                    )

            # 构造 interact body：prompt 模式 或 code 模式
            body: dict[str, Any]
            if args.code:
                body = {"code": args.code, "language": args.language, "timeout": args.timeout}
            else:
                body = {"prompt": args.prompt}

            data = await self._request_keyless(
                client, "POST", f"scrape/{scrape_id}/interact",
                cfg=cfg, mode=mode, json_body=body, endpoint="interact",
            )

        # 透传会话句柄 + 执行结果（prompt→output，code→stdout/result/stderr/exitCode/killed）
        out: dict[str, Any] = {"scrapeId": scrape_id}
        for k in ("output", "stdout", "result", "stderr", "exitCode", "killed",
                  "cdpUrl", "liveViewUrl", "interactiveLiveViewUrl"):
            if data.get(k) is not None:
                out[k] = data.get(k)
        return out

    async def agent(self, args: Any) -> dict[str, Any]:
        """异步 agent 数据提取：POST /v2/agent 拿 id，轮询 GET /v2/agent/{id} 到 completed/failed。"""
        from ..errors import ArgsError

        cfg = self._cfg()
        self._require_keyed(cfg, "agent")
        body: dict[str, Any] = {"prompt": args.prompt, "model": args.model}
        if args.urls:
            body["urls"] = args.urls
        if args.schema:
            try:
                body["schema"] = json.loads(args.schema)
            except (ValueError, json.JSONDecodeError) as e:
                raise ArgsError(f"firecrawl agent: --schema 不是合法 JSON ({e})", provider="firecrawl") from e
        if args.max_credits is not None:
            body["maxCredits"] = args.max_credits
        if args.strict:
            body["strictConstrainToURLs"] = True

        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
            headers=self._headers(cfg),
        ) as client:
            submit = await http_mod.request_json(client, "POST", "agent", provider="firecrawl", json_body=body)
            self._check(submit, endpoint="agent")
            job_id = submit.get("id")
            if args.async_submit:
                return {"id": job_id, "status": "submitted"}
            data = await self._poll_job(client, f"agent/{job_id}", poll_timeout=args.poll_timeout)

        out: dict[str, Any] = {"status": data.get("status"), "data": data.get("data")}
        if data.get("model"):
            out["model"] = data.get("model")
        if data.get("creditsUsed") is not None:
            out["creditsUsed"] = data.get("creditsUsed")
        if data.get("expiresAt"):
            out["expiresAt"] = data.get("expiresAt")
        if data.get("error"):
            out["error"] = data.get("error")
        if data.get("status") != "completed":
            out["note"] = "job not completed; partial or timeout. Resubmit with --async-submit and poll agent/<id>."
        return out

    async def monitor(self, args: Any) -> dict[str, Any]:
        """列出当前团队所有进行中的 crawl 任务（GET /v2/crawl/active，同步）。

        注意：这与 Firecrawl 的 Monitoring（定时调度 + 变更通知）feature 不同；此处按 active-crawls 端点实现。
        OpenAPI 的 required 误写为 data，实际响应字段是 crawls，两个字段都兼容。
        """
        cfg = self._cfg()
        self._require_keyed(cfg, "monitor")
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
            headers=self._headers(cfg),
        ) as client:
            data = await http_mod.request_json(client, "GET", "crawl/active", provider="firecrawl")
        self._check(data, endpoint="crawl/active")
        # 兼容文档不一致：properties 定义 crawls，required 却写 data
        crawls = data.get("crawls")
        if crawls is None:
            crawls = data.get("data") or []
        return {
            "data": [
                {"id": c.get("id"), "url": c.get("url"), "status": c.get("status")}
                for c in crawls
            ]
        }

    async def parse(self, args: Any) -> dict[str, Any]:
        """上传本地文件解析（POST /v2/parse，multipart/form-data，keyless）。

        走 _request_keyless 的 multipart 分支（_do_request files=...），享受与 scrape 等一致的
        key 策略（auto/keyless/use_key）与错误映射。
        """
        from pathlib import Path

        from ..errors import ArgsError

        cfg = self._cfg()
        mode = self._resolve_mode(args, cfg)
        path = Path(args.file)
        if not path.is_file():
            raise ArgsError(f"firecrawl parse: 文件不存在: {args.file}", provider="firecrawl")
        allowed = {".html", ".htm", ".pdf", ".docx", ".doc", ".odt", ".rtf", ".xlsx", ".xls"}
        suffix = path.suffix.lower()
        if suffix not in allowed:
            raise ArgsError(
                f"firecrawl parse: 不支持的扩展名 {suffix}（允许 {','.join(sorted(allowed))}）",
                provider="firecrawl",
            )
        file_bytes = path.read_bytes()
        if len(file_bytes) > 50 * 1024 * 1024:
            raise ArgsError("firecrawl parse: 文件超过 50MB 上限", provider="firecrawl")

        # 构造 options（JSON part）
        options: dict[str, Any] = {"formats": args.format}
        if args.only_main_content:
            options["onlyMainContent"] = True
        if args.include_tags:
            options["includeTags"] = args.include_tags
        if args.exclude_tags:
            options["excludeTags"] = args.exclude_tags
        if args.timeout is not None:
            options["timeout"] = args.timeout
        if args.pdf_mode:
            parser: dict[str, Any] = {"type": "pdf", "mode": args.pdf_mode}
            if args.max_pages is not None:
                parser["maxPages"] = args.max_pages
            options["parsers"] = [parser]

        # multipart：file 是二进制 part，options 是 application/json part（无 filename）
        files = {
            "file": (path.name, file_bytes, "application/octet-stream"),
            "options": (None, json.dumps(options), "application/json"),
        }

        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
        ) as client:
            data = await self._request_keyless(
                client, "POST", "parse", cfg=cfg, mode=mode, files=files, endpoint="parse",
            )

        # ScrapeResponse 结构：markdown 映射到 content（对齐 api-contract §2.1）
        inner = data.get("data") or {}
        out: dict[str, Any] = {}
        md = inner.get("markdown")
        if md:
            out["content"] = md
        for k in ("html", "rawHtml", "summary", "links", "images"):
            if inner.get(k) is not None:
                out[k] = inner.get(k)
        meta = inner.get("metadata") or {}
        if meta:
            out["metadata"] = meta
        return out
