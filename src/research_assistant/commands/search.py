"""search 命令：聚合搜索源 API（exa/tavily 等）的统一搜索入口（原义）。

    research-assistant search "<query>" [--providers exa,tavily] [--limit N]

search 是「调搜索 API」的纯粹语义：把 query 发给 exa/tavily（等 web 搜索源 provider）
各自的 search 能力，聚合去重，返回候选源。它不是 LLM 问答（自然语言意图问答见 ask）。

默认查已配置的 exa、tavily；--providers CSV 指定子集或加入 firecrawl（如 exa,firecrawl）。

响应：{query, candidates[]{url, title?, snippet?}, sources[]}
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from .. import providers as providers_pkg
from ..config import Config
from ..errors import ArgsError
from ..providers.base import bounded_int

logger = logging.getLogger("research_assistant.commands.search")

NAME = "search"
ALIASES: list[str] = []
HELP = "Aggregate web search across search-source APIs (exa/tavily); not an LLM."

# 默认搜索源（顺序即优先级）：exa/tavily 需已配置；browser 永远可用（本地浏览器，免配置兜底）。
# firecrawl 需显式 --providers 加入（有 key 才有意义）。
_DEFAULT_SOURCES = ("exa", "tavily", "browser")


def _resolve_wanted(spec: str, configured: set[str]) -> list[str]:
    """解析要聚合的搜索源：--providers 显式指定；否则已配置的 exa/tavily + browser（免配置兜底）。"""
    spec = (spec or "").strip()
    if spec:
        return [s.strip() for s in spec.split(",") if s.strip()]
    # browser 永远可用（本地浏览器），exa/tavily 需已配置 → 默认永远至少有 browser（开箱即用）
    return [p for p in _DEFAULT_SOURCES if p == "browser" or p in configured]


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(NAME, aliases=ALIASES, help=HELP, description=HELP)
    p.add_argument("query", help="Search query (keywords for the search APIs).")
    p.add_argument(
        "--providers",
        help="Comma list of search-source providers (default: configured exa,tavily + browser). "
        "e.g. exa,tavily,firecrawl,browser.",
    )
    p.add_argument("--limit", type=int, default=5, help="Max results per provider (default 5).")
    p.add_argument("--include-domains", nargs="+", metavar="DOMAIN", help="Restrict to domains (exa/tavily; browser ignores).")
    p.add_argument("--exclude-domains", nargs="+", metavar="DOMAIN", help="Drop these domains (exa/tavily; browser ignores).")
    p.add_argument("--start-date", metavar="YYYY-MM-DD", help="Results published on/after this date (exa/tavily; browser ignores).")
    p.add_argument("--end-date", metavar="YYYY-MM-DD", help="Results published on/before this date (exa/tavily; browser ignores).")
    p.add_argument("--text", action="store_true", help="Include page text per result (exa/tavily; browser ignores).")
    p.add_argument("--timeout", type=bounded_int(1, 300), default=60, help="Overall search timeout in seconds per provider (1-300, default 60); a slow source is skipped, others continue.")
    p.set_defaults(_handler=run)


async def run(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    wanted = _resolve_wanted(args.providers or "", {p.type for p in config.providers})
    if not wanted:
        raise ArgsError(
            "search: 没有可用的搜索源 provider；请用 --providers 指定",
            provider="search",
        )

    candidates: list[dict[str, Any]] = []
    used: list[str] = []
    for ptype in wanted:
        try:
            extra = await asyncio.wait_for(
                _from_provider(
                    config,
                    ptype,
                    query=args.query,
                    limit=args.limit,
                    include_domains=args.include_domains,
                    exclude_domains=args.exclude_domains,
                    start_date=args.start_date,
                    end_date=args.end_date,
                    text=args.text,
                    timeout=args.timeout,
                ),
                timeout=args.timeout,
            )
        except asyncio.TimeoutError:
            # 单家超时不阻断其余源；跳过该家继续聚合
            logger.warning("search: provider %s 超时（%ds），跳过", ptype, args.timeout)
            continue
        except Exception:
            # 单家失败不阻断其余源（如某家未配置/限流）；跳过该家继续聚合
            continue
        _merge_unique(candidates, extra)
        used.append(ptype)

    return {"query": args.query, "candidates": candidates, "sources": used}


async def _from_provider(
    config: Config,
    ptype: str,
    *,
    query: str,
    limit: int,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
    start_date: str | None,
    end_date: str | None,
    text: bool,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """调用指定 provider 的 search 能力（复用插件实例），返回候选源列表。

    search 层暴露的跨家通用参数（limit / include-domains / exclude-domains /
    start-date / end-date / text）在此映射到各 provider 的原生字段；browser 不支持
    域名/日期/正文过滤，这些对其静默忽略。各家专有参数（exa --type、tavily --topic 等）
    不在 search 层暴露，要用请走 exa/tavily 子命令。
    """
    classes = providers_pkg.all_provider_classes()
    pclass = classes.get(ptype)
    if pclass is None:
        raise ArgsError(f"search: 未知 provider '{ptype}'", provider=ptype)
    provider = pclass(config)
    cap = next((c for c in provider.capabilities() if c.name == "search"), None)
    if cap is None:
        raise ArgsError(f"search: provider '{ptype}' 无 search 能力", provider=ptype)

    ns = argparse.Namespace(query=query)
    # 跨家通用参数 → 各 provider 原生字段
    ns.num_results = limit          # exa
    ns.max_results = limit          # tavily
    ns.limit = limit                # browser
    ns.include_domains = include_domains   # exa/tavily 同名
    ns.exclude_domains = exclude_domains
    ns.start_date = start_date
    ns.end_date = end_date
    ns.text = bool(text)                          # exa: 含正文
    ns.include_raw_content = "markdown" if text else None  # tavily: 含正文 → markdown
    # 其余各 provider 专有字段默认值（handler 可能读取，避免 AttributeError）
    for attr, default in (
        ("type", "auto"),
        ("depth", "basic"),
        ("topic", "general"),
        ("highlights", False),
        ("category", None),
        ("scrape", False),
        ("sources", None),
        ("include_subdomains", False),
        ("only_main_content", False),
        ("wait_for", None),
        ("extract_depth", "basic"),
        ("format", "markdown"),
        ("include_answer", None),
        ("time_range", None),
        ("include_text", None),
        ("exclude_text", None),
        ("chunks_per_source", None),
        ("country", None),
        ("days", None),
        ("include_images", False),
        ("include_image_descriptions", False),
        ("include_favicon", False),
        ("auto_parameters", False),
        ("engine", "bing-intl"),  # browser search 用
        ("max_pages", 10),        # browser search 用
    ):
        if not hasattr(ns, attr):
            setattr(ns, attr, default)
    ns.timeout = timeout  # browser search 整体超时（其余 provider 的 search handler 不读此字段）
    result = await cap.handler(ns)
    return _normalize_provider_results(result, limit)


def _normalize_provider_results(result: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """把各 provider 的 search 响应归一为候选源列表。"""
    items: list[Any] = []
    for key in ("results", "data", "contents"):
        raw = result.get(key)
        if isinstance(raw, list):
            items = raw
            break
    out: list[dict[str, Any]] = []
    for it in items[:limit]:
        if not isinstance(it, dict):
            continue
        url = it.get("url") or it.get("id")
        if not url:
            continue
        item: dict[str, Any] = {"url": url}
        title = it.get("title") or it.get("name")
        if title:
            item["title"] = title
        snippet = it.get("content") or it.get("text") or it.get("snippet") or it.get("description")
        if snippet:
            item["snippet"] = snippet
        out.append(item)
    return out


def _merge_unique(target: list[dict[str, Any]], extras: list[dict[str, Any]]) -> None:
    seen = {c["url"] for c in target}
    for e in extras:
        if e["url"] not in seen:
            target.append(e)
            seen.add(e["url"])
