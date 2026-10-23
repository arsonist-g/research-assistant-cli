"""fetch 命令：跨工具爬取增强（PM §4.2 / api-contract §2.2）。

    research-assistant fetch <url> [<url>...] [--write <path>] [--format markdown|html|text] [--timeout N] [--no-browser]
                              [--login|--no-login] [--concurrency N]

路径（先直取、再付费抓取、最后浏览器）：
    0) GitHub 文件 / 仓库 URL → GitHub API（DEC-028，method="github"）
    0b) 站点 URL 规则命中：按 transport 分流（http 直取 / browser 交给浏览器层 / html 只做预处理）。
        规则只描述 URL 层；响应由规则引用的适配器解析。失败自动落回普通链路
        （method="rule"，规则 id 记入结果与快照）
    1) 普通接口（tavily extract / firecrawl scrape，直接出 md）
    2) 失败回退 headless 浏览器 + CF auto-detect
批量 = 单浏览器多页并发（ADR-0004）。落盘只写快照（D8）。
响应：FetchResult[]{url, method∈{github,rule,normal,browser}, rule?, status, md_path, error?}
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
from ..fetch.rule_http import fetch_rule_http
from ..github_urls import is_github_url
from ..providers.github import fetch_github_text
from ..site_rules import RuleMatch, match_site_rule

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

    # 0) GitHub 文件 / 仓库 URL：直接走 GitHub API（DEC-028）。公开仓库取文件走 raw（零配额、无需凭据）；
    #    取不到则记 None，由 1)/2) 照常兜底 —— best-effort，不打断既有回退链。
    #    method 记 "github"：自动接管必须是**可观测**的替换（快照 frontmatter + 返回值），不是静默换掉内容形态。
    github_md: dict[str, str] = {}
    gh_urls = [u for u in urls if is_github_url(u)]
    if gh_urls:
        gh_sem = asyncio.Semaphore(args.concurrency)

        async def _gated_gh(u: str) -> str | None:
            async with gh_sem:
                try:
                    return await asyncio.wait_for(
                        fetch_github_text(config, u, args.format), timeout=args.timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning("github 接管超时 %s（%ds），落回普通路径", u, args.timeout)
                    return None

        gh_list = await asyncio.gather(*[_gated_gh(u) for u in gh_urls], return_exceptions=True)
        for u, res in zip(gh_urls, gh_list):
            if isinstance(res, str) and res.strip():
                github_md[u] = res

    rest_urls = [u for u in urls if u not in github_md]

    # 0b) 站点规则（URL 层）：按 transport 分流。
    #     - http 规则先直取数据接口；失败落回普通层。
    #     - browser 规则跳过普通接口，交给浏览器层（CF + 登录态）；--no-browser 时按原链路走。
    sem = asyncio.Semaphore(args.concurrency)
    rule_matches: dict[str, RuleMatch] = {}
    for u in rest_urls:
        hit = match_site_rule(u)
        if hit is not None:
            rule_matches[u] = hit

    http_rule_urls = [u for u, h in rule_matches.items() if h.rule.transport == "http"]
    browser_rule_urls = [
        u for u, h in rule_matches.items()
        if h.rule.transport == "browser" and not args.no_browser
    ]
    normal_urls = [u for u in rest_urls if u not in browser_rule_urls]

    # 0c) http 规则直取（登录 cookie 按 host 过滤；不启浏览器）
    rule_md: dict[str, str] = {}
    rule_ids: dict[str, str] = {}
    if http_rule_urls:
        async def _gated_rule(u: str) -> str | None:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        fetch_rule_http(config, rule_matches[u], login=use_login),
                        timeout=args.timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning("站点规则(http)超时 %s（%ds），落回普通路径", u, args.timeout)
                    return None
                except Exception as e:
                    logger.warning("站点规则(http)失败 %s: %s，落回普通路径", u, e)
                    return None

        http_list = await asyncio.gather(*[_gated_rule(u) for u in http_rule_urls], return_exceptions=True)
        for u, res in zip(http_rule_urls, http_list):
            if isinstance(res, str) and res.strip():
                rule_md[u] = res
                rule_ids[u] = rule_matches[u].rule_id
        # 成功的 http 规则不再进普通层；失败的留在 normal_urls 里继续兜底
        normal_urls = [u for u in normal_urls if u not in rule_md]

    # 1) 普通抓取：semaphore 限并发（与 browser 层对齐）+ 单 url 超时
    async def _gated(u: str) -> str | None:
        async with sem:
            try:
                return await asyncio.wait_for(
                    fetch_normal(config, u, args.format), timeout=args.timeout
                )
            except asyncio.TimeoutError:
                logger.warning("normal 抓取超时 %s（%ds），记失败", u, args.timeout)
                return None

    normal_results_list = await asyncio.gather(*[_gated(u) for u in normal_urls], return_exceptions=True)
    normal_results: dict[str, str | None] = {}
    for url, res in zip(normal_urls, normal_results_list):
        if isinstance(res, Exception) or res is None:
            normal_results[url] = None
        else:
            normal_results[url] = res

    # 2) 浏览器层：浏览器规则 + 普通接口失败的回退（透传 format + timeout + concurrency）
    failed = [u for u in normal_urls if normal_results.get(u) is None]
    browser_targets = ([] if args.no_browser else failed) + browser_rule_urls
    rule_hits: dict[str, str] = {}
    browser_results: dict[str, str | None] = {}
    if browser_targets:
        try:
            browser_results = await fetch_with_browser(
                config, browser_targets, login=use_login, concurrency=args.concurrency,
                fmt=args.format, timeout=args.timeout, rule_hits=rule_hits,
            )
        except ResearchAssistantError:
            browser_results = {u: None for u in browser_targets}
    for u, rid in rule_hits.items():
        md = browser_results.get(u)
        if md:
            rule_md[u] = md
            rule_ids[u] = rid

    # 3) 组装 FetchResult + 落盘
    results: list[dict[str, Any]] = []
    for url in urls:
        md = github_md.get(url)
        rule_id = rule_ids.get(url)
        if md is not None:
            method = "github"
            status = "success"
            error = None
        elif rule_id is not None:
            md = rule_md.get(url)
            method = "rule"
            if md:
                status = "success"
                error = None
            else:
                status = "failed"
                error = "站点规则与浏览器回退均失败"
        else:
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
            md_path = write_snapshot(
                url, method, md, args.write, extra={"rule": rule_id} if rule_id else None
            )
        results.append(
            {
                "url": url,
                "method": method,
                "status": status,
                "md_path": md_path,
                **({"rule": rule_id} if rule_id else {}),
                **({"error": error} if error else {}),
            }
        )
    return {"results": results}

