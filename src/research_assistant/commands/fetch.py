"""fetch 命令：跨工具爬取增强（PM §4.2 / api-contract §2.2）。

    research-assistant fetch <url> [<url>...] [--write <path>] [--format markdown|html|text] [--timeout N] [--no-browser]
                              [--login|--no-login] [--concurrency N]

普通接口（tavily extract / firecrawl scrape，直接出 md）→ 失败回退 headless 浏览器 + CF auto-detect。
批量 = 单浏览器多页并发（ADR-0004）。落盘只写快照（D8）。
响应：FetchResult[]{url, method, status, md_path, error?}
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from .. import config as config_mod
from ..config import Config
from ..errors import ArgsError, ResearchAssistantError
from ..fetch import fetch_normal, fetch_with_browser, write_snapshot
from ..providers.base import bounded_int

NAME = "fetch"
ALIASES: list[str] = ["f"]
HELP = "Cross-tool page fetch with browser fallback."

logger = logging.getLogger("research_assistant.commands.fetch")


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(NAME, aliases=ALIASES, help=HELP, description=HELP)
    p.add_argument("urls", nargs="+", metavar="URL", help="URL(s) to fetch.")
    p.add_argument("--write", metavar="PATH", help="Write content to this path (single URL only).")
    p.add_argument("--format", choices=["markdown", "html", "text"], default="markdown",
                   help="Output format; each source uses it if it supports, else falls back to markdown.")
    p.add_argument("--timeout", type=bounded_int(1, 300), default=60, help="Per-URL fetch timeout in seconds (1-300, default 60).")
    p.add_argument("--no-browser", action="store_true", help="Disable browser fallback.")
    login = p.add_mutually_exclusive_group()
    login.add_argument("--login", action="store_true", default=True, help="Inject login cookies (default).")
    login.add_argument("--no-login", action="store_true", help="Do not inject login cookies.")
    p.add_argument("--concurrency", type=bounded_int(1, 16), default=4,
                   help="Max concurrent fetches for both normal API and browser layers (1-16, default 4).")
    p.set_defaults(_handler=run)


async def run(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    urls = list(args.urls)
    if args.write and len(urls) > 1:
        raise ArgsError("fetch: --write 仅支持单个 URL")

    use_login = not getattr(args, "no_login", False)

    import asyncio

    # 1) 普通抓取：semaphore 限并发（与 browser 层对齐）+ 单 url 超时
    sem = asyncio.Semaphore(args.concurrency)

    async def _gated(u: str) -> str | None:
        async with sem:
            try:
                return await asyncio.wait_for(
                    fetch_normal(config, u, args.format), timeout=args.timeout
                )
            except asyncio.TimeoutError:
                logger.warning("normal 抓取超时 %s（%ds），记失败", u, args.timeout)
                return None

    normal_results_list = await asyncio.gather(*[_gated(u) for u in urls], return_exceptions=True)
    normal_results: dict[str, str | None] = {}
    for url, res in zip(urls, normal_results_list):
        if isinstance(res, Exception) or res is None:
            normal_results[url] = None
        else:
            normal_results[url] = res

    # 2) 失败的回退浏览器（透传 format + timeout + concurrency）
    failed = [u for u in urls if normal_results.get(u) is None]
    browser_results: dict[str, str | None] = {}
    if failed and not args.no_browser:
        try:
            browser_results = await fetch_with_browser(
                config, failed, login=use_login, concurrency=args.concurrency,
                fmt=args.format, timeout=args.timeout,
            )
        except ResearchAssistantError:
            browser_results = {u: None for u in failed}

    # 3) 组装 FetchResult + 落盘
    results: list[dict[str, Any]] = []
    for url in urls:
        md = normal_results.get(url)
        if md is not None:
            method = "normal"
            status = "success"
            error = None
        else:
            md = browser_results.get(url)
            method = "browser"
            if md:
                status = "success"
                error = None
            else:
                status = "failed"
                error = "普通接口与浏览器回退均失败" if not args.no_browser else "普通接口失败且已禁用浏览器回退"
        md_path = None
        if md and status == "success":
            md_path = write_snapshot(url, method, md, args.write)
        results.append(
            {
                "url": url,
                "method": method,
                "status": status,
                "md_path": md_path,
                **({"error": error} if error else {}),
            }
        )
    return {"results": results}

