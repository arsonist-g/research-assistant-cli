"""规则 transport="http"：直接 HTTP 取数（不启浏览器）。

适合公开的 JSON / HTML 数据接口。带登录态时按源 host 过滤 cookie（不跨域外发）。
任何失败返回 None，由 fetch 的普通层继续兜底。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from ..config import Config
from ..http import make_client
from ..proxy import resolve_proxy
from ..site_rules import RuleMatch, max_response_bytes
from .html2md import html_to_md

logger = logging.getLogger("research_assistant.fetch.rule_http")


def _cookie_header(cookies: list[dict[str, Any]], host: str) -> str:
    pairs: list[str] = []
    for c in cookies:
        domain = str(c.get("domain") or "").lstrip(".").lower()
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            name = c.get("name")
            value = c.get("value")
            if name:
                pairs.append(f"{name}={value}")
    return "; ".join(pairs)


async def fetch_rule_http(config: Config, match: RuleMatch, *, login: bool = True) -> str | None:
    """按规则取 http 数据接口并转 markdown；失败返回 None。"""
    rule = match.rule
    is_json = rule.parser in ("discourse_topic", "json_path")
    headers: dict[str, str] = {
        "Accept": "application/json, text/plain, */*" if is_json
        else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    headers.update(rule.headers)

    if login and rule.needs_login and "Cookie" not in headers:
        try:
            from ..loginstate import get_login_state

            raw, _storage = await get_login_state(config)
            cookie = _cookie_header(raw, (urlparse(match.request_url).hostname or "").lower())
            if cookie:
                headers["Cookie"] = cookie
        except Exception as e:
            logger.debug("规则 %s 取登录 cookie 失败（按公开请求继续）: %s", rule.id, e)

    try:
        async with make_client(proxy_url=resolve_proxy(config.proxy.url), timeout=30.0) as client:
            resp = await client.get(match.request_url, headers=headers)
            if resp.status_code >= 400:
                logger.warning("规则 %s HTTP %s: %s", rule.id, resp.status_code, match.request_url)
                return None
            body = resp.text
    except Exception as e:
        logger.warning("规则 %s HTTP 请求失败 %s: %s", rule.id, match.request_url, e)
        return None

    if len(body.encode("utf-8", errors="replace")) > max_response_bytes():
        logger.warning("规则 %s 响应超过大小上限，放弃: %s", rule.id, match.request_url)
        return None

    if rule.parser:
        return rule.parse(body, match)
    return html_to_md(rule.preprocess_html(body, match), match.source_url) or None
