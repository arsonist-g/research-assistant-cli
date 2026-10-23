"""rules 命令：validate/list/explain/test（隔离用户规则目录，不联网）。"""

from __future__ import annotations

import argparse
import json

import pytest

import research_assistant.site_rules as sr
from research_assistant.commands import rules as rules_cmd
from research_assistant.config import Config


RULE = r'''schema = 1

[[rule]]
id = "discourse-test"
hosts = ["linux.do"]
path_patterns = ['^/t/(?P<id>\d+)$']
transport = "browser"
request = "https://{host}/t/topic/{id}.json"
parser = "discourse_topic"
preprocess = [{ name = "strip_hidden_nodes" }]
'''


def _setup(home, parse_expected: str = "hello") -> None:
    rules = home / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "discourse.toml").write_text(RULE, encoding="utf-8")
    fix = rules / "fixtures" / "discourse-test"
    fix.mkdir(parents=True, exist_ok=True)
    (fix / "cases.toml").write_text(
        '[[match]]\nurl = "https://linux.do/t/1"\nrequest = "https://linux.do/t/topic/1.json"\n'
        '[[reject]]\nurl = "https://linux.do/latest"\n'
        f'[[parse]]\nurl = "https://linux.do/t/1"\nresponse = "response.json"\ncontains = ["{parse_expected}"]\n',
        encoding="utf-8",
    )
    payload = {
        "title": "T",
        "posts_count": 1,
        "post_stream": {"posts": [
            {"post_number": 1, "username": "alice",
             "cooked": "<p>hello</p>"},
        ]},
    }
    (fix / "response.json").write_text(json.dumps(payload), encoding="utf-8")
    sr.reload_rules()


@pytest.mark.asyncio
async def test_validate_list_explain(home):
    _setup(home)
    rep = await rules_cmd._validate(argparse.Namespace(), Config())
    assert rep["ok"] is True
    assert rep["rule_count"] == 1

    listed = await rules_cmd._list(argparse.Namespace(), Config())
    assert listed["count"] == 1
    assert listed["rules"][0]["id"] == "discourse-test"

    explained = await rules_cmd._explain(argparse.Namespace(url="https://linux.do/t/1"), Config())
    assert explained["matched"] is True
    assert explained["request_url"] == "https://linux.do/t/topic/1.json"
    assert explained["parser"] == "discourse_topic"

    missing = await rules_cmd._explain(argparse.Namespace(url="https://linux.do/latest"), Config())
    assert missing["matched"] is False


@pytest.mark.asyncio
async def test_rules_test_passes_and_fails(home):
    _setup(home)
    ok = await rules_cmd._test(argparse.Namespace(rule=None), Config())
    assert ok["ok"] is True
    assert ok["passed"] == 1
    assert ok["failed"] == 0

    # 改 fixture 的期望，测试必须红
    _setup(home, parse_expected="not in the response")
    bad = await rules_cmd._test(argparse.Namespace(rule=None), Config())
    assert bad["ok"] is False
    assert bad["failed"] == 1
