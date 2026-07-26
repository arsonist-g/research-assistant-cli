"""统一错误体系（横切约定 #1，api-contract.md §4）。

错误体（stdout 固定 JSON，不受 --output 影响）：
    { "error": { "code": "...", "message": "...", "provider": "...", "details": {...} } }

退出码（语义化，跨命令一致）：
    0 成功 | 1 INTERNAL 其他 | 2 ARGS 参数错 | 3 CONFIG 配置错
    4 NETWORK 网络失败（含 PROVIDER 远端失败）| 5 ANTIBOT 反爬失败
"""

from __future__ import annotations

from typing import Any

# 退出码
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_ARGS = 2
EXIT_CONFIG = 3
EXIT_NETWORK = 4
EXIT_ANTIBOT = 5

# 错误 code 枚举 → 退出码映射
CODE_TO_EXIT: dict[str, int] = {
    "ARGS": EXIT_ARGS,
    "CONFIG": EXIT_CONFIG,
    "NETWORK": EXIT_NETWORK,
    "ANTIBOT": EXIT_ANTIBOT,
    "PROVIDER": EXIT_NETWORK,  # 远端 provider 应用层失败，归入网络类（4）
    "INTERNAL": EXIT_INTERNAL,
}


class ResearchAssistantError(Exception):
    """所有 research-assistant 业务错误的基类。

    Attributes:
        code: 错误枚举（ARGS/CONFIG/NETWORK/ANTIBOT/PROVIDER/INTERNAL）。
        message: 人类可读信息。
        provider: 出错的 provider 名（可选）。
        details: 附加结构化细节（可选，进入 error.details）。
    """

    code: str = "INTERNAL"

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.details = details

    def exit_code(self) -> int:
        return CODE_TO_EXIT.get(self.code, EXIT_INTERNAL)

    def to_json(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.provider:
            err["provider"] = self.provider
        if self.details:
            err["details"] = self.details
        return {"error": err}


class ArgsError(ResearchAssistantError):
    code = "ARGS"


class ConfigError(ResearchAssistantError):
    code = "CONFIG"


class NetworkError(ResearchAssistantError):
    code = "NETWORK"


class AntibotError(ResearchAssistantError):
    code = "ANTIBOT"


class ProviderError(ResearchAssistantError):
    """远端 provider 返回了错误响应（4xx/5xx 等），与传输层 NETWORK 区分。"""

    code = "PROVIDER"


def wrap_provider_http_error(provider: str, exc: Exception) -> ResearchAssistantError:
    """把 httpx/网络异常归一成 NetworkError / ProviderError。

    传输层失败（连接/超时/DNS/TLS）→ NETWORK；其余按 INTERNAL 兜底。
    httpx.HTTPStatusError 由各 provider 自行映射为 ProviderError（带状态码 details）。
    """
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return NetworkError(f"{provider}: 请求超时 ({exc})", provider=provider)
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return NetworkError(f"{provider}: 无法连接 ({exc})", provider=provider)
    if isinstance(exc, httpx.HTTPError):
        return NetworkError(f"{provider}: 网络错误 ({exc})", provider=provider)
    return ResearchAssistantError(f"{provider}: 内部错误 ({exc})", provider=provider)
