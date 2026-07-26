"""skills 命令：managed skill/agent 的同步（api-contract §2.3）。

    research-assistant skills status [--targets list]
    research-assistant skills update  [--targets list] [--skills-root path]

status 判定 missing/stale/up-to-date/extra；update 刷新 managed 文件。
"""

from __future__ import annotations

import argparse
from typing import Any

from .. import installer
from ..config import Config
from ..errors import ArgsError
from ..targets import ALL_FAMILIES

NAME = "skills"
ALIASES: list[str] = ["skill"]
HELP = "Manage managed skill/agent files across AI agent families."


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(NAME, aliases=ALIASES, help=HELP, description=HELP)
    sub = p.add_subparsers(dest="subcommand", required=True, metavar="<status|update>")
    st = sub.add_parser("status", help="Check managed file freshness.")
    st.add_argument("--targets", help=f"Comma list (default all: {','.join(ALL_FAMILIES)}).")
    st.add_argument("--skills-root", help="Override install root (default $HOME).")
    st.set_defaults(_handler=run_status)
    up = sub.add_parser("update", help="Refresh managed files.")
    up.add_argument("--targets", help=f"Comma list (default all: {','.join(ALL_FAMILIES)}).")
    up.add_argument("--skills-root", help="Override install root (default $HOME).")
    up.set_defaults(_handler=run_update)


def _resolve_targets(raw: str | None) -> list[str]:
    if not raw:
        return list(ALL_FAMILIES.keys())
    tokens = [t.strip().lower() for t in raw.replace(";", ",").split(",") if t.strip()]
    if not tokens:
        return list(ALL_FAMILIES.keys())
    if "all" in tokens:
        return list(ALL_FAMILIES.keys())
    out: list[str] = []
    for t in tokens:
        if t not in ALL_FAMILIES:
            raise ArgsError(f"skills: 未知目标 '{t}'（可选: {','.join(ALL_FAMILIES)}）")
        out.append(t)
    return out


async def run_status(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    families = _resolve_targets(args.targets)
    return installer.status(families, root=args.skills_root)


async def run_update(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    families = _resolve_targets(args.targets)
    return installer.update(families, root=args.skills_root)
