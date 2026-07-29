"""context7 provider：base_url 归一化 + v2 endpoint 构造 + docs JSON 解析。

mock 掉 http.make_client 注入 MockTransport，断言请求 URL/params（不发网络）。
核心回归：网关场景 path 不含 /api/v2（不再双重拼接 404）。
"""

from __future__ import annotations

import argparse

import httpx
import pytest

from research_assistant import http as ra_http
from research_assistant.config import Config, ProviderConfig
from research_assistant.errors import ArgsError
from research_assistant.providers.context7 import Context7Provider, _parse_docs_json, _resolve_base

# 测试用占位地址/凭据：官方根为公开地址可真实；网关根与 key 一律占位，勿填真实值。
_DIRECT_BASE = "https://context7.com"  # context7 官方根（公开）
_GATEWAY_BASE = "https://gateway.example.com/context7"  # 网关示例根（占位）
_FAKE_KEY = "ctx7sk-test"  # 占位 key


def _provider(base_url: str = _DIRECT_BASE) -> Context7Provider:
    cfg = Config(providers=[ProviderConfig(type="context7", base_url=base_url, api_key=_FAKE_KEY)])
    return Context7Provider(config=cfg)


def _patch_client(monkeypatch, handler):
    """让 context7 的 make_client 返回走 MockTransport 的 client，透传 headers，handler 捕获请求。"""

    def fake_make_client(*, headers=None, **_kw):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=headers or {})

    monkeypatch.setattr(ra_http, "make_client", fake_make_client)


class TestResolveBase:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("https://context7.com", "https://context7.com/api/v2"),
            ("https://context7.com/", "https://context7.com/api/v2"),
            ("https://context7.com/api/v2", "https://context7.com/api/v2"),
            ("https://context7.com/api/v1", "https://context7.com/api/v1"),
            ("https://gateway.example.com/context7/", "https://gateway.example.com/context7"),
            ("https://gateway.example.com/context7", "https://gateway.example.com/context7"),
            ("", "https://context7.com/api/v2"),
            ("  https://context7.com  ", "https://context7.com/api/v2"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert _resolve_base(raw) == expected


class TestParseDocsJson:
    def test_maps_code_and_info_snippets(self):
        data = {
            "codeSnippets": [
                {
                    "codeTitle": "Example",
                    "codeId": "https://github.com/o/r/blob/a/b.ts#L1",
                    "codeList": [{"code": "const x = 1"}, {"code": "const y = 2"}],
                }
            ],
            "infoSnippets": [{"pageId": "https://docs.example/page", "content": "Some narrative docs"}],
        }
        assert _parse_docs_json(data) == [
            {"text": "Example\nconst x = 1\nconst y = 2", "source_url": "https://github.com/o/r/blob/a/b.ts#L1"},
            {"text": "Some narrative docs", "source_url": "https://docs.example/page"},
        ]

    def test_snippet_without_source_url_omits_field(self):
        assert _parse_docs_json({"infoSnippets": [{"content": "no url here"}]}) == [{"text": "no url here"}]

    def test_empty_and_non_dict_safe(self):
        assert _parse_docs_json({}) == []
        assert _parse_docs_json(None) == []
        assert _parse_docs_json("not a dict") == []

    def test_skips_empty_snippets(self):
        assert _parse_docs_json({"codeSnippets": [{"codeList": []}], "infoSnippets": [{"content": ""}]}) == []


class TestLibraryEndpoint:
    async def test_direct_appends_api_v2(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={"results": [{"id": "/facebook/react", "title": "React", "description": "d"}]},
            )

        _patch_client(monkeypatch, handler)

        result = await _provider(_DIRECT_BASE).library(argparse.Namespace(name="react", query="hooks"))
        assert seen["path"] == "/api/v2/libs/search"
        assert seen["params"] == {"libraryName": "react", "query": "hooks"}
        assert seen["auth"] == f"Bearer {_FAKE_KEY}"
        assert result == {"results": [{"id": "/facebook/react", "name": "React", "description": "d"}]}

    async def test_gateway_no_double_prefix(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"results": []})

        _patch_client(monkeypatch, handler)

        await _provider(_GATEWAY_BASE + "/").library(argparse.Namespace(name="react", query=None))
        # 网关根原样拼接：path 不含 /api/v2，否则上游会 .../api/v2/context7/... 双重前缀
        assert seen["path"] == "/context7/libs/search"
        assert seen["params"] == {"libraryName": "react", "query": "react"}  # query 缺省回退到 name


class TestDocsEndpoint:
    async def test_v2_context_endpoint_and_parse(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                json={
                    "codeSnippets": [{"codeTitle": "T", "codeId": "https://src/x", "codeList": [{"code": "c"}]}],
                    "infoSnippets": [{"pageId": "https://p", "content": "narr"}],
                },
            )

        _patch_client(monkeypatch, handler)

        result = await _provider(_GATEWAY_BASE).docs(
            argparse.Namespace(library_id="/facebook/react", query="useState", tokens=5000)
        )
        assert seen["path"] == "/context7/context"
        assert seen["params"] == {
            "libraryId": "/facebook/react",
            "query": "useState",
            "type": "json",
            "tokens": "5000",
        }
        assert result == {
            "id": "/facebook/react",
            "contents": [
                {"text": "T\nc", "source_url": "https://src/x"},
                {"text": "narr", "source_url": "https://p"},
            ],
        }

    async def test_library_id_must_start_with_slash(self):
        with pytest.raises(ArgsError):
            await _provider().docs(argparse.Namespace(library_id="facebook/react", query="x", tokens=5000))
