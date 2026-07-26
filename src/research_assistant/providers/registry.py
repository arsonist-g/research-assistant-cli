"""Provider 注册表（ADR-0011）。

- @register 装饰器：provider 模块定义类时自动登记 type → class。
- discover()：自动扫描 providers/ 包下所有模块（import 触发 @register），
  无需在 __init__.py 显式列举——加 provider 文件即被发现。
- 映射：type → Provider class；command/alias → type。
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import Provider

_REGISTRY: dict[str, type["Provider"]] = {}
# command/alias → type（多对一）
_COMMAND_INDEX: dict[str, str] = {}
# discover 是否已跑过。用独立标志而非"依赖 _REGISTRY 非空"判断——否则任一 provider
# 被其他模块预先 import（触发其 @register，使 _REGISTRY 非空）后，完整扫描会被跳过，
# 导致 registry 只含那个 provider（locate engine 即会预先 import openai_compat）。
_discovered: bool = False


def register(cls: type["Provider"]) -> type["Provider"]:
    """类装饰器：把 Provider 子类按 type 登记，并建立 command/alias 索引。"""
    if not cls.type:
        raise ValueError(f"{cls.__name__} 缺少 type 属性")
    _REGISTRY[cls.type] = cls
    if cls.command:
        _COMMAND_INDEX[cls.command] = cls.type
    for alias in cls.aliases or []:
        _COMMAND_INDEX[alias] = cls.type
    return cls


def _discover() -> None:
    """导入 providers 包下所有模块（触发各模块顶层的 @register）。仅执行一次。"""
    global _discovered
    if _discovered:
        return
    _discovered = True
    pkg = importlib.import_module(__package__)
    path = getattr(pkg, "__path__", None)
    if path is None:  # pragma: no cover
        return
    for _finder, name, _is_pkg in pkgutil.iter_modules(path):
        if name in ("base", "registry"):
            continue
        importlib.import_module(f"{__package__}.{name}")


def all_provider_classes() -> dict[str, type["Provider"]]:
    _discover()
    return dict(_REGISTRY)


def resolve_type(command_or_alias: str) -> str | None:
    _discover()
    return _COMMAND_INDEX.get(command_or_alias)
