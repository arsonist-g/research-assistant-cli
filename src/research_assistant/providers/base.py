"""Provider 统一接口（ADR-0011 插件化解耦的核心）。

每个 provider 声明：
    type        —— config 中的 type 键（如 "exa"）
    command     —— 主命令名（如 "exa"）
    aliases     —— 命令别名（如 ["x"]）
    capabilities() —— 返回 Capability 列表（子命令 → async 处理函数 + 参数声明）

加/删/换 provider = 1 文件 + 1 条 config，不改 CLI 入口/路由/横切/其他 provider。
能力差异用 capabilities{} 声明式表达，CLI 按声明动态生成命令（不强制统一方法签名）。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config


def bounded_int(lo: int, hi: int) -> Callable[[str], int]:
    """argparse type 工厂：解析时校验 int 且 lo<=v<=hi，越界抛 ArgumentTypeError。

    声明在 ArgSpec.type，与 choices 同层（都在 argparse 解析时校验），错误走统一的
    argparse error → stdout JSON 通道（cli._JSONArgumentParser.error 接管）。
    """

    def _convert(raw: str) -> int:
        try:
            v = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"需为整数（得到 {raw!r}）")
        if not (lo <= v <= hi):
            raise argparse.ArgumentTypeError(f"需在 {lo}-{hi} 之间（得到 {v}）")
        return v

    return _convert


@dataclass
class ArgSpec:
    """单个 CLI 参数声明（透传到 argparse.add_argument）。

    kind="positional" 时 name_or_flags 给单个位置参数名（如 ["query"]）；
    kind="optional" 时给 flag（如 ["--num-results"]）。
    """

    name_or_flags: list[str]
    help: str = ""
    default: Any = None
    required: bool = False
    type: Callable[[str], Any] | None = None
    choices: list[str] | None = None
    nargs: str | int | None = None
    action: str | None = None
    dest: str | None = None
    metavar: str | None = None
    kind: str = "optional"

    def add_to(self, parser: Any) -> None:
        kwargs: dict[str, Any] = {"help": self.help}
        if self.kind == "optional":
            if self.default is not None:
                kwargs["default"] = self.default
            if self.required:
                kwargs["required"] = True
        if self.type is not None:
            kwargs["type"] = self.type
        if self.choices is not None:
            kwargs["choices"] = self.choices
        if self.nargs is not None:
            kwargs["nargs"] = self.nargs
        if self.action is not None:
            kwargs["action"] = self.action
        if self.dest is not None:
            kwargs["dest"] = self.dest
        if self.metavar is not None:
            kwargs["metavar"] = self.metavar
        parser.add_argument(*self.name_or_flags, **kwargs)


@dataclass
class Capability:
    """一个子命令（如 exa search）：参数声明 + async 处理函数。

    handler 签名：async (provider_instance, argparse.Namespace) -> dict
    返回 dict 直接作为 JSON 输出（对齐 api-contract.md §3）。
    """

    name: str
    help: str = ""
    args: list[ArgSpec] = field(default_factory=list)
    handler: Callable[..., Any] | None = None  # async callable


class Provider:
    """所有 provider 的基类。子类设置类属性 + 实现 capabilities() 与各 handler。"""

    type: str = ""
    command: str = ""
    aliases: list[str] = []
    help: str = ""

    def __init__(self, config: Config) -> None:
        self.config = config

    def capabilities(self) -> list[Capability]:
        raise NotImplementedError

    def client_kwargs(self) -> dict[str, Any]:
        """子类可覆盖：构造 httpx 客户端时的额外 header/base_url。"""
        return {}
