"""firecrawl keyless 策略：key 模式解析、可重试判定、auto fallback 调度、keyed 前置校验。

覆盖 _resolve_mode / _is_keyless_retryable / _require_keyed / _request_keyless 的分支。
_do_request 用 AsyncMock 隔离，不发网络。
"""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock

import pytest

from research_assistant.config import ProviderConfig
from research_assistant.errors import ArgsError, ProviderError
from research_assistant.providers.firecrawl import FirecrawlProvider


def _provider() -> FirecrawlProvider:
    return FirecrawlProvider(config=None)


def _cfg(key: str = "") -> ProviderConfig:
    return ProviderConfig(type="firecrawl", base_url="https://api.firecrawl.dev/v2", api_key=key)


def _args(*, keyless: bool = False, use_key: bool = False) -> argparse.Namespace:
    return argparse.Namespace(keyless=keyless, use_key=use_key)


class TestResolveMode:
    def test_default_auto(self):
        assert _provider()._resolve_mode(_args(), _cfg(key="fk")) == "auto"

    def test_keyless_flag(self):
        assert _provider()._resolve_mode(_args(keyless=True), _cfg(key="fk")) == "keyless"

    def test_use_key_flag(self):
        assert _provider()._resolve_mode(_args(use_key=True), _cfg(key="fk")) == "use_key"

    def test_use_key_without_key_raises(self):
        with pytest.raises(ArgsError):
            _provider()._resolve_mode(_args(use_key=True), _cfg(key=""))

    def test_mutex_raises(self):
        with pytest.raises(ArgsError):
            _provider()._resolve_mode(_args(keyless=True, use_key=True), _cfg(key="fk"))


class TestIsKeylessRetryable:
    @pytest.mark.parametrize("status", [403, 429])
    def test_retryable_status(self, status):
        err = ProviderError("x", provider="firecrawl", details={"status_code": status})
        assert FirecrawlProvider._is_keyless_retryable(err) is True

    def test_retryable_message_keyword(self):
        err = ProviderError("rate limit exceeded", provider="firecrawl", details={})
        assert FirecrawlProvider._is_keyless_retryable(err) is True

    def test_non_retryable_status(self):
        err = ProviderError("bad request", provider="firecrawl", details={"status_code": 400})
        assert FirecrawlProvider._is_keyless_retryable(err) is False

    def test_non_provider_error_not_retryable(self):
        assert FirecrawlProvider._is_keyless_retryable(ValueError("x")) is False


class TestRequireKeyed:
    def test_no_key_raises(self):
        with pytest.raises(ArgsError):
            _provider()._require_keyed(_cfg(key=""), "map")

    def test_with_key_passes(self):
        _provider()._require_keyed(_cfg(key="fk"), "map")  # 不抛即通过


class TestRequestKeyless:
    """_request_keyless 调度：mock _do_request 验证调用次数与 headers（key 决策）。"""

    @staticmethod
    def _provider_with_do_request(side_effects) -> FirecrawlProvider:
        p = _provider()
        p._do_request = AsyncMock(side_effect=side_effects)
        return p

    async def test_keyless_mode_no_fallback(self):
        # keyless：免 key 失败（可重试）也不带 key 重试
        p = self._provider_with_do_request(
            [ProviderError("rate limited", provider="firecrawl", details={"status_code": 429})]
        )
        with pytest.raises(ProviderError):
            await p._request_keyless(
                client=None, method="POST", path="scrape",
                cfg=_cfg(key="fk"), mode="keyless", json_body={}, endpoint="scrape",
            )
        assert p._do_request.await_count == 1
        assert p._do_request.call_args.kwargs["headers"] is None

    async def test_auto_fallback_on_retryable(self):
        # auto + 有 key：免 key 失败（可重试）→ 带 key 重试成功
        p = self._provider_with_do_request([
            ProviderError("rate limited", provider="firecrawl", details={"status_code": 429}),
            {"success": True, "data": {"markdown": "ok"}},
        ])
        result = await p._request_keyless(
            client=None, method="POST", path="scrape",
            cfg=_cfg(key="fk"), mode="auto", json_body={}, endpoint="scrape",
        )
        assert p._do_request.await_count == 2
        assert p._do_request.call_args_list[0].kwargs["headers"] is None  # 先免 key
        assert p._do_request.call_args_list[1].kwargs["headers"]["Authorization"] == "Bearer fk"  # 再带 key
        assert result["data"]["markdown"] == "ok"

    async def test_auto_no_fallback_on_non_retryable(self):
        # auto：免 key 失败（不可重试，如 400）→ 不 fallback，直接抛
        p = self._provider_with_do_request(
            [ProviderError("bad request", provider="firecrawl", details={"status_code": 400})]
        )
        with pytest.raises(ProviderError):
            await p._request_keyless(
                client=None, method="POST", path="scrape",
                cfg=_cfg(key="fk"), mode="auto", json_body={}, endpoint="scrape",
            )
        assert p._do_request.await_count == 1

    async def test_auto_no_key_no_fallback(self):
        # auto + 无 key：免 key 失败（可重试）也无法 fallback（没 key 可带）
        p = self._provider_with_do_request(
            [ProviderError("rate limited", provider="firecrawl", details={"status_code": 429})]
        )
        with pytest.raises(ProviderError):
            await p._request_keyless(
                client=None, method="POST", path="scrape",
                cfg=_cfg(key=""), mode="auto", json_body={}, endpoint="scrape",
            )
        assert p._do_request.await_count == 1

    async def test_use_key_mode_skips_keyless(self):
        # use_key：直接带 key，只调一次
        p = self._provider_with_do_request([{"success": True, "data": {}}])
        await p._request_keyless(
            client=None, method="POST", path="scrape",
            cfg=_cfg(key="fk"), mode="use_key", json_body={}, endpoint="scrape",
        )
        assert p._do_request.await_count == 1
        assert p._do_request.call_args.kwargs["headers"]["Authorization"] == "Bearer fk"

    async def test_auto_keyless_success_no_retry(self):
        # auto：免 key 成功 → 不重试，headers 为 None
        p = self._provider_with_do_request([{"success": True, "data": {"markdown": "ok"}}])
        result = await p._request_keyless(
            client=None, method="POST", path="scrape",
            cfg=_cfg(key="fk"), mode="auto", json_body={}, endpoint="scrape",
        )
        assert p._do_request.await_count == 1
        assert p._do_request.call_args.kwargs["headers"] is None
        assert result["data"]["markdown"] == "ok"
