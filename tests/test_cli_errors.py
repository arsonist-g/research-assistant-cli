"""cli 参数错误归一：choices/越界/缺参/未知参数 都走 stdout JSON + exit 2。

_JSONArgumentParser.error 把 argparse 层错误转 ArgsError，与 handler 层 ArgsError 统一进
JSON error 体。覆盖顶层命令、provider 子命令、全局 flag 三个 parser 层级。
"""

from __future__ import annotations

import json

import pytest

from research_assistant.cli import main
from research_assistant.config import Config


@pytest.fixture(autouse=True)
def _stub_config(monkeypatch):
    """main() 会 load_config（默认路径）；stub 成空 Config，免依赖测试机配置文件。"""
    monkeypatch.setattr("research_assistant.cli.load_config", lambda path: Config())


def _run(capsys, argv):
    code = main(argv)
    return code, capsys.readouterr().out


def test_choices_error_is_json(capsys):
    """非法 choices（--format pdf）→ stdout JSON + exit 2。"""
    code, out = _run(capsys, ["fetch", "http://x", "--format", "pdf"])
    assert code == 2
    data = json.loads(out)
    assert data["error"]["code"] == "ARGS"
    assert "format" in data["error"]["message"]


def test_range_error_is_json(capsys):
    """越界整数（--timeout 999）→ stdout JSON，message 含范围 1-300。"""
    code, out = _run(capsys, ["fetch", "http://x", "--timeout", "999"])
    assert code == 2
    data = json.loads(out)
    assert data["error"]["code"] == "ARGS"
    assert "1-300" in data["error"]["message"]


def test_non_integer_error_is_json(capsys):
    """非整数（--timeout abc）→ stdout JSON + exit 2（bounded_int 抛 ArgumentTypeError）。"""
    code, out = _run(capsys, ["fetch", "http://x", "--timeout", "abc"])
    assert code == 2
    assert json.loads(out)["error"]["code"] == "ARGS"


def test_missing_required_arg_is_json(capsys):
    """缺必填位置参数（fetch 无 URL）→ stdout JSON + exit 2。"""
    code, out = _run(capsys, ["fetch"])
    assert code == 2
    assert json.loads(out)["error"]["code"] == "ARGS"


def test_browser_search_range_error_is_json(capsys):
    """provider 子命令越界（browser search --timeout 0）→ stdout JSON（子 parser 也归一）。"""
    code, out = _run(capsys, ["browser", "search", "q", "--timeout", "0"])
    assert code == 2
    data = json.loads(out)
    assert data["error"]["code"] == "ARGS"
    assert "1-300" in data["error"]["message"]


def test_global_output_choices_error_is_json(capsys):
    """全局 flag 非法 choices（--output yaml）→ stdout JSON（pre-parser 也归一）。"""
    code, out = _run(capsys, ["--output", "yaml", "fetch", "http://x"])
    assert code == 2
    assert json.loads(out)["error"]["code"] == "ARGS"
