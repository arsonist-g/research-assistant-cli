"""普通抓取：复用已集成的 provider 做直接出 md 的抓取（D4）。

优先 tavily extract（quality 好）；不可用/失败再用 firecrawl scrape。
两者都不可用或失败 → 返回 None，由调用方回退浏览器（ADR-0004）。

fmt 由 fetch 聚合层透传：tavily extract 只认 markdown|text，firecrawl scrape 只认
markdown|html；不认的格式降级为 markdown（避免向 API 传非法值）。
"""

from __future__ import annotations

import argparse
from typing import Any

from .. import providers as providers_pkg
from ..config import Config


async def fetch_normal(config: Config, url: str, fmt: str = "markdown") -> str | None:
    """对一个 URL 做普通抓取，返回正文或 None。fmt 透传给各 provider（不认的降级 markdown）。"""
    classes = providers_pkg.all_provider_classes()

    # 1) tavily extract
    if config.provider("tavily"):
        md = await _try_tavily_extract(classes, config, url, fmt)
        if md:
            return md

    # 2) firecrawl scrape
    if config.provider("firecrawl"):
        md = await _try_firecrawl_scrape(classes, config, url, fmt)
        if md:
            return md

    return None


async def _try_tavily_extract(classes: dict, config: Config, url: str, fmt: str) -> str | None:
    try:
        provider = classes["tavily"](config)
        cap = next((c for c in provider.capabilities() if c.name == "extract"), None)
        if cap is None:
            return None
        # tavily extract 只认 markdown|text；html 等降级 markdown
        tavily_fmt = fmt if fmt in ("markdown", "text") else "markdown"
        ns = argparse.Namespace(urls=[url], extract_depth="advanced", format=tavily_fmt)
        result = await cap.handler(ns)
        results = result.get("results") or []
        if results:
            content = results[0].get("content")
            if content and content.strip():
                return content
        return None
    except Exception:
        return None


async def _try_firecrawl_scrape(classes: dict, config: Config, url: str, fmt: str) -> str | None:
    try:
        provider = classes["firecrawl"](config)
        cap = next((c for c in provider.capabilities() if c.name == "scrape"), None)
        if cap is None:
            return None
        # firecrawl scrape 只认 markdown|html；text 等降级 markdown
        fc_fmt = fmt if fmt in ("markdown", "html") else "markdown"
        ns = argparse.Namespace(urls=[url], format=fc_fmt, only_main_content=True, wait_for=None)
        result = await cap.handler(ns)
        data = result.get("data") or {}
        content = data.get("html") if fc_fmt == "html" else data.get("markdown")
        if content and content.strip():
            return content
        return None
    except Exception:
        return None
