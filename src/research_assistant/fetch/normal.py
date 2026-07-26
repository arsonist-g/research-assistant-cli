"""普通抓取：复用已集成的 provider 做直接出 md 的抓取（D4）。

优先 tavily extract（quality 好）；不可用/失败再用 firecrawl scrape。
两者都不可用或失败 → 返回 None，由调用方回退浏览器（ADR-0004）。
"""

from __future__ import annotations

import argparse
from typing import Any

from .. import providers as providers_pkg
from ..config import Config


async def fetch_normal(config: Config, url: str) -> str | None:
    """对一个 URL 做普通抓取，返回 markdown 或 None。"""
    classes = providers_pkg.all_provider_classes()

    # 1) tavily extract
    if config.provider("tavily"):
        md = await _try_tavily_extract(classes, config, url)
        if md:
            return md

    # 2) firecrawl scrape
    if config.provider("firecrawl"):
        md = await _try_firecrawl_scrape(classes, config, url)
        if md:
            return md

    return None


async def _try_tavily_extract(classes: dict, config: Config, url: str) -> str | None:
    try:
        provider = classes["tavily"](config)
        cap = next((c for c in provider.capabilities() if c.name == "extract"), None)
        if cap is None:
            return None
        ns = argparse.Namespace(urls=[url], extract_depth="advanced", format="markdown")
        result = await cap.handler(ns)
        results = result.get("results") or []
        if results:
            content = results[0].get("content")
            if content and content.strip():
                return content
        return None
    except Exception:
        return None


async def _try_firecrawl_scrape(classes: dict, config: Config, url: str) -> str | None:
    try:
        provider = classes["firecrawl"](config)
        cap = next((c for c in provider.capabilities() if c.name == "scrape"), None)
        if cap is None:
            return None
        ns = argparse.Namespace(
            urls=[url], format="markdown", only_main_content=True, wait_for=None
        )
        result = await cap.handler(ns)
        data = result.get("data") or {}
        md = data.get("markdown")
        if md and md.strip():
            return md
        return None
    except Exception:
        return None
