"""CLI 入口：全局 flag 预解析 + 动态命令路由 + 统一分发/错误处理。

命令树（api-contract.md §2）：
    research-assistant [全局flags] <tool|cmd> <subcommand> [args] [flags]

  provider 命令（从 registry 动态生成，ADR-0011）：
    ctx7/exa/tavily/firecrawl + openai_compat 的各自子命令
  research-assistant 增值命令：fetch / locate / search
  管理命令：setup / skills / doctor

全局 flags 可出现在任意位置（预解析抽离）：
    --config <path>  --output json|markdown  --proxy <url>  --verbose  --version
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Sequence

from . import __version__
from .config import load as load_config
from .errors import ArgsError, ResearchAssistantError
from .output import emit, emit_error, emit_json
from .providers import all_provider_classes
from . import proxy as proxy_mod


# ---------------------------------------------------------------------------
# 命令分发 handler 契约：async(args_ns, config) -> dict
# ---------------------------------------------------------------------------


def _wrap_provider_handler(bound_handler: Any) -> Any:
    """把 provider 的 bound handler(self 已绑定) 适配成 (args_ns, config) 契约。"""

    async def runner(args_ns: argparse.Namespace, config: Any) -> Any:
        return await bound_handler(args_ns)

    return runner


def _build_parser(config: Any) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-assistant",
        description="搜索调研子 Agent + 可并发的搜索/爬取/定位 CLI 原子工具。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subs = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # ---- provider 命令（动态，从 registry 生成）----
    for ptype, pclass in sorted(all_provider_classes().items()):
        provider = pclass(config)
        cap_list = provider.capabilities()
        if not cap_list:
            continue
        names = [provider.command] + list(provider.aliases or [])
        names = [n for n in names if n]
        if not names:
            continue
        p_parser = subs.add_parser(
            names[0],
            aliases=names[1:],
            help=pclass.help,
            description=pclass.help,
        )
        p_sub = p_parser.add_subparsers(dest="subcommand", required=True, metavar="<subcommand>")
        for cap in cap_list:
            c_parser = p_sub.add_parser(cap.name, help=cap.help, description=cap.help)
            for arg in cap.args:
                arg.add_to(c_parser)
            c_parser.set_defaults(_handler=_wrap_provider_handler(cap.handler))

    # ---- 原生命令 ----
    from .commands import ask as ask_cmd
    from .commands import fetch as fetch_cmd
    from .commands import locate as locate_cmd
    from .commands import search as search_cmd
    from .commands import setup as setup_cmd
    from .commands import skills as skills_cmd
    from .commands import doctor as doctor_cmd

    for cmd in (fetch_cmd, locate_cmd, search_cmd, ask_cmd, setup_cmd, skills_cmd, doctor_cmd):
        cmd.register(subs)

    return parser


def _parse_globals(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    """预解析全局 flags（允许出现在命令任意位置），返回 (globals_ns, remaining_argv)。"""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None, dest="config_path")
    pre.add_argument("--output", default="json", choices=["json", "markdown"])
    pre.add_argument("--proxy", default=None, help="Proxy URL override (横切); 'none' = force direct, no proxy.")
    pre.add_argument("--verbose", action="store_true")
    pre.add_argument("--version", action="store_true")
    ns, remaining = pre.parse_known_args(list(argv))
    return ns, remaining


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    g_ns, remaining = _parse_globals(argv)

    if g_ns.version:
        print(f"research-assistant {__version__}")
        return 0

    # 代理横切覆盖（CLI --proxy 优先级最高，ADR-0007）
    proxy_mod.set_override(g_ns.proxy)

    # 加载配置（文件不存在=首次使用，返回空 Config）
    try:
        config = load_config(g_ns.config_path)
    except ResearchAssistantError as e:
        emit_json(e.to_json())
        emit_error(e.message)
        return e.exit_code()

    # 构建并解析子命令树
    parser = _build_parser(config)
    try:
        args_ns = parser.parse_args(remaining)
    except SystemExit as e:  # argparse 解析失败/--help 已退出
        code = e.code if isinstance(e.code, int) else 2
        return code

    handler = getattr(args_ns, "_handler", None)
    if handler is None:  # 只有顶层命令没有 sub（理论上 required=True 已挡）
        parser.print_help(sys.stderr)
        return 2

    try:
        result = asyncio.run(handler(args_ns, config))
    except ResearchAssistantError as e:
        emit_json(e.to_json())
        emit_error(e.message)
        return e.exit_code()
    except KeyboardInterrupt:
        emit_error("已中断")
        return 1
    except Exception as e:  # 兜底：未预期异常 → INTERNAL
        err = ResearchAssistantError(f"内部错误: {e}", details={"type": type(e).__name__})
        emit_json(err.to_json())
        emit_error(err.message)
        return err.exit_code()

    emit(result, g_ns.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
