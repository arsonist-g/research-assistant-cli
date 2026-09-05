"""permissions：放行规则注册表 / install 幂等合并 / status 探测 / 命令层目标解析。

回归契约（行为验收标准，逐条对应）：
- 注册表：五家键集；codex/hermes 无文件机制（file_relative=None）；WRITABLE_TARGETS 为可写三家。
- install 全新根：三家文件写入平台契约规则（Claude Bash(prefix:*) / Cursor terminalAllowlist
  前缀条目 / Gemini run_shell_command(prefix)），条目 status=updated、status_counts={"updated": 3}。
- 幂等：二跑全部 already、added 为空，文件字节不变（无重写扰动）。
- 合并：保留用户既有键与数组顺序，仅追加缺失规则。
- 不可解析 / 类型不符文件：错误显式浮现（非静默跳过），文件字节不动（绝不覆盖）。
- 无机制平台（codex/hermes）：skipped + reason，root 下不创建任何文件。
- status：fresh 根 0/N 全 missing；装后 N/N 全 present；不可解析全 unreachable 且不抛异常。
- _resolve_targets：None/空串/空白 → 默认；all → 五家；逗号/分号/空格分隔；大小写不敏感；
  未知名 → ArgsError。

所有 root 一律用 tmp_path，绝不触碰真实 $HOME；无网络、无时间依赖。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from research_assistant import permissions as perm
from research_assistant.commands import permissions as perm_cmd
from research_assistant.errors import ArgsError

# 平台契约规则字面量（契约给定，独立书写，勿从实现拷贝）
CLAUDE_ALLOW = ["Bash(research-assistant:*)", "PowerShell(research-assistant:*)"]
CURSOR_ALLOW = ["research-assistant"]
GEMINI_ALLOW = ["run_shell_command(research-assistant)"]

ALL_NAMES = ["claude", "cursor", "gemini", "codex", "hermes"]
WRITABLE = ["claude", "cursor", "gemini"]

# 契约给定的各平台文件相对路径
EXPECTED_RELPATHS = {
    "claude": ".claude/settings.json",
    "cursor": ".cursor/permissions.json",
    "gemini": ".gemini/settings.json",
}

# 契约 §4 的用户既有 claude 配置样例
USER_CLAUDE_SETTINGS = {
    "model": "opus",
    "permissions": {
        "allow": ["Bash(npm run test:*)", "Bash(research-assistant:*)"],
        "deny": [],
    },
    "env": {"FOO": "bar"},
}


def _entry(result: dict, name: str) -> dict:
    return next(e for e in result["targets"] if e["name"] == name)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class TestRegistry:
    """注册表形状。Oracle: specified（契约 §1 的键集与 file_relative 约定）。"""

    def test_all_targets_keys(self):
        assert set(perm.ALL_PERMISSION_TARGETS) == {"claude", "cursor", "gemini", "codex", "hermes"}

    def test_writable_targets(self):
        assert perm.WRITABLE_TARGETS == ("claude", "cursor", "gemini")

    def test_file_relative_presence(self):
        # 无机制平台没有文件目标
        for name in ("codex", "hermes"):
            assert perm.ALL_PERMISSION_TARGETS[name].file_relative is None
        # 可写平台必须有文件目标
        for name in ("claude", "cursor", "gemini"):
            assert perm.ALL_PERMISSION_TARGETS[name].file_relative is not None


# ---------------------------------------------------------------------------
# install：全新根
# ---------------------------------------------------------------------------


class TestInstallFreshRoot:
    """install 全新根写入三家文件。Oracle: specified（契约 §2 平台规则字面量与条目断言）。"""

    def test_claude_settings_json(self, tmp_path):
        perm.install(WRITABLE, root=tmp_path)
        data = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        # 恰好两条、Bash 在前（契约给定顺序）
        assert data["permissions"]["allow"] == CLAUDE_ALLOW

    def test_cursor_permissions_json(self, tmp_path):
        perm.install(WRITABLE, root=tmp_path)
        data = json.loads((tmp_path / ".cursor" / "permissions.json").read_text(encoding="utf-8"))
        assert data["terminalAllowlist"] == CURSOR_ALLOW

    def test_gemini_settings_json(self, tmp_path):
        perm.install(WRITABLE, root=tmp_path)
        data = json.loads((tmp_path / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        assert data["tools"]["allowed"] == GEMINI_ALLOW

    def test_result_entries_and_counts(self, tmp_path):
        result = perm.install(WRITABLE, root=tmp_path)
        for name, rel in EXPECTED_RELPATHS.items():
            entry = _entry(result, name)
            assert entry["status"] == "updated"
            assert Path(entry["file"]) == tmp_path / rel
        assert result["status_counts"] == {"updated": 3}


# ---------------------------------------------------------------------------
# install：幂等
# ---------------------------------------------------------------------------


class TestInstallIdempotent:
    """install 幂等。Oracle: specified（二跑 already / added 空）+ derived（字节不变关系）。"""

    def test_second_run_all_already(self, tmp_path):
        perm.install(WRITABLE, root=tmp_path)
        result = perm.install(WRITABLE, root=tmp_path)
        for name in WRITABLE:
            entry = _entry(result, name)
            assert entry["status"] == "already"
            assert entry["added"] == ""
        assert result["status_counts"] == {"already": 3}

    def test_second_run_no_rewrite_churn(self, tmp_path):
        perm.install(WRITABLE, root=tmp_path)
        before = {rel: (tmp_path / rel).read_bytes() for rel in EXPECTED_RELPATHS.values()}
        perm.install(WRITABLE, root=tmp_path)
        for rel, blob in before.items():
            assert (tmp_path / rel).read_bytes() == blob


# ---------------------------------------------------------------------------
# install：合并保留用户内容
# ---------------------------------------------------------------------------


class TestInstallMerge:
    """install 合并语义。Oracle: specified（契约 §4 的 JSON 样例与合并后 allow 列表）。"""

    @staticmethod
    def _write_user_claude(tmp_path) -> None:
        p = tmp_path / ".claude" / "settings.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(USER_CLAUDE_SETTINGS, indent=2), encoding="utf-8")

    def test_merge_preserves_user_keys_and_appends_missing(self, tmp_path):
        self._write_user_claude(tmp_path)
        perm.install(["claude"], root=tmp_path)
        data = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        # 用户其余键原样保留
        assert data["model"] == "opus"
        assert data["env"] == {"FOO": "bar"}
        assert data["permissions"]["deny"] == []
        # 用户两条在前（顺序保留），仅追加缺失的 PowerShell 规则
        assert data["permissions"]["allow"] == [
            "Bash(npm run test:*)",
            "Bash(research-assistant:*)",
            "PowerShell(research-assistant:*)",
        ]

    def test_merge_entry_reports_added_rule(self, tmp_path):
        self._write_user_claude(tmp_path)
        result = perm.install(["claude"], root=tmp_path)
        entry = _entry(result, "claude")
        assert entry["status"] == "updated"
        assert entry["added"] == "PowerShell(research-assistant:*)"

    def test_untouched_families_report_already(self, tmp_path):
        perm.install(WRITABLE, root=tmp_path)
        # 用户手动改写 claude 配置（保留 Bash 规则、缺 PowerShell 规则）
        self._write_user_claude(tmp_path)
        result = perm.install(WRITABLE, root=tmp_path)
        assert _entry(result, "claude")["status"] == "updated"
        assert _entry(result, "cursor")["status"] == "already"
        assert _entry(result, "gemini")["status"] == "already"
        assert result["status_counts"] == {"updated": 1, "already": 2}


# ---------------------------------------------------------------------------
# install：不可解析文件绝不触碰
# ---------------------------------------------------------------------------


class TestInstallUnparseable:
    """不可解析文件显式报错且字节不动。Oracle: specified（契约 §5）。

    契约原文写 "raises ConfigError"；实现把 ConfigError 记入条目（status=error、error=消息）
    而非向上抛。本类断言契约的实质部分：错误显式浮现（非静默跳过）+ 文件字节不变；
    上抛与否的分歧见交付报告。
    """

    def test_jsonc_comment_file_surfaces_error_untouched(self, tmp_path):
        p = tmp_path / ".cursor" / "permissions.json"
        p.parent.mkdir(parents=True)
        original = b'// Cursor-style JSONC comment\n{\n  "terminalAllowlist": []\n}\n'
        p.write_bytes(original)
        result = perm.install(["cursor"], root=tmp_path)
        entry = _entry(result, "cursor")
        assert entry["status"] == "error"
        assert entry["error"]
        assert p.read_bytes() == original

    def test_invalid_json_file_surfaces_error_untouched(self, tmp_path):
        p = tmp_path / ".cursor" / "permissions.json"
        p.parent.mkdir(parents=True)
        original = b"{not-json"
        p.write_bytes(original)
        result = perm.install(["cursor"], root=tmp_path)
        entry = _entry(result, "cursor")
        assert entry["status"] == "error"
        assert p.read_bytes() == original


# ---------------------------------------------------------------------------
# install：类型不符拒绝改写
# ---------------------------------------------------------------------------


class TestInstallWrongTypes:
    """目标路径类型不符时拒绝改写。Oracle: specified（契约 §6，错误呈现方式同 §5）。"""

    def _run_claude_install_with_content(self, tmp_path, content: bytes) -> dict:
        p = tmp_path / ".claude" / "settings.json"
        p.parent.mkdir(parents=True)
        p.write_bytes(content)
        result = perm.install(["claude"], root=tmp_path)
        assert p.read_bytes() == content
        return result

    def test_allow_not_a_list(self, tmp_path):
        result = self._run_claude_install_with_content(
            tmp_path, json.dumps({"permissions": {"allow": "not-a-list"}}).encode("utf-8")
        )
        assert _entry(result, "claude")["status"] == "error"

    def test_permissions_not_an_object(self, tmp_path):
        result = self._run_claude_install_with_content(
            tmp_path, json.dumps({"permissions": "not-an-object"}).encode("utf-8")
        )
        assert _entry(result, "claude")["status"] == "error"

    def test_top_level_not_an_object(self, tmp_path):
        result = self._run_claude_install_with_content(tmp_path, b"[1, 2]")
        assert _entry(result, "claude")["status"] == "error"


# ---------------------------------------------------------------------------
# install：无机制平台
# ---------------------------------------------------------------------------


class TestInstallUnsupported:
    """codex/hermes 无文件机制 → skipped。Oracle: specified（契约 §7）。"""

    def test_codex_entry_skipped_with_reason(self, tmp_path):
        result = perm.install(["codex"], root=tmp_path)
        entry = _entry(result, "codex")
        assert entry["status"] == "skipped"
        assert entry["file"] is None
        assert entry["reason"]

    def test_codex_creates_nothing_under_root(self, tmp_path):
        perm.install(["codex"], root=tmp_path)
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# status：全新根 / 装后 / 不可解析
# ---------------------------------------------------------------------------


class TestStatusFreshRoot:
    """status 全新根。Oracle: specified（§8 计数与 supported）+ derived（由 0/N 推出的逐规则 missing）。"""

    def test_writable_families_all_missing(self, tmp_path):
        st = perm.status(WRITABLE, root=tmp_path)
        claude = _entry(st, "claude")
        assert claude["supported"] is True
        assert claude["rules_present"] == "0/2"
        assert [r["status"] for r in claude["rules"]] == ["missing", "missing"]
        for rule in CLAUDE_ALLOW:
            assert rule in claude["missing"]
        for name in ("cursor", "gemini"):
            e = _entry(st, name)
            assert e["supported"] is True
            assert e["rules_present"] == "0/1"
            assert [r["status"] for r in e["rules"]] == ["missing"]

    def test_guidance_families_unsupported(self, tmp_path):
        st = perm.status(["codex", "hermes"], root=tmp_path)
        for name in ("codex", "hermes"):
            e = _entry(st, name)
            assert e["supported"] is False
            assert e["guidance"]


class TestStatusAfterInstall:
    """status 装后全 present。Oracle: specified（契约 §9）。"""

    def test_all_present_after_install(self, tmp_path):
        perm.install(WRITABLE, root=tmp_path)
        st = perm.status(WRITABLE, root=tmp_path)
        claude = _entry(st, "claude")
        assert claude["rules_present"] == "2/2"
        assert claude["missing"] == ""
        assert all(r["status"] == "present" for r in claude["rules"])
        for name in ("cursor", "gemini"):
            e = _entry(st, name)
            assert e["rules_present"] == "1/1"
            assert e["missing"] == ""
            assert all(r["status"] == "present" for r in e["rules"])


class TestStatusUnparseable:
    """status 遇不可解析文件不抛异常，规则全 unreachable。Oracle: specified（契约 §10）。"""

    def test_invalid_json_rules_unreachable(self, tmp_path):
        p = tmp_path / ".cursor" / "permissions.json"
        p.parent.mkdir(parents=True)
        p.write_text("{oops", encoding="utf-8")
        st = perm.status(["cursor"], root=tmp_path)  # 不得抛异常
        entry = _entry(st, "cursor")
        assert all(r["status"] == "unreachable" for r in entry["rules"])


# ---------------------------------------------------------------------------
# 命令层：_resolve_targets 与命令表面
# ---------------------------------------------------------------------------


class TestResolveTargets:
    """--targets 解析文法。Oracle: specified（契约 §11）。"""

    DEFAULT = ["claude", "cursor", "gemini"]

    def test_none_returns_default(self):
        assert perm_cmd._resolve_targets(None, list(self.DEFAULT)) == self.DEFAULT

    def test_empty_returns_default(self):
        assert perm_cmd._resolve_targets("", list(self.DEFAULT)) == self.DEFAULT

    def test_whitespace_returns_default(self):
        assert perm_cmd._resolve_targets("   ", list(self.DEFAULT)) == self.DEFAULT

    def test_all_returns_five_names(self):
        assert perm_cmd._resolve_targets("all", list(self.DEFAULT)) == ALL_NAMES

    def test_comma_list_with_spaces(self):
        assert perm_cmd._resolve_targets("claude, cursor", list(self.DEFAULT)) == ["claude", "cursor"]

    def test_semicolon_separator(self):
        assert perm_cmd._resolve_targets("claude;cursor", list(self.DEFAULT)) == ["claude", "cursor"]

    def test_case_insensitive(self):
        assert perm_cmd._resolve_targets("CLAUDE", list(self.DEFAULT)) == ["claude"]

    def test_unknown_name_raises_args_error(self):
        with pytest.raises(ArgsError):
            perm_cmd._resolve_targets("vscode", list(self.DEFAULT))


class TestCommandSurface:
    """命令层公共面：NAME/ALIASES/HELP 与 status/install 子命令注册。Oracle: specified（命令行语法）。"""

    def test_name_aliases_help(self):
        assert perm_cmd.NAME == "permissions"
        assert isinstance(perm_cmd.ALIASES, list) and perm_cmd.ALIASES
        assert isinstance(perm_cmd.HELP, str) and perm_cmd.HELP.strip()

    def test_registers_status_and_install_subcommands(self):
        parser = argparse.ArgumentParser()
        perm_cmd.register(parser.add_subparsers(dest="command"))
        ns = parser.parse_args(["permissions", "status"])
        assert ns.subcommand == "status"
        ns = parser.parse_args(["permissions", "install"])
        assert ns.subcommand == "install"
