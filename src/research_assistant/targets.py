"""安装目标矩阵（四家 AI Agent，ADR-0008 / data-model §2.3）。

每家是一个 Family：skill 路径 + agent 路径 + agent 格式（md/toml/none）。
managed skill 文件（SKILL.md）相对通用；managed agent 定义因家而异：
    Claude / Cursor → md + frontmatter
    Codex           → toml
    Hermes          → 无独立文件（researcher 人设融入 skill）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Family:
    name: str
    label: str
    skill_relative: str  # 相对 $HOME 的 skill 目录
    agent_relative: str | None  # 相对 $HOME 的 agent 文件（None=无独立文件）
    agent_format: str  # "md" | "toml" | "none"
    agent_support: str  # "static" | "runtime" | "none"
    md_fields: dict  # md frontmatter 额外字段（tools/readonly 等）


SKILL_NAME = "research-assistant"
AGENT_NAME = "researcher"

ALL_FAMILIES: dict[str, Family] = {
    "claude": Family(
        name="claude",
        label="Claude Code",
        skill_relative=f".claude/skills/{SKILL_NAME}",
        agent_relative=".claude/agents/researcher.md",
        agent_format="md",
        agent_support="static",
        md_fields={"tools": "WebFetch, Bash", "model": "inherit"},
    ),
    "cursor": Family(
        name="cursor",
        label="Cursor",
        skill_relative=f".cursor/skills/{SKILL_NAME}",
        agent_relative=".cursor/agents/researcher.md",
        agent_format="md",
        agent_support="static",
        md_fields={"readonly": "false", "model": "inherit"},
    ),
    "codex": Family(
        name="codex",
        label="Codex",
        skill_relative=f".agents/skills/{SKILL_NAME}",  # ~/.agents/skills/（~/.codex/skills deprecated）
        agent_relative=".codex/agents/researcher.toml",
        agent_format="toml",
        agent_support="static",
        md_fields={},
    ),
    "hermes": Family(
        name="hermes",
        label="Hermes Agent",
        skill_relative=f".hermes/skills/{SKILL_NAME}",
        agent_relative=None,  # 无独立 agent 文件
        agent_format="none",
        agent_support="runtime",  # 运行时 delegate_task
        md_fields={},
    ),
}
