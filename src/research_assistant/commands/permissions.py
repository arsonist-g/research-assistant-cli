"""permissions 命令：把 CLI 命令放行规则装进各平台免审配置（api-contract §2.3 Delta）。

    research-assistant permissions status  [--targets <list>] [--root <path>]
    research-assistant permissions install [--targets <list>] [--root <path>]

status 报告每平台规则 present/missing（含无机制平台的指引）；install 幂等追加缺失规则。
默认目标：status = 全部（claude,cursor,gemini,codex,hermes）；install = 可写三家（claude,cursor,gemini）。
与 skills 命令解耦：放行目标 ≠ skill 四家，install 不随 skills update 自动执行（用户显式跑）。
"""

from __future__ import annotations

import argparse
from typing import Any

from .. import permissions as permissions_lib
from ..config import Config
from ..errors import ArgsError

NAME = "permissions"
ALIASES: list[str] = ["permission", "allow"]
HELP = "Install command allow rules into AI agent platforms (skip approval prompts)."

_ALL = ",".join(permissions_lib.ALL_PERMISSION_TARGETS)
_WRITABLE = ",".join(permissions_lib.WRITABLE_TARGETS)


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(NAME, aliases=ALIASES, help=HELP, description=HELP)
    sub = p.add_subparsers(dest="subcommand", required=True, metavar="<status|install>")
    st = sub.add_parser("status", help="Check allow-rule presence per platform.")
    st.add_argument("--targets", help=f"Comma list (default all: {_ALL}).")
    st.add_argument("--root", help="Override install root (default $HOME).")
    st.set_defaults(_handler=run_status)
    inst = sub.add_parser("install", help="Idempotently append missing allow rules.")
    inst.add_argument("--targets", help=f"Comma list (default writable: {_WRITABLE}; status sees all: {_ALL}).")
    inst.add_argument("--root", help="Override install root (default $HOME).")
    inst.set_defaults(_handler=run_install)


def _resolve_targets(raw: str | None, default: list[str]) -> list[str]:
    if not raw:
        return default
    tokens = [t.strip().lower() for t in raw.replace(";", ",").split(",") if t.strip()]
    if not tokens:
        return default
    if "all" in tokens:
        return list(permissions_lib.ALL_PERMISSION_TARGETS)
    out: list[str] = []
    for t in tokens:
        if t not in permissions_lib.ALL_PERMISSION_TARGETS:
            raise ArgsError(f"permissions: 未知目标 '{t}'（可选: {_ALL}）")
        out.append(t)
    return out


async def run_status(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    targets = _resolve_targets(args.targets, list(permissions_lib.ALL_PERMISSION_TARGETS))
    return permissions_lib.status(targets, root=args.root)


async def run_install(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    targets = _resolve_targets(args.targets, list(permissions_lib.WRITABLE_TARGETS))
    return permissions_lib.install(targets, root=args.root)
