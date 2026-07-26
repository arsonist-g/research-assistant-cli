"""Tavily provider（自写 HTTP，ADR-0010）。

API（webReader 查证 docs.tavily.com，2026-07）：
    POST https://api.tavily.com/search    鉴权 Authorization: Bearer <key>
        body: query / search_depth(ultra-fast|fast|basic|advanced) /
              topic(general|news|finance) / time_range(day|week|month|year) /
              max_results / chunks_per_source(1-3) / days /
              include_answer(basic|true|advanced) /
              include_raw_content(markdown|text) /
              include_images / include_image_descriptions / include_favicon /
              country / auto_parameters / start_date / end_date /
              include_domains[] / exclude_domains[]
        resp: {results[]{title,url,content,raw_content?,score?}, answer?, images?}
    POST /extract
        body: urls[] / extract_depth(basic|advanced) / format(markdown|text)
        resp: {results[]{url,raw_content,...}, failed_results[]{url,error}}
    POST /map     同步阻塞，最长 150s
        body: url / instructions? / max_depth(1-5) / max_breadth(1-500) / limit /
              select_paths[] / select_domains[] / exclude_paths[] / exclude_domains[] /
              allow_external / timeout(10-150) / include_usage?
        resp: {base_url, results[<url:str>], response_time, usage?, request_id?}
    POST /crawl   同步阻塞，最长 150s
        body: url / instructions? / chunks_per_source(1-5) / max_depth(1-5) /
              max_breadth(1-500) / limit / select_paths[]/select_domains[]/
              exclude_paths[]/exclude_domains[] / allow_external /
              include_images / extract_depth(basic|advanced) / format(markdown|text) /
              include_favicon / timeout(10-150) / include_usage?
        resp: {base_url, results[]{url,raw_content,favicon?}, response_time, usage?, request_id?}

对齐 api-contract.md §2.1/§3：raw_content → content。
map/crawl 是服务端同步阻塞调用，handler 用 server timeout + 15s 覆盖客户端 timeout。
"""

from __future__ import annotations

from typing import Any

from ..config import ProviderConfig
from ..errors import ArgsError
from .. import http as http_mod
from ..proxy import resolve_proxy
from .base import ArgSpec, Capability, Provider
from .registry import register

DEFAULT_BASE_URL = "https://api.tavily.com"


def _split_csv(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        for part in str(v).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


@register
class TavilyProvider(Provider):
    type = "tavily"
    command = "tavily"
    aliases = ["tvly"]
    help = "Tavily web search, URL extract, site map, and recursive crawl."

    def capabilities(self) -> list[Capability]:
        return [
            Capability(
                name="search",
                help="Search the web with Tavily.",
                args=[
                    ArgSpec(["query"], kind="positional", help="Search query."),
                    ArgSpec(
                        ["--depth"],
                        choices=["ultra-fast", "fast", "basic", "advanced"],
                        default="basic",
                        help="Search depth (default basic).",
                    ),
                    ArgSpec(["--max-results"], type=int, default=10, help="Max results (default 10)."),
                    ArgSpec(
                        ["--topic"],
                        choices=["general", "news", "finance"],
                        default="general",
                        help="Search topic (default general).",
                    ),
                    ArgSpec(
                        ["--time-range"],
                        choices=["day", "week", "month", "year"],
                        help="Restrict to a time range.",
                    ),
                    ArgSpec(
                        ["--include-answer"],
                        choices=["basic", "true", "advanced"],
                        help="Include a synthesized answer.",
                    ),
                    ArgSpec(["--chunks-per-source"], type=int, help="Snippets per source (advanced, 1-3)."),
                    ArgSpec(["--include-raw-content"], choices=["markdown", "text"], help="Return full body per result."),
                    ArgSpec(["--include-domains"], nargs="+", metavar="DOMAIN", help="Restrict to domains."),
                    ArgSpec(["--exclude-domains"], nargs="+", metavar="DOMAIN", help="Exclude domains."),
                    ArgSpec(["--start-date"], metavar="YYYY-MM-DD", help="Results published on/after this date."),
                    ArgSpec(["--end-date"], metavar="YYYY-MM-DD", help="Results published on/before this date."),
                    ArgSpec(["--country"], metavar="COUNTRY", help="Boost results from a country (topic=general only)."),
                    ArgSpec(["--days"], type=int, help="For news topic: limit to last N days."),
                    ArgSpec(["--include-images"], action="store_true", help="Include query-related images."),
                    ArgSpec(["--include-image-descriptions"], action="store_true", help="Add a description per image."),
                    ArgSpec(["--include-favicon"], action="store_true", help="Include the favicon URL per result."),
                    ArgSpec(["--auto-parameters"], action="store_true", help="Let Tavily auto-tune params (2 credits)."),
                ],
                handler=self.search,
            ),
            Capability(
                name="extract",
                help="Extract clean content from one or more URLs.",
                args=[
                    ArgSpec(["urls"], kind="positional", nargs="+", metavar="URL", help="URL(s) to extract."),
                    ArgSpec(["--extract-depth"], choices=["basic", "advanced"], default="basic", help="Extract depth."),
                    ArgSpec(["--format"], choices=["markdown", "text"], default="markdown", help="Output format."),
                ],
                handler=self.extract,
            ),
            Capability(
                name="map",
                help="Map a site: list reachable URLs (graph traversal).",
                args=[
                    ArgSpec(["url"], kind="positional", help="Root URL to map."),
                    ArgSpec(["--instructions"], metavar="TEXT", help="Natural-language guidance for the crawler."),
                    ArgSpec(["--max-depth"], type=int, default=1, help="Max crawl depth (1-5)."),
                    ArgSpec(["--max-breadth"], type=int, default=10, help="Max links to follow per page (1-500)."),
                    ArgSpec(["--limit"], type=int, default=50, help="Total links to process."),
                    ArgSpec(["--select-paths"], nargs="+", metavar="REGEX", help="Keep only URLs matching these path regexes."),
                    ArgSpec(["--select-domains"], nargs="+", metavar="REGEX", help="Keep only these domains/subdomains."),
                    ArgSpec(["--exclude-paths"], nargs="+", metavar="REGEX", help="Drop URLs matching these path regexes."),
                    ArgSpec(["--exclude-domains"], nargs="+", metavar="REGEX", help="Drop these domains/subdomains."),
                    ArgSpec(["--allow-external"], action="store_true", help="Include external-domain links."),
                    ArgSpec(["--timeout"], type=int, default=60, help="Server-side timeout in seconds (10-150)."),
                    ArgSpec(["--include-usage"], action="store_true", help="Include credit usage in the response."),
                ],
                handler=self.map,
            ),
            Capability(
                name="crawl",
                help="Crawl a site recursively and extract each page's content.",
                args=[
                    ArgSpec(["url"], kind="positional", help="Root URL to crawl."),
                    ArgSpec(["--instructions"], metavar="TEXT", help="Natural-language guidance for the crawler."),
                    ArgSpec(["--chunks-per-source"], type=int, default=3, help="Chunks per source (1-5, needs --instructions)."),
                    ArgSpec(["--max-depth"], type=int, default=1, help="Max crawl depth (1-5)."),
                    ArgSpec(["--max-breadth"], type=int, default=20, help="Max links to follow per page (1-500)."),
                    ArgSpec(["--limit"], type=int, default=50, help="Total pages to process."),
                    ArgSpec(["--select-paths"], nargs="+", metavar="REGEX", help="Keep only URLs matching these path regexes."),
                    ArgSpec(["--select-domains"], nargs="+", metavar="REGEX", help="Keep only these domains/subdomains."),
                    ArgSpec(["--exclude-paths"], nargs="+", metavar="REGEX", help="Drop URLs matching these path regexes."),
                    ArgSpec(["--exclude-domains"], nargs="+", metavar="REGEX", help="Drop these domains/subdomains."),
                    ArgSpec(["--no-external"], action="store_false", default=True, dest="allow_external", help="Follow external links by default; pass to keep crawl site-local."),
                    ArgSpec(["--include-images"], action="store_true", help="Include images in results."),
                    ArgSpec(["--extract-depth"], choices=["basic", "advanced"], default="basic", help="Per-page extraction depth."),
                    ArgSpec(["--format"], choices=["markdown", "text"], default="markdown", help="Content format."),
                    ArgSpec(["--include-favicon"], action="store_true", help="Include favicon URL per result."),
                    ArgSpec(["--timeout"], type=int, default=60, help="Server-side timeout in seconds (10-150)."),
                    ArgSpec(["--include-usage"], action="store_true", help="Include credit usage in the response."),
                ],
                handler=self.crawl,
            ),
        ]

    def _cfg(self) -> ProviderConfig:
        cfg = self.config.require_provider("tavily")
        if not cfg.api_key:
            raise ArgsError("tavily: 缺少 api_key", provider="tavily")
        if not cfg.base_url:
            cfg.base_url = DEFAULT_BASE_URL
        return cfg

    async def search(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        body: dict[str, Any] = {
            "query": args.query,
            "search_depth": args.depth,
            "max_results": args.max_results,
            "topic": args.topic,
        }
        if args.time_range:
            body["time_range"] = args.time_range
        if args.include_answer:
            ia = args.include_answer
            body["include_answer"] = True if ia == "true" else ia
        if args.chunks_per_source:
            body["chunks_per_source"] = args.chunks_per_source
        if args.include_raw_content:
            body["include_raw_content"] = args.include_raw_content
        if args.include_domains:
            body["include_domains"] = _split_csv(args.include_domains)
        if args.exclude_domains:
            body["exclude_domains"] = _split_csv(args.exclude_domains)
        if args.start_date:
            body["start_date"] = args.start_date
        if args.end_date:
            body["end_date"] = args.end_date
        if args.country:
            body["country"] = args.country
        if args.days is not None:
            body["days"] = args.days
        if args.include_images:
            body["include_images"] = True
            if args.include_image_descriptions:
                body["include_image_descriptions"] = True
        if args.include_favicon:
            body["include_favicon"] = True
        if args.auto_parameters:
            body["auto_parameters"] = True

        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        ) as client:
            data = await http_mod.request_json(client, "POST", "search", provider="tavily", json_body=body)

        results = []
        for r in data.get("results", []) or []:
            item: dict[str, Any] = {"url": r.get("url"), "title": r.get("title")}
            if r.get("content"):
                item["content"] = r.get("content")
            if r.get("raw_content"):
                item["raw_content"] = r.get("raw_content")
            if r.get("score") is not None:
                item["score"] = r.get("score")
            if args.include_images and r.get("images"):
                item["images"] = r.get("images")
            results.append(item)
        out: dict[str, Any] = {"results": results}
        if data.get("answer"):
            out["answer"] = data.get("answer")
        if data.get("images"):
            out["images"] = data.get("images")
        return out

    async def extract(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        urls = _split_csv(args.urls)
        if not urls:
            raise ArgsError("tavily extract: 至少提供一个 url", provider="tavily")
        body = {"urls": urls, "extract_depth": args.extract_depth, "format": args.format}
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        ) as client:
            data = await http_mod.request_json(client, "POST", "extract", provider="tavily", json_body=body)

        results = []
        for r in data.get("results", []) or []:
            item: dict[str, Any] = {"url": r.get("url"), "content": r.get("raw_content") or ""}
            results.append(item)
        out: dict[str, Any] = {"results": results}
        if data.get("failed_results"):
            out["failed_results"] = data.get("failed_results")
        return out

    async def map(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        body: dict[str, Any] = {
            "url": args.url,
            "max_depth": args.max_depth,
            "max_breadth": args.max_breadth,
            "limit": args.limit,
            "timeout": args.timeout,
        }
        if args.instructions:
            body["instructions"] = args.instructions
        if args.select_paths:
            body["select_paths"] = args.select_paths
        if args.select_domains:
            body["select_domains"] = args.select_domains
        if args.exclude_paths:
            body["exclude_paths"] = args.exclude_paths
        if args.exclude_domains:
            body["exclude_domains"] = args.exclude_domains
        if args.allow_external:
            body["allow_external"] = True
        if args.include_usage:
            body["include_usage"] = True

        # map 服务端会阻塞到爬完或 timeout；客户端多给 15s 缓冲，且不低于 provider 默认 timeout
        client_timeout = max(cfg.timeout, float(args.timeout) + 15.0)
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=client_timeout,
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        ) as client:
            data = await http_mod.request_json(client, "POST", "map", provider="tavily", json_body=body)

        out: dict[str, Any] = {"base_url": data.get("base_url"), "results": list(data.get("results", []) or [])}
        if data.get("response_time") is not None:
            out["response_time"] = data.get("response_time")
        return out

    async def crawl(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        body: dict[str, Any] = {
            "url": args.url,
            "chunks_per_source": args.chunks_per_source,
            "max_depth": args.max_depth,
            "max_breadth": args.max_breadth,
            "limit": args.limit,
            "allow_external": bool(args.allow_external),
            "include_images": bool(args.include_images),
            "extract_depth": args.extract_depth,
            "format": args.format,
            "include_favicon": bool(args.include_favicon),
            "timeout": args.timeout,
        }
        if args.instructions:
            body["instructions"] = args.instructions
        if args.select_paths:
            body["select_paths"] = args.select_paths
        if args.select_domains:
            body["select_domains"] = args.select_domains
        if args.exclude_paths:
            body["exclude_paths"] = args.exclude_paths
        if args.exclude_domains:
            body["exclude_domains"] = args.exclude_domains
        if args.include_usage:
            body["include_usage"] = True

        client_timeout = max(cfg.timeout, float(args.timeout) + 15.0)
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=client_timeout,
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        ) as client:
            data = await http_mod.request_json(client, "POST", "crawl", provider="tavily", json_body=body)

        results = []
        for r in data.get("results", []) or []:
            item: dict[str, Any] = {"url": r.get("url"), "content": r.get("raw_content") or ""}
            if r.get("favicon"):
                item["favicon"] = r.get("favicon")
            results.append(item)
        out: dict[str, Any] = {"base_url": data.get("base_url"), "results": results}
        if data.get("response_time") is not None:
            out["response_time"] = data.get("response_time")
        return out
