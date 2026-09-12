"""installer：targets 矩阵、按家生成 agent（md/toml/none）、Codex TOML schema、install/status。

重点回归：Codex agent TOML 必须只含 AgentRoleToml 合法顶层字段
（name/description/developer_instructions），不含 sandbox_mode / mcp_servers（已修）。
"""

from __future__ import annotations

import re
import sys
import tomllib  # 3.11+；3.10 测试环境若是 3.10 需 tomli，此处 venv 为 3.12

from research_assistant import installer
from research_assistant.targets import AGENT_NAME, ALL_FAMILIES, SKILL_NAME


# ---------------------------------------------------------------------------
# targets 矩阵
# ---------------------------------------------------------------------------


class TestTargetsMatrix:
    def test_five_families_present(self):
        assert set(ALL_FAMILIES) == {"claude", "cursor", "codex", "hermes", "pidesktop"}

    def test_format_per_family(self):
        assert ALL_FAMILIES["claude"].agent_format == "md"
        assert ALL_FAMILIES["cursor"].agent_format == "md"
        assert ALL_FAMILIES["codex"].agent_format == "toml"
        assert ALL_FAMILIES["hermes"].agent_format == "none"
        assert ALL_FAMILIES["pidesktop"].agent_format == "md"

    def test_hermes_has_no_agent_file(self):
        assert ALL_FAMILIES["hermes"].agent_relative is None
        assert ALL_FAMILIES["hermes"].agent_support == "runtime"

    def test_pidesktop_shares_skill_dir_with_codex(self):
        """PI-Desktop 的 host-core 硬编码 AGENTS_DIR=".agents"，与 codex 家同根。"""
        assert ALL_FAMILIES["pidesktop"].skill_relative == ALL_FAMILIES["codex"].skill_relative
        assert ALL_FAMILIES["pidesktop"].agent_relative == ".agents/subagents/researcher.md"

    def test_skill_name_constants(self):
        assert SKILL_NAME == "research-assistant"
        assert AGENT_NAME == "researcher"


# ---------------------------------------------------------------------------
# generate_agent
# ---------------------------------------------------------------------------


class TestGenerateAgentMarkdown:
    def test_claude_has_frontmatter_with_tools_and_model(self):
        text = installer.generate_agent(ALL_FAMILIES["claude"])
        assert text.startswith("---\n")
        assert "name: researcher" in text
        assert "description:" in text
        # md_fields 里 Claude 带 tools + model
        assert "tools:" in text
        assert "model:" in text
        # tools 必须授予 Read/Write：persona 契约要求报告写盘、回读落盘长文档
        tools_line = next(ln for ln in text.splitlines() if ln.startswith("tools:"))
        assert "Read" in tools_line
        assert "Write" in tools_line
        # frontmatter 后接 persona 正文
        assert "researcher" in text.split("---", 2)[-1].lower()

    def test_cursor_has_readonly_field(self):
        text = installer.generate_agent(ALL_FAMILIES["cursor"])
        assert "readonly:" in text
        assert "name: researcher" in text

    def test_frontmatter_is_valid_yaml_block(self):
        text = installer.generate_agent(ALL_FAMILIES["claude"])
        # 取第一个 --- ... --- 块
        parts = text.split("---\n", 2)
        assert len(parts) >= 3  # ['', fm, body...]
        fm = parts[1]
        assert "name:" in fm


class TestGenerateCodexToml:
    def test_is_valid_toml_with_required_fields(self):
        text = installer.generate_agent(ALL_FAMILIES["codex"])
        parsed = tomllib.loads(text)  # 合法 TOML
        assert parsed["name"] == "researcher"
        assert "description" in parsed
        assert "developer_instructions" in parsed
        # persona 内容注入到 developer_instructions
        assert "research" in parsed["developer_instructions"].lower()

    def test_does_not_contain_invalid_top_level_keys(self):
        """回归：sandbox_mode / mcp_servers 不属于 AgentRoleToml 顶层字段，必须移除。"""
        text = installer.generate_agent(ALL_FAMILIES["codex"])
        assert "sandbox_mode" not in text
        assert "mcp_servers" not in text

    def test_developer_instructions_is_multiline_string(self):
        parsed = tomllib.loads(installer.generate_agent(ALL_FAMILIES["codex"]))
        di = parsed["developer_instructions"]
        assert isinstance(di, str)
        assert "\n" in di  # persona 多行


class TestGenerateAgentHermes:
    def test_returns_none(self):
        assert installer.generate_agent(ALL_FAMILIES["hermes"]) is None


class TestGenerateAgentPideDesktop:
    """PI-Desktop 契约（host-core user_subagents.rs + shared subagent-definition.ts）。

    - tools 用方括号列表，工具名限于其可指派集合（Read/Glob/Grep/BrowserPreview/Bash/Edit/Write）。
    - 不写 model：其 model 必须是 `<provider>/<model>`，省略即继承会话模型。
    - description 必填；正文即 delegate 的 system prompt。
    """

    def test_bracket_tool_list_and_no_model_pin(self):
        text = installer.generate_agent(ALL_FAMILIES["pidesktop"])
        fm = text.split("---\n", 2)[1]
        tools_line = next(ln for ln in fm.splitlines() if ln.startswith("tools:"))
        assert tools_line == "tools: [Bash, Read, Write]"
        assert "model:" not in fm

    def test_name_and_description_satisfy_pidesktop_parser(self):
        text = installer.generate_agent(ALL_FAMILIES["pidesktop"])
        fm = text.split("---\n", 2)[1]
        name = next(ln for ln in fm.splitlines() if ln.startswith("name:"))
        assert re.fullmatch(r"name: [a-z0-9-]{1,40}", name)
        desc = next(ln for ln in fm.splitlines() if ln.startswith("description:"))
        assert desc != "description:"
        assert len(desc) - len("description: ") <= 400

    def test_body_carries_the_persona(self):
        text = installer.generate_agent(ALL_FAMILIES["pidesktop"])
        body = text.split("---\n", 2)[2]
        assert body.strip()
        assert "research" in body.lower()


# ---------------------------------------------------------------------------
# skill_for_family
# ---------------------------------------------------------------------------


class TestSkillForFamily:
    def test_hermes_skill_includes_persona(self):
        skill = installer.skill_for_family(ALL_FAMILIES["hermes"])
        assert "researcher persona" in skill.lower()
        # persona 的指导内容融入
        assert "isolated" in skill.lower() or "隔离" in skill

    def test_non_hermes_skill_is_plain_skill_text(self):
        base = installer.skill_text()
        assert installer.skill_for_family(ALL_FAMILIES["claude"]) == base
        # 非 hermes 不附 persona
        assert "researcher persona (runtime" not in installer.skill_for_family(ALL_FAMILIES["cursor"])


# ---------------------------------------------------------------------------
# install / status / _compare（用 tmp root，不污染真实 home）
# ---------------------------------------------------------------------------


class TestInstallAndStatus:
    def test_install_writes_skill_and_agent_files(self, tmp_path):
        result = installer.install(["claude", "codex"], root=tmp_path)
        assert result["failed"] == []
        # Claude：skill + agent(md)
        claude_skill = tmp_path / ".claude/skills/research-assistant/SKILL.md"
        claude_agent = tmp_path / ".claude/agents/researcher.md"
        assert claude_skill.exists()
        assert claude_agent.exists()
        # Codex：skill + agent(toml)
        codex_agent = tmp_path / ".codex/agents/researcher.toml"
        assert codex_agent.exists()

    def test_hermes_installs_only_skill(self, tmp_path):
        result = installer.install(["hermes"], root=tmp_path)
        hermes_skill = tmp_path / ".hermes/skills/research-assistant/SKILL.md"
        assert hermes_skill.exists()
        hermes_entry = next(e for e in result["installed"] if e["family"] == "hermes")
        assert hermes_entry["agent_path"] is None
        assert hermes_entry["agent_format"] == "none"

    def test_pidesktop_installs_skill_and_subagent_under_agents_root(self, tmp_path):
        result = installer.install(["pidesktop"], root=tmp_path)
        assert result["failed"] == []
        assert (tmp_path / ".agents/skills/research-assistant/SKILL.md").exists()
        agent_file = tmp_path / ".agents/subagents/researcher.md"
        assert agent_file.exists()
        # 非递归扫描目录里的直接 .md 文件，frontmatter 后正文非空
        assert agent_file.read_text(encoding="utf-8").split("---\n", 2)[2].strip()

    def test_status_reports_up_to_date_after_install(self, tmp_path):
        installer.install(["claude"], root=tmp_path)
        st = installer.status(["claude"], root=tmp_path)
        claude = next(t for t in st["targets"] if t["family"] == "claude")
        assert claude["skill_status"] == "up-to-date"
        assert claude["agent_status"] == "up-to-date"

    def test_status_reports_missing_before_install(self, tmp_path):
        st = installer.status(["codex"], root=tmp_path)
        codex = next(t for t in st["targets"] if t["family"] == "codex")
        assert codex["skill_status"] == "missing"
        assert codex["agent_status"] == "missing"

    def test_status_reports_stale_after_manual_edit(self, tmp_path):
        installer.install(["claude"], root=tmp_path)
        agent_file = tmp_path / ".claude/agents/researcher.md"
        agent_file.write_text("# tampered\n", encoding="utf-8")
        st = installer.status(["claude"], root=tmp_path)
        claude = next(t for t in st["targets"] if t["family"] == "claude")
        assert claude["agent_status"] == "stale"

    def test_update_rewrites_to_up_to_date(self, tmp_path):
        installer.install(["claude"], root=tmp_path)
        (tmp_path / ".claude/agents/researcher.md").write_text("old", encoding="utf-8")
        installer.update(["claude"], root=tmp_path)
        st = installer.status(["claude"], root=tmp_path)
        assert next(t for t in st["targets"] if t["family"] == "claude")["agent_status"] == "up-to-date"


class TestCompare:
    def test_missing(self, tmp_path):
        assert installer._compare(tmp_path / "nope.md", "x") == "missing"

    def test_up_to_date(self, tmp_path):
        p = tmp_path / "f.md"
        p.write_text("content", encoding="utf-8")
        assert installer._compare(p, "content") == "up-to-date"

    def test_stale(self, tmp_path):
        p = tmp_path / "f.md"
        p.write_text("old", encoding="utf-8")
        assert installer._compare(p, "new") == "stale"
