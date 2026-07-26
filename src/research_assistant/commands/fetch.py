"""fetch 命令：跨工具爬取增强（PM §4.2 / api-contract §2.2）。

    research-assistant fetch <url> [<url>...] [--output <path>] [--no-browser]
                              [--login|--no-login] [--concurrency N]

普通接口（tavily extract / firecrawl scrape，直接出 md）→ 失败回退 headed 浏览器 + CF auto-detect。
批量 = 单浏览器多页并发（ADR-0004）。落盘只写快照（D8）。
响应：FetchResult[]{url, method, status, md_path, error?}
"""

from __future__ import annotations

import argparse
from typing import Any

from .. import config as config_mod
from ..config import Config
from ..errors import ArgsError, ResearchAssistantError
from ..fetch import fetch_normal, fetch_with_browser, write_snapshot

NAME = "fetch"
ALIASES: list[str] = ["f"]
HELP = "Cross-tool page fetch with browser fallback."


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(NAME, aliases=ALIASES, help=HELP, description=HELP)
    p.add_argument("urls", nargs="+", metavar="URL", help="URL(s) to fetch.")
    p.add_argument("--output", help="Write markdown to this path (single URL only).")
    p.add_argument("--no-browser", action="store_true", help="Disable browser fallback.")
    login = p.add_mutually_exclusive_group()
    login.add_argument("--login", action="store_true", default=True, help="Inject login cookies (default).")
    login.add_argument("--no-login", action="store_true", help="Do not inject login cookies.")
    p.add_argument("--concurrency", type=int, default=4, help="Browser page concurrency (default 4).")
    p.set_defaults(_handler=run)


async def run(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    urls = list(args.urls)
    if args.output and len(urls) > 1:
        raise ArgsError("fetch: --output 仅支持单个 URL")

    use_login = not getattr(args, "no_login", False)

    # 1) 普通抓取（并发）
    import asyncio

    normal_results: dict[str, str | None] = {}
    normal_results_list = await asyncio.gather(
        *[fetch_normal(config, u) for u in urls], return_exceptions=True
    )
    for url, res in zip(urls, normal_results_list):
        if isinstance(res, Exception) or res is None:
            normal_results[url] = None
        else:
            normal_results[url] = res

    # 2) 失败的回退浏览器
    failed = [u for u in urls if normal_results.get(u) is None]
    browser_results: dict[str, str | None] = {}
    if failed and not args.no_browser:
        try:
            browser_results = await fetch_with_browser(
                config, failed, login=use_login, concurrency=args.concurrency
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
            md_path = write_snapshot(url, method, md, args.output)
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

