"""browser provider：本地浏览器作为一个"平台"（与 exa/tavily/firecrawl 同级，走 registry 自动发现）。

不是 HTTP API（无 key/base_url），直接驱动本地浏览器：
  - browser fetch：headless DrissionPage + CF auto-detect 抓取（跳过普通 API，直接浏览器入口）。
  - browser search：headless DrissionPage 抓搜索引擎结果页（必应国内/国际、Google），页数表翻页。
与 fetch 命令的浏览器层共用 fetch_with_browser（统一 headless + CF auto-detect）。
config 无需 [[provider]] type=browser，运行参数从 config.browser 读（channel 等）；永远可用。
"""

from __future__ import annotations

import argparse
from typing import Any

from ..errors import ArgsError
from ..fetch import fetch_with_browser, write_snapshot
from ..fetch.search_engine import search_engine
from .base import ArgSpec, Capability, Provider, bounded_int
from .registry import register


@register
class BrowserProvider(Provider):
    type = "browser"
    command = "browser"
    aliases = ["br"]
    help = "Local browser platform: headless fetch with CF auto-detect, or headless search-engine search."

    def capabilities(self) -> list[Capability]:
        return [
            Capability(
                name="fetch",
                help="Headed browser fetch with CF auto-detect (skips normal API).",
                args=[
                    ArgSpec(["urls"], kind="positional", nargs="+", metavar="URL", help="URL(s) to fetch."),
                    ArgSpec(["--no-login"], action="store_true", help="Do not inject login cookies (default injects)."),
                    ArgSpec(["--concurrency"], type=bounded_int(1, 16), default=4, help="Browser concurrency (1-16, default 4)."),
                    ArgSpec(["--format"], choices=["markdown", "html"], default="markdown", help="Output format (default markdown)."),
                    ArgSpec(["--timeout"], type=bounded_int(1, 300), default=60, help="Per-URL fetch timeout in seconds (1-300, default 60)."),
                    ArgSpec(["--write"], metavar="PATH", help="Write content to this path (single URL only)."),
                ],
                handler=self.fetch,
            ),
            Capability(
                name="search",
                help="Headless search-engine search (default bing-intl; bing-cn|bing-intl|google).",
                args=[
                    ArgSpec(["query"], kind="positional", metavar="QUERY", help="Search query."),
                    ArgSpec(
                        ["--engine"],
                        choices=["bing-cn", "bing-intl", "google"],
                        default="bing-intl",
                        help="Search engine (default bing-intl; intl avoids CN-sensitive-word filtering).",
                    ),
                    ArgSpec(["--limit"], type=int, default=10, help="Min results to collect (default 10)."),
                    ArgSpec(["--max-pages"], type=int, default=10, help="Max pages to paginate (default 10; engine may offer fewer)."),
                    ArgSpec(["--timeout"], type=bounded_int(1, 300), default=60, help="Overall search timeout in seconds (1-300, default 60)."),
                ],
                handler=self.search,
            ),
        ]

    async def fetch(self, args_ns: argparse.Namespace) -> dict[str, Any]:
        urls = list(args_ns.urls)
        if args_ns.write and len(urls) > 1:
            raise ArgsError("browser fetch: --write 仅支持单个 URL")
        use_login = not getattr(args_ns, "no_login", False)
        raw = await fetch_with_browser(
            self.config, urls, login=use_login, concurrency=args_ns.concurrency,
            fmt=args_ns.format, timeout=args_ns.timeout,
        )
        results: list[dict[str, Any]] = []
        for url in urls:
            md = raw.get(url)
            if md:
                md_path = write_snapshot(url, "browser", md, args_ns.write)
                results.append(
                    {"url": url, "method": "browser", "status": "success", "md_path": md_path}
                )
            else:
                results.append(
                    {
                        "url": url,
                        "method": "browser",
                        "status": "failed",
                        "md_path": None,
                        "error": "浏览器抓取失败（CF 未通过/无浏览器/内容过短）",
                    }
                )
        return {"results": results}

    async def search(self, args_ns: argparse.Namespace) -> dict[str, Any]:
        return await search_engine(
            self.config, args_ns.query, args_ns.engine, args_ns.limit, args_ns.max_pages,
            timeout=args_ns.timeout,
        )
