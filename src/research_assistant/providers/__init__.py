"""providers 包：各搜索/文档 provider 的自写 HTTP 客户端（ADR-0010）。

加 provider = 在本包新增一个 .py 文件（类用 @register 装饰）+ 1 条 config。
registry.discover() 会自动发现，CLI 据此动态生成命令——无需改本文件之外的东西。
"""

from .base import ArgSpec, Capability, Provider
from .registry import all_provider_classes, register, resolve_type

__all__ = ["ArgSpec", "Capability", "Provider", "all_provider_classes", "register", "resolve_type"]
