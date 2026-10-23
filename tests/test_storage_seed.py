"""Web Storage 种入：脚本构造、大小上限、导航前安装顺序、扩展协议静态契约。"""

from __future__ import annotations

import json
from pathlib import Path

from research_assistant.fetch import browser as b


def _state(origin="https://example.com", **vals):
    return {"origins": {origin: {"l": vals or {"token": "abc"}, "s": {"tab": "1"}}}, "truncated": False}


# ---------------------------------------------------------------------------
# build_storage_seed_js
# ---------------------------------------------------------------------------


def test_empty_state_returns_none():
    assert b.build_storage_seed_js(None) is None
    assert b.build_storage_seed_js({}) is None
    assert b.build_storage_seed_js({"origins": {}}) is None


def test_seed_js_scopes_by_origin_and_writes_only_missing_keys():
    js = b.build_storage_seed_js(_state())
    assert js is not None
    assert "https://example.com" in js
    assert '"token":"abc"' in js
    assert "location.origin" in js
    assert "window.top !== window" in js  # 只种主框架
    assert "getItem(k) === null" in js  # 已有值不覆盖


def test_seed_js_escapes_line_separators():
    js = b.build_storage_seed_js(_state(value="a b c"))
    assert js is not None
    assert "\\u2028" in js and "\\u2029" in js


def test_seed_js_is_trimmed_under_size_cap():
    big = "x" * (1024 * 1024)
    origins = {f"https://site{i}.example": {"l": {"k": big}, "s": {}} for i in range(5)}
    js = b.build_storage_seed_js({"origins": origins})
    assert js is not None
    assert len(js.encode("utf-8")) <= b._STORAGE_SEED_MAX_BYTES + 4096


# ---------------------------------------------------------------------------
# 安装时机
# ---------------------------------------------------------------------------


class _FakeTab:
    def __init__(self):
        self.calls: list[tuple] = []

    def run_cdp(self, method, **kwargs):
        self.calls.append(("run_cdp", method, kwargs.get("source")))

    def get(self, url):
        self.calls.append(("get", url))

    @property
    def html(self):
        return "<html><body><p>" + "content" * 50 + "</p></body></html>"


def test_install_happens_before_first_navigation(monkeypatch):
    monkeypatch.setattr(b.cfbypass, "solve", lambda tab: True)
    monkeypatch.setattr(b, "_wait_network_idle", lambda tab, **k: None)
    monkeypatch.setattr(b.time, "sleep", lambda *a, **k: None)

    tab = _FakeTab()
    out = b._fetch_one_tab(tab, "https://example.com/page", "markdown", seed_js="SEED_JS")
    assert out
    assert tab.calls[0] == ("run_cdp", "Page.addScriptToEvaluateOnNewDocument", "SEED_JS")
    assert tab.calls[1][0] == "get"


def test_no_seed_means_no_cdp_call(monkeypatch):
    monkeypatch.setattr(b.cfbypass, "solve", lambda tab: True)
    monkeypatch.setattr(b, "_wait_network_idle", lambda tab, **k: None)
    monkeypatch.setattr(b.time, "sleep", lambda *a, **k: None)

    tab = _FakeTab()
    b._fetch_one_tab(tab, "https://example.com/page", "markdown", seed_js=None)
    assert not any(c[0] == "run_cdp" for c in tab.calls)


def test_install_swallows_cdp_errors(monkeypatch):
    class _Tab:
        def run_cdp(self, *a, **k):
            raise RuntimeError("cdp unavailable")

    b._install_storage_seed(_Tab(), "SEED")  # 不抛


# ---------------------------------------------------------------------------
# 扩展协议静态契约（防止扩展/manifest 漏改）
# ---------------------------------------------------------------------------

_EXT = Path(__file__).resolve().parents[1] / "src" / "research_assistant" / "loginstate" / "extension"


def test_manifest_grants_scripting():
    manifest = json.loads((_EXT / "manifest.json").read_text(encoding="utf-8"))
    assert "scripting" in manifest["permissions"]
    assert "<all_urls>" in manifest["host_permissions"]


def test_background_speaks_storage_proto_2():
    js = (_EXT / "background.js").read_text(encoding="utf-8")
    assert "STORAGE_PROTO = 2" in js
    assert "wantStorage" in js
    assert "sendStorage" in js
    assert "storageProto" in js
