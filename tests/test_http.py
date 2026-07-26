"""http 工厂：trust_env=False、base_url 规范化、UA、错误归类、request_json。

用 httpx.MockTransport 拦截请求，不打真实网络。
"""

from __future__ import annotations

import json

import httpx
import pytest

from research_assistant import http
from research_assistant.errors import NetworkError, ProviderError


def _make_status_error(status: int, body: bytes = b"") -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://svc.example/api")
    resp = httpx.Response(status, content=body, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


class TestMakeClient:
    async def test_trust_env_disabled(self):
        # 关键：禁止 httpx 二次读 env 代理变量（否则 --proxy none 仍走系统代理）
        client = http.make_client()
        try:
            assert client.trust_env is False
        finally:
            await client.aclose()

    async def test_base_url_normalized_with_trailing_slash(self):
        client = http.make_client(base_url="http://x/api")
        try:
            assert str(client.base_url).endswith("/")
        finally:
            await client.aclose()

    async def test_user_agent_set(self):
        client = http.make_client()
        try:
            assert "research-assistant" in client.headers["user-agent"]
        finally:
            await client.aclose()

    async def test_custom_headers_merged(self):
        client = http.make_client(headers={"X-Test": "1"})
        try:
            assert client.headers["x-test"] == "1"
        finally:
            await client.aclose()

    async def test_proxy_attached(self):
        client = http.make_client(proxy_url="http://127.0.0.1:7890")
        # proxy 在 info 里（不深究内部结构，确认构造不抛异常即可）
        await client.aclose()


class TestHandleHttpError:
    def test_auth_failure_message(self):
        err = http.handle_http_error("exa", _make_status_error(401, b'{"error":"bad key"}'))
        assert isinstance(err, ProviderError)
        assert err.provider == "exa"
        assert err.details["status_code"] == 401
        assert "鉴权" in err.message

    def test_rate_limit_message(self):
        err = http.handle_http_error("exa", _make_status_error(429))
        assert "限流" in err.message

    def test_server_error_message(self):
        err = http.handle_http_error("exa", _make_status_error(502))
        assert "服务端" in err.message

    def test_extracts_nested_error_message(self):
        body = b'{"error":{"message":"invalid query param","code":"bad_query"}}'
        err = http.handle_http_error("exa", _make_status_error(400, body))
        assert "invalid query param" in err.message
        assert err.details.get("provider_code") == "bad_query"

    def test_extracts_string_error(self):
        body = b'{"error":"plan exhausted"}'
        err = http.handle_http_error("tavily", _make_status_error(402, body))
        assert "plan exhausted" in err.message

    def test_extracts_detail_field(self):
        body = b'{"detail":"not found"}'
        err = http.handle_http_error("ctx7", _make_status_error(404, body))
        assert "not found" in err.message

    def test_non_json_body_put_in_details(self):
        err = http.handle_http_error("exa", _make_status_error(500, b"<html>oops</html>"))
        assert err.details.get("body") == "<html>oops</html>"


class TestRequestJson:
    async def test_returns_parsed_json_on_success(self):
        def handler(request):
            return httpx.Response(200, json={"ok": True, "n": 1})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        data = await http.request_json(client, "GET", "http://x/api", provider="exa")
        assert data == {"ok": True, "n": 1}
        await client.aclose()

    async def test_raises_provider_error_on_4xx(self):
        def handler(request):
            return httpx.Response(403, content=b'{"error":"forbidden"}')

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(ProviderError) as ei:
            await http.request_json(client, "GET", "http://x/api", provider="exa")
        assert ei.value.details["status_code"] == 403
        await client.aclose()

    async def test_raises_network_error_on_connection_failure(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(NetworkError):
            await http.request_json(client, "GET", "http://x/api", provider="exa")
        await client.aclose()

    async def test_passes_params_and_json_body(self):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await http.request_json(
            client, "POST", "http://x/api", provider="exa",
            json_body={"q": "hi"}, params={"num": "3"},
        )
        assert seen["params"] == {"num": "3"}
        assert seen["body"] == {"q": "hi"}
        await client.aclose()
