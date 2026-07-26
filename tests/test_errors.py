"""errors 模块：错误 code → 退出码映射、错误体序列化、httpx 异常归一。"""

from __future__ import annotations

import httpx

from research_assistant import errors


class TestExitCodeMapping:
    def test_each_subclass_maps_to_expected_exit_code(self):
        assert errors.ArgsError("x").exit_code() == errors.EXIT_ARGS  # 2
        assert errors.ConfigError("x").exit_code() == errors.EXIT_CONFIG  # 3
        assert errors.NetworkError("x").exit_code() == errors.EXIT_NETWORK  # 4
        assert errors.AntibotError("x").exit_code() == errors.EXIT_ANTIBOT  # 5
        assert errors.ProviderError("x").exit_code() == errors.EXIT_NETWORK  # PROVIDER 归 4
        assert errors.ResearchAssistantError("x").exit_code() == errors.EXIT_INTERNAL  # 1

    def test_provider_error_is_network_class_for_exit(self):
        # 远端 provider 应用层失败在退出码上与传输层网络失败同类（都 4）
        assert errors.CODE_TO_EXIT["PROVIDER"] == errors.CODE_TO_EXIT["NETWORK"] == 4


class TestToJson:
    def test_minimal_error_body_has_code_and_message(self):
        body = errors.ConfigError("未配置").to_json()
        assert body == {"error": {"code": "CONFIG", "message": "未配置"}}

    def test_error_body_includes_provider_and_details_when_given(self):
        err = errors.ProviderError(
            "鉴权失败", provider="exa", details={"status_code": 401}
        )
        body = err.to_json()
        assert body["error"]["provider"] == "exa"
        assert body["error"]["details"] == {"status_code": 401}

    def test_provider_omitted_when_falsy(self):
        # provider="" / details=None 不应出现在 error 体
        body = errors.NetworkError("超时").to_json()
        assert "provider" not in body["error"]
        assert "details" not in body["error"]


class TestWrapProviderHttpError:
    def test_timeout_becomes_network_error(self):
        wrapped = errors.wrap_provider_http_error("exa", httpx.ReadTimeout("slow"))
        assert isinstance(wrapped, errors.NetworkError)
        assert wrapped.provider == "exa"

    def test_connect_error_becomes_network_error(self):
        wrapped = errors.wrap_provider_http_error("tavily", httpx.ConnectError("refused"))
        assert isinstance(wrapped, errors.NetworkError)

    def test_generic_http_error_becomes_network_error(self):
        wrapped = errors.wrap_provider_http_error("ctx7", httpx.HTTPError("generic"))
        assert isinstance(wrapped, errors.NetworkError)

    def test_unknown_exception_falls_back_to_internal(self):
        wrapped = errors.wrap_provider_http_error("exa", ValueError("weird"))
        assert wrapped.code == "INTERNAL"
        assert wrapped.provider == "exa"
