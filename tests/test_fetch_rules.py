"""fetch 命令的站点规则层：URL 规则分流、method="rule"、失败落回。

规则从隔离用户目录加载（不依赖包里内置规则）。不联网、不起浏览器。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import research_assistant.site_rules as sr


class _RunArgs:
    def __init__(self, urls, timeout=10, concurrency=4, no_browser=False, fmt="markdown", write=None):
        self.urls = urls
        self.write = write
        self.format = fmt
        self.timeout = timeout
        self.no_browser = no_browser
        self.no_login = False
        self.concurrency = concurrency


RULE_URL = "https://linux.do/t/topic/3066000"
PLAIN_URL = "https://example.com/page"


def _write_rule(home, name: str, text: str):
    d = home / "rules"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")
    rep = sr.reload_rules()
    assert rep.ok, rep.errors
    return rep


BROWSER_RULE = r'''schema = 1
[[rule]]
id = "browser-test"
hosts = ["linux.do"]
path_patterns = ['^/t/(?:[^/]+/)?(?P<id>\d+)$']
transport = "browser"
request = "https://{host}/t/topic/{id}.json"
parser = "discourse_topic"
'''

HTTP_RULE = r'''schema = 1
[[rule]]
id = "http-test"
hosts = ["api.example.com"]
path_patterns = ['^/item/(?P<id>\d+)$']
transport = "http"
request = "https://{host}/item/{id}.json"
parser = "json_path"

[rule.parser_config]
title = "$.title"
'''


async def test_browser_rule_skips_normal_and_reports_method(monkeypatch, home, tmp_path):
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    monkeypatch.chdir(tmp_path)
    _write_rule(home, "browser.toml", BROWSER_RULE)
    normal_calls: list[str] = []
    browser_calls: list[list[str]] = []

    async def forbidden_normal(*a, **k):
        normal_calls.append(a[1] if len(a) > 1 else "?")
        return "should not be used"

    async def fake_browser(config, urls, **kwargs):
        browser_calls.append(list(urls))
        kwargs["rule_hits"][RULE_URL] = "browser-test"
        return {u: "# 帖子正文" for u in urls}

    monkeypatch.setattr(fetch_cmd, "fetch_normal", forbidden_normal)
    monkeypatch.setattr(fetch_cmd, "fetch_with_browser", fake_browser)

    result = await fetch_cmd.run(_RunArgs([RULE_URL]), Config())
    [r] = result["results"]
    assert normal_calls == []
    assert browser_calls == [[RULE_URL]]
    assert r["method"] == "rule"
    assert r["rule"] == "browser-test"
    assert r["status"] == "success"
    text = Path(r["md_path"]).read_text(encoding="utf-8")
    assert "fetch_method: rule" in text
    assert "rule: browser-test" in text


async def test_browser_rule_failure_falls_back_to_html(monkeypatch, home, tmp_path):
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    monkeypatch.chdir(tmp_path)
    _write_rule(home, "browser.toml", BROWSER_RULE)

    async def fake_browser(config, urls, **kwargs):
        return {u: "# HTML 回退正文" for u in urls}  # 不写 rule_hits

    monkeypatch.setattr(fetch_cmd, "fetch_with_browser", fake_browser)

    result = await fetch_cmd.run(_RunArgs([RULE_URL]), Config())
    [r] = result["results"]
    assert r["method"] == "browser"
    assert "rule" not in r
    assert r["status"] == "success"


async def test_no_browser_keeps_original_chain(monkeypatch, home):
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    _write_rule(home, "browser.toml", BROWSER_RULE)
    normal_calls: list[str] = []

    async def fake_normal(config, url, fmt):
        normal_calls.append(url)
        return "normal body"

    async def forbid_browser(*a, **k):
        raise AssertionError("--no-browser 不应调用浏览器层")

    monkeypatch.setattr(fetch_cmd, "fetch_normal", fake_normal)
    monkeypatch.setattr(fetch_cmd, "fetch_with_browser", forbid_browser)

    result = await fetch_cmd.run(_RunArgs([RULE_URL], no_browser=True), Config())
    [r] = result["results"]
    assert normal_calls == [RULE_URL]
    assert r["method"] == "normal"


async def test_mixed_batch_routes_each_url_to_its_layer(monkeypatch, home, tmp_path):
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    monkeypatch.chdir(tmp_path)
    _write_rule(home, "browser.toml", BROWSER_RULE)
    seen_normal: list[str] = []
    seen_browser: list[str] = []

    async def fake_normal(config, url, fmt):
        seen_normal.append(url)
        return "plain body"

    async def fake_browser(config, urls, **kwargs):
        seen_browser.extend(urls)
        kwargs["rule_hits"][RULE_URL] = "browser-test"
        return {u: "rule body" if u == RULE_URL else "plain body" for u in urls}

    monkeypatch.setattr(fetch_cmd, "fetch_normal", fake_normal)
    monkeypatch.setattr(fetch_cmd, "fetch_with_browser", fake_browser)

    result = await fetch_cmd.run(_RunArgs([PLAIN_URL, RULE_URL]), Config())
    by_url = {r["url"]: r for r in result["results"]}
    assert seen_normal == [PLAIN_URL]
    assert seen_browser == [RULE_URL]
    assert by_url[PLAIN_URL]["method"] == "normal"
    assert by_url[RULE_URL]["method"] == "rule"


async def test_http_rule_runs_before_normal(monkeypatch, home, tmp_path):
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    monkeypatch.chdir(tmp_path)
    _write_rule(home, "http.toml", HTTP_RULE)
    url = "https://api.example.com/item/7"

    async def fake_rule_http(config, match, login=True):
        assert match.request_url == "https://api.example.com/item/7.json"
        return "# from json"

    async def forbid_normal(*a, **k):
        raise AssertionError("http 规则成功后不应再走普通接口")

    monkeypatch.setattr(fetch_cmd, "fetch_rule_http", fake_rule_http)
    monkeypatch.setattr(fetch_cmd, "fetch_normal", forbid_normal)

    result = await fetch_cmd.run(_RunArgs([url]), Config())
    [r] = result["results"]
    assert r["method"] == "rule"
    assert r["rule"] == "http-test"
    assert r["status"] == "success"


async def test_http_rule_failure_falls_back_to_normal(monkeypatch, home, tmp_path):
    from research_assistant.commands import fetch as fetch_cmd
    from research_assistant.config import Config

    monkeypatch.chdir(tmp_path)
    _write_rule(home, "http.toml", HTTP_RULE)
    url = "https://api.example.com/item/7"
    normal_calls: list[str] = []

    async def fake_rule_http(config, match, login=True):
        return None

    async def fake_normal(config, u, fmt):
        normal_calls.append(u)
        return "normal body"

    monkeypatch.setattr(fetch_cmd, "fetch_rule_http", fake_rule_http)
    monkeypatch.setattr(fetch_cmd, "fetch_normal", fake_normal)

    result = await fetch_cmd.run(_RunArgs([url]), Config())
    [r] = result["results"]
    assert normal_calls == [url]
    assert r["method"] == "normal"
    assert r["status"] == "success"
