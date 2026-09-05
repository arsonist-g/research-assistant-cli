"""放行规则安装器：把 `research-assistant` 命令写进各 AI agent 平台的免审配置。

目标注册表与 skill 四家（targets.ALL_FAMILIES）解耦：只有存在**文件级命令放行机制**的平台
才是放行目标（机制均经官方文档核实，2026-09）：

- claude  ~/.claude/settings.json   permissions.allow   Bash(research-assistant:*) /
                                                           PowerShell(research-assistant:*)
          `:*` = 尾通配前缀匹配（官方等价于 `research-assistant *`）；PowerShell 规则覆盖
          Windows 下 PowerShell 工具调起，非 Windows 平台该规则不匹配、无害。
- cursor  ~/.cursor/permissions.json terminalAllowlist  "research-assistant"（前缀语义）
- gemini  ~/.gemini/settings.json    tools.allowed      "run_shell_command(research-assistant)"
- codex   无命令级放行（仅 approval_policy / sandbox 全局档位，属用户安全决策，不代写）
- hermes  无命令级放行（approvals.mode 只拦危险命令模式，本 CLI 常规调用不触发）

写入策略：JSON 合并、幂等、只追加缺失规则、保留用户其余键与顺序；文件解析失败
（如 Cursor permissions.json 的 JSONC 注释）→ ConfigError 让用户手动合，绝不覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

CLI_NAME = "research-assistant"


@dataclass(frozen=True)
class PermissionTarget:
    name: str
    label: str
    file_relative: str | None  # 相对 $HOME；None = 无文件机制（指引型）
    key_path: tuple[str, ...]  # JSON 内目标数组键路径，如 ("permissions", "allow")
    rules: tuple[str, ...]  # 要 ensure 的放行规则
    note: str | None = None  # 生效前提 / 副作用说明
    guidance: str | None = None  # 无机制平台的指引（status 展示）


ALL_PERMISSION_TARGETS: dict[str, PermissionTarget] = {
    "claude": PermissionTarget(
        name="claude",
        label="Claude Code",
        file_relative=".claude/settings.json",
        key_path=("permissions", "allow"),
        rules=(
            f"Bash({CLI_NAME}:*)",
            f"PowerShell({CLI_NAME}:*)",
        ),
        note="user 级文件，全项目生效；新会话读取，已开的会话需重启。",
    ),
    "cursor": PermissionTarget(
        name="cursor",
        label="Cursor",
        file_relative=".cursor/permissions.json",
        key_path=("terminalAllowlist",),
        rules=(CLI_NAME,),
        note=(
            "需在 Cursor Settings 开启 Run Mode（Auto-review / Allowlist / Run Everything）才生效；"
            "文件中定义 terminalAllowlist 后将接管 IDE 内置放行表（官方行为）。"
        ),
    ),
    "gemini": PermissionTarget(
        name="gemini",
        label="Gemini CLI",
        file_relative=".gemini/settings.json",
        key_path=("tools", "allowed"),
        rules=(f"run_shell_command({CLI_NAME})",),
        note="写入后需重启 Gemini CLI 会话生效。",
    ),
    "codex": PermissionTarget(
        name="codex",
        label="Codex",
        file_relative=None,
        key_path=(),
        rules=(),
        guidance=(
            "Codex 无命令级放行机制（config.toml 仅 approval_policy / sandbox 全局档位）。"
            "如需免审只能全局放宽（如 workspace-write 档开 network_access），属用户安全决策，本工具不代写。"
        ),
    ),
    "hermes": PermissionTarget(
        name="hermes",
        label="Hermes Agent",
        file_relative=None,
        key_path=(),
        rules=(),
        guidance=(
            "Hermes approvals.mode 只拦危险命令模式，无命令级放行；"
            "research-assistant 常规调用不匹配危险模式，smart/manual 档下本就不触发审批。"
        ),
    ),
}

# install 的默认目标 = 有文件机制的可写平台；status 默认看全部（含指引型）
WRITABLE_TARGETS: tuple[str, ...] = ("claude", "cursor", "gemini")


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def _home(root: str | Path | None) -> Path:
    if root:
        return Path(root).expanduser()
    return Path.home()


def _file_path(target: PermissionTarget, home: Path) -> Path | None:
    if target.file_relative is None:
        return None
    return home / target.file_relative


# ---------------------------------------------------------------------------
# JSON 合并（幂等，只追加缺失规则）
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(
            f"permissions: {path} 解析失败（{e}）；疑似含注释或非标准 JSON，"
            "请手动把规则合并进去，本工具不覆盖无法解析的文件"
        ) from e
    if not isinstance(data, dict):
        raise ConfigError(f"permissions: {path} 顶层不是 JSON object，拒绝改写")
    return data


def _ensure_rules(path: Path, key_path: tuple[str, ...], rules: tuple[str, ...]) -> list[str]:
    """在 key_path 指向的数组里 ensure 规则，返回本次新增的规则（空 = 已全部存在）。

    中间键缺失则逐层创建；类型不符（中间节点非 object / 目标键非 array）→ ConfigError，不动文件。
    """
    data = _load_json(path)
    node: Any = data
    for key in key_path[:-1]:
        child = node.get(key)
        if child is None:
            child = {}
            node[key] = child
        if not isinstance(child, dict):
            raise ConfigError(f"permissions: {path} 的 {'/'.join(key_path)} 路径中间节点不是 object，拒绝改写")
        node = child
    last = key_path[-1]
    arr = node.get(last)
    if arr is None:
        arr = []
        node[last] = arr
    if not isinstance(arr, list):
        raise ConfigError(f"permissions: {path} 的 {'/'.join(key_path)} 不是数组，拒绝改写")
    added = [r for r in rules if r not in arr]
    if added:
        arr.extend(added)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added


def _rule_status(path: Path, key_path: tuple[str, ...], rules: tuple[str, ...]) -> list[dict[str, str]]:
    """逐规则报告 present/missing；文件不存在或不可解析时全部 missing / unreachable。"""
    statuses: list[dict[str, str]] = []
    try:
        data = _load_json(path)
    except ConfigError:
        return [{"rule": r, "status": "unreachable"} for r in rules]
    node: Any = data
    for key in key_path:
        if not isinstance(node, dict) or key not in node:
            node = None
            break
        node = node[key]
    present_set = set(node) if isinstance(node, list) else set()
    for r in rules:
        statuses.append({"rule": r, "status": "present" if r in present_set else "missing"})
    return statuses


# ---------------------------------------------------------------------------
# install / status
# ---------------------------------------------------------------------------


def install(targets: list[str], *, root: str | Path | None = None) -> dict[str, Any]:
    home = _home(root)
    out: list[dict[str, Any]] = []
    for name in targets:
        target = ALL_PERMISSION_TARGETS[name]
        entry: dict[str, Any] = {"name": name, "label": target.label}
        path = _file_path(target, home)
        if path is None:
            entry.update(status="skipped", file=None, added="", reason=target.guidance)
            out.append(entry)
            continue
        try:
            added = _ensure_rules(path, target.key_path, target.rules)
        except ConfigError as e:
            entry.update(status="error", file=str(path), added="", error=e.message)
            out.append(entry)
            continue
        entry.update(
            status="updated" if added else "already",
            file=str(path),
            added=", ".join(added),
            note=target.note,
        )
        out.append(entry)
    counts: dict[str, int] = {}
    for it in out:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    return {"root": str(home), "targets": out, "status_counts": counts}


def status(targets: list[str], *, root: str | Path | None = None) -> dict[str, Any]:
    home = _home(root)
    out: list[dict[str, Any]] = []
    for name in targets:
        target = ALL_PERMISSION_TARGETS[name]
        path = _file_path(target, home)
        if path is None:
            out.append(
                {
                    "name": name,
                    "label": target.label,
                    "supported": False,
                    "file": None,
                    "guidance": target.guidance,
                }
            )
            continue
        rules = _rule_status(path, target.key_path, target.rules)
        missing = [r["rule"] for r in rules if r["status"] != "present"]
        out.append(
            {
                "name": name,
                "label": target.label,
                "supported": True,
                "file": str(path),
                "rules": rules,
                "rules_present": f"{len(target.rules) - len(missing)}/{len(target.rules)}",
                "missing": ", ".join(missing),
                "note": target.note,
            }
        )
    return {"root": str(home), "targets": out}
