"""Context7 provider（自写 HTTP，ADR-0010；Context7 无 Python SDK 本就需自写）。

公共 REST API v2（官方 api-guide.mdx / openapi.json，context7.com）：
    GET {base}/libs/search?libraryName=<name>&query=<question>
        鉴权 Authorization: Bearer <key>
        resp: {results[]{id"/org/repo", title, description, totalSnippets, ...}}
    GET {base}/context?libraryId=<id>&query=<q>&type=json&tokens=<n>
        resp: {codeSnippets[]{codeTitle, codeId, codeList[]{code}, pageTitle, ...},
               infoSnippets[]{pageId, breadcrumb, content, contentTokens}}
        libraryId 迁移时 301 + JSON {redirectUrl: "/new/id"}（httpx follow_redirects 自动跟随）

base_url 归一化（_resolve_base）—— 直连和走网关都无需手动补版本前缀，避免网关上游
（已含 /api/v2）把 /api/v1 当 endpoint 双重拼接成 404：
    直连裸域名 https://context7.com            → 自动补 /api/v2
    网关根 https://<gateway>/context7          → 原样，只拼相对路径
    显式带版本前缀 .../api/v2                  → 原样

对齐 api-contract.md §2.1/§3：
    ctx7 library → {results[]{id, name, description}}
    ctx7 docs    → {id, contents[]{text, source_url?}}
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from ..config import ProviderConfig
from ..errors import ArgsError
from .. import http as http_mod
from ..proxy import resolve_proxy
from .base import ArgSpec, Capability, Provider
from .registry import register

DEFAULT_BASE_URL = "https://context7.com"
_API_VERSION = "api/v2"


def _resolve_base(base_url: str) -> str:
    """归一化 context7 base_url，返回拼相对路径（/libs/search、/context）用的根。

    - 已显式带 /api/vN → 原样（支持显式填 https://context7.com/api/v2）
    - 指向 context7.com 但无版本前缀（直连裸域名）→ 自动补 /api/v2
    - 其他（网关根，host ≠ context7.com）→ 原样，只拼相对路径（网关上游自带 /api/v2）
    """
    base = base_url.strip().rstrip("/")
    if not base:
        return f"https://context7.com/{_API_VERSION}"
    if re.search(r"/api/v\d+$", base):
        return base
    if urlparse(base).netloc == "context7.com":
        return f"{base}/{_API_VERSION}"
    return base


@register
class Context7Provider(Provider):
    type = "context7"
    command = "ctx7"
    aliases = ["c7"]
    help = "Context7 library resolution and up-to-date docs retrieval."

    def capabilities(self) -> list[Capability]:
        return [
            Capability(
                name="library",
                help="Resolve a library ID by name.",
                args=[
                    ArgSpec(["name"], kind="positional", help="Library name (e.g. 'Next.js')."),
                    ArgSpec(["query"], kind="positional", nargs="?", help="Your question (for relevance ranking)."),
                ],
                handler=self.library,
            ),
            Capability(
                name="docs",
                help="Fetch up-to-date docs for a library ID (must start with '/').",
                args=[
                    ArgSpec(["library_id"], kind="positional", metavar="LIBRARY_ID", help="Library ID, e.g. /facebook/react."),
                    ArgSpec(["query"], kind="positional", help="Topic / question."),
                    ArgSpec(["--tokens"], type=int, default=5000, help="Max tokens to return (default 5000)."),
                ],
                handler=self.docs,
            ),
        ]

    def _cfg(self) -> ProviderConfig:
        cfg = self.config.require_provider("context7")
        if not cfg.api_key:
            raise ArgsError("context7: 缺少 api_key", provider="context7")
        if not cfg.base_url:
            cfg.base_url = DEFAULT_BASE_URL
        # 归一化：直连裸域名自动补 /api/v2，网关根原样，避免上游路径前缀重复
        cfg.base_url = _resolve_base(cfg.base_url)
        return cfg

    async def library(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        params: dict[str, str] = {"libraryName": args.name}
        params["query"] = args.query or args.name
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        ) as client:
            data = await http_mod.request_json(
                client, "GET", f"{cfg.base_url}/libs/search", provider="context7", params=params
            )
        results = []
        for r in data.get("results", []) or []:
            results.append(
                {
                    "id": r.get("id"),
                    "name": r.get("title"),
                    "description": r.get("description"),
                }
            )
        return {"results": results}

    async def docs(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        lib_id = args.library_id
        if not lib_id.startswith("/"):
            raise ArgsError(
                "context7 docs: libraryId 必须以 '/' 开头（如 /facebook/react）", provider="context7"
            )
        params = {"libraryId": lib_id, "query": args.query, "type": "json", "tokens": str(args.tokens)}
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        ) as client:
            data = await http_mod.request_json(
                client, "GET", f"{cfg.base_url}/context", provider="context7", params=params
            )
        contents = _parse_docs_json(data)
        return {"id": lib_id, "contents": contents}


def _parse_docs_json(data: Any) -> list[dict[str, Any]]:
    """把 Context7 v2 context 响应拆成 contents[]{text, source_url?}。

    codeSnippets：codeTitle + 各 codeList[].code 拼成 text；codeId 作 source_url。
    infoSnippets：content 作 text；pageId 作 source_url。
    """
    if not isinstance(data, dict):
        return []
    out: list[dict[str, Any]] = []
    for snip in data.get("codeSnippets", []) or []:
        if not isinstance(snip, dict):
            continue
        parts: list[str] = []
        title = snip.get("codeTitle")
        if title:
            parts.append(str(title))
        for ex in snip.get("codeList", []) or []:
            if isinstance(ex, dict) and ex.get("code"):
                parts.append(str(ex["code"]))
        text = "\n".join(parts).strip()
        if not text:
            continue
        item: dict[str, Any] = {"text": text}
        if snip.get("codeId"):
            item["source_url"] = str(snip["codeId"])
        out.append(item)
    for snip in data.get("infoSnippets", []) or []:
        if not isinstance(snip, dict):
            continue
        text = str(snip.get("content") or "").strip()
        if not text:
            continue
        item = {"text": text}
        if snip.get("pageId"):
            item["source_url"] = str(snip["pageId"])
        out.append(item)
    return out
