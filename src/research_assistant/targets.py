"""安装目标矩阵（各家 AI Agent，ADR-0008 / data-model §2.3）。

每家是一个 Family：skill 路径 + agent 路径 + agent 格式（md/toml/none）。
managed skill 文件（SKILL.md）相对通用；managed agent 定义因家而异：
    Claude / Cursor / PI-Desktop → md + frontmatter
    Codex                        → toml
    Hermes                       → 无独立文件（researcher 人设融入 skill）

PI-Desktop 的 host-core 把 AGENTS_DIR 硬编码为 `.agents`，与 Codex 家共用同一根目录：
skill 落点相同，只有 agent 文件不同（`.agents/subagents/researcher.md`）。
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
        md_fields={"tools": "Bash, Read, Write", "model": "haiku"},
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
    "pidesktop": Family(
        name="pidesktop",
        label="PI-Desktop",
        skill_relative=f".agents/skills/{SKILL_NAME}",  # 与 codex 家同目录（~/.agents/）
        agent_relative=".agents/subagents/researcher.md",
        agent_format="md",
        agent_support="static",
        # tools 用 PI-Desktop 自己的方括号列表写法（匹配其内置 agent 定义）。
        # 不写 model：PI-Desktop 的 model 必须是 <provider>/<model>，由用户按会话配置；
        # 省略即继承会话模型，写裸 id（如 haiku）无法解析。
        md_fields={"tools": "[Bash, Read, Write]"},
    ),
}
