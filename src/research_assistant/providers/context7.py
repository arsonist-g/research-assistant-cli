"""Context7 provider（自写 HTTP，ADR-0010；Context7 无 Python SDK 本就需自写）。

公共 REST API（实测确认，context7.com）：
    GET https://context7.com/api/v1/search?libraryName=<name>&query=<question>
        鉴权 Authorization: Bearer <key>
        resp: {results[]{id"/org/repo", title, description, totalSnippets, trustScore, ...}}
    GET https://context7.com/api/v1/<libraryId>?topic=<query>&tokens=<n>&format=markdown
        注：libraryId 在路径里（catch-all 路由），需去掉前导 /
        resp: text/markdown，块间以 '--------------------------------' 分隔，每块含 'Source: <url>'

对齐 api-contract.md §2.1/§3：
    ctx7 library → {results[]{id, name, description}}
    ctx7 docs    → {id, contents[]{text, source_url?}}
"""

from __future__ import annotations

import httpx
import re
from typing import Any

from ..config import ProviderConfig
from ..errors import ArgsError, ProviderError
from .. import http as http_mod
from ..proxy import resolve_proxy
from .base import ArgSpec, Capability, Provider
from .registry import register

DEFAULT_BASE_URL = "https://context7.com"
_SEPARATOR = re.compile(r"\n-{10,}\n")


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
        return cfg

    async def library(self, args: Any) -> dict[str, Any]:
        cfg = self._cfg()
        params: dict[str, str] = {"libraryName": args.name}
        if args.query:
            params["query"] = args.query
        else:
            params["query"] = args.name
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        ) as client:
            data = await http_mod.request_json(
                client, "GET", f"{cfg.base_url}/api/v1/search", provider="context7", params=params
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
        params = {"topic": args.query, "tokens": str(args.tokens), "format": "markdown"}
        async with http_mod.make_client(
            proxy_url=resolve_proxy(self.config.proxy.url),
            timeout=cfg.timeout,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        ) as client:
            text, final_id = await _fetch_docs(client, cfg.base_url, lib_id, params)
        contents = _parse_docs_text(text)
        return {"id": final_id, "contents": contents}


async def _fetch_docs(
    client: httpx.AsyncClient, base_url: str, lib_id: str, params: dict[str, str], _depth: int = 0
) -> tuple[str, str]:
    """取 docs 文本；Context7 对旧 id 会 404 重定向，自动跟随（最多 3 次）。"""
    if _depth > 3:
        raise ProviderError("context7: libraryId 重定向次数过多", provider="context7")
    path_id = lib_id.lstrip("/")
    try:
        resp = await client.request("GET", f"{base_url}/api/v1/{path_id}", params=params)
    except Exception as e:
        from ..errors import wrap_provider_http_error

        raise wrap_provider_http_error("context7", e) from e
    if resp.status_code == 404:
        # 解析重定向提示："Library X has been redirected to this library: /new/id."
        import re

        m = re.search(r"redirected to this library:\s*(/[^\s.]+)", resp.text)
        if m:
            new_id = m.group(1)
            return await _fetch_docs(client, base_url, new_id, params, _depth + 1)
    if resp.status_code >= 400:
        raise http_mod.handle_http_error(
            "context7",
            httpx.HTTPStatusError(f"context7 HTTP {resp.status_code}", request=resp.request, response=resp),
        )
    return resp.text, lib_id


def _parse_docs_text(text: str) -> list[dict[str, Any]]:
    """把 Context7 markdown 文本拆成 contents[]{text, source_url?}。

    块间以一行短横线分隔；每块若含 'Source: <url>' 行，抽出为 source_url。
    """
    if not text or not text.strip():
        return []
    blocks = _SEPARATOR.split(text)
    out: list[dict[str, Any]] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        source_url = None
        lines = block.splitlines()
        # 找 Source: 行
        clean_lines: list[str] = []
        for line in lines:
            m = re.match(r"\s*Source:\s*(\S+)", line)
            if m and source_url is None:
                source_url = m.group(1)
                continue  # 去掉 Source 行本身
            clean_lines.append(line)
        item: dict[str, Any] = {"text": "\n".join(clean_lines).strip()}
        if source_url:
            item["source_url"] = source_url
        out.append(item)
    return out
