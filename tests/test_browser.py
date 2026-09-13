"""browser 模块单元测试：_wait_network_idle（网络静默等待）+ 浏览器进程数限流。

不联网、不起浏览器（live 路径在 test_browser_live.py / test_cf_live.py）。
"""

from __future__ import annotations

import itertools
import time

import pytest


# ---------------------------------------------------------------------------
# _wait_network_idle（fake tab：listen.start/wait 可控）
# ---------------------------------------------------------------------------


class _FakeListen:
    """模拟 DrissionPage tab.listen：start 标记，wait 按 results 序列返（耗尽返 None 模拟超时）。"""

    def __init__(self, wait_results=None, start_raises=False):
        self._results = iter(wait_results) if wait_results is not None else iter(())
        self._start_raises = start_raises
        self.started = False

    def start(self, *a, **k):
        if self._start_raises:
            raise RuntimeError("listen unavailable")
        self.started = True

    def wait(self, count=1, timeout=None, fit_count=True, raise_err=False):
        try:
            r = next(self._results)
            time.sleep(0.001)  # 模拟请求到达耗时，避免密集请求下循环过快 time 不增
            return r
        except StopIteration:
            time.sleep(timeout or 0)  # 无请求 → 阻塞到 timeout（模拟）
            return None


class _FakeTab:
    def __init__(self, listen):
        self.listen = listen


def test_wait_network_idle_silent_returns_fast():
    """全静默（wait 一直超时无请求）→ idle_window 后返，不等到 total_timeout。"""
    from research_assistant.fetch.browser import _wait_network_idle

    tab = _FakeTab(_FakeListen(wait_results=()))  # wait 永远返 None
    start = time.monotonic()
    _wait_network_idle(tab, total_timeout=5, idle_window=0.1, poll_interval=0.05)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0  # idle 0.1s 触发，远不到 total_timeout 5
    assert tab.listen.started


def test_wait_network_idle_listen_start_failure_returns():
    """listen.start 抛异常 → 直接返（不抛、不卡）。"""
    from research_assistant.fetch.browser import _wait_network_idle

    tab = _FakeTab(_FakeListen(start_raises=True))
    _wait_network_idle(tab, total_timeout=5)  # 不抛即通过


def test_wait_network_idle_timeout_fallback():
    """持续密集请求（间隔≈0，不进稳态区间 0.3-3s）+ idle_window 大 → 走 total_timeout 兜底。"""
    from research_assistant.fetch.browser import _wait_network_idle

    pkt = object()  # truthy
    tab = _FakeTab(_FakeListen(wait_results=itertools.repeat(pkt)))
    start = time.monotonic()
    _wait_network_idle(tab, total_timeout=0.3, idle_window=10, poll_interval=0.05)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2  # 走到 total_timeout（≈0.3s），未因 idle/稳态提前返


# ---------------------------------------------------------------------------
# 浏览器进程数限流（_acquire_browser_slot / _release_browser_slot / cleanup）
# ---------------------------------------------------------------------------


def _cfg(max_inst):
    from research_assistant.config import BrowserConfig, Config

    return Config(browser=BrowserConfig(max_browser_instances=max_inst))


def test_acquire_within_limit(home):
    from research_assistant.fetch.browser import _acquire_browser_slot, _release_browser_slot

    cfg = _cfg(2)
    s1 = _acquire_browser_slot(cfg)
    s2 = _acquire_browser_slot(cfg)
    assert s1.exists() and s2.exists()
    _release_browser_slot(s1)
    _release_browser_slot(s2)


def test_acquire_over_limit_raises(home):
    from research_assistant.fetch.browser import _acquire_browser_slot, _release_browser_slot
    from research_assistant.errors import ConfigError

    cfg = _cfg(2)
    s1 = _acquire_browser_slot(cfg)
    s2 = _acquire_browser_slot(cfg)
    with pytest.raises(ConfigError):
        _acquire_browser_slot(cfg)
    _release_browser_slot(s1)
    _release_browser_slot(s2)


def test_release_frees_slot(home):
    from research_assistant.fetch.browser import _acquire_browser_slot, _release_browser_slot

    cfg = _cfg(1)
    s1 = _acquire_browser_slot(cfg)
    _release_browser_slot(s1)
    s2 = _acquire_browser_slot(cfg)  # 释放后能再获取
    assert s2.exists()
    _release_browser_slot(s2)


def test_cleanup_reaps_dead_pid_lock(home):
    from research_assistant.fetch.browser import _browser_locks_dir, cleanup_browser_locks

    base = _browser_locks_dir()
    d = base / "lock-dead"
    d.mkdir()
    old = int(time.time()) - 99999  # 远超 ORPHAN_AGE_SECONDS
    (d / ".ra-meta").write_text(f"pid=999999\nstarted_at={old}\n", encoding="utf-8")
    reaped = cleanup_browser_locks()
    assert reaped >= 1
    assert not d.exists()


# ---------------------------------------------------------------------------
# DEC-028：真无头 —— 真实 UA 构造与启动参数
# ---------------------------------------------------------------------------


def test_native_user_agent_edge(monkeypatch):
    from research_assistant.fetch import browser as b

    monkeypatch.setattr(b, "_file_version", lambda exe: "152.0.4191.66")
    monkeypatch.setattr(b.sys, "platform", "win32")
    ua = b._native_user_agent("C:/fake/msedge.exe", "msedge")
    assert ua == (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36 Edg/152.0.0.0"
    )


def test_native_user_agent_never_contains_headless(monkeypatch):
    """核心不变量：构造出的 UA 绝不带 "Headless"（带则 UA 头与 client hints 矛盾被 CF 拦）。"""
    from research_assistant.fetch import browser as b

    monkeypatch.setattr(b, "_file_version", lambda exe: "152.0.4191.66")
    monkeypatch.setattr(b.sys, "platform", "win32")
    assert "Headless" not in b._native_user_agent("C:/fake/msedge.exe", "msedge")


def test_native_user_agent_chrome_has_no_edge_token(monkeypatch):
    from research_assistant.fetch import browser as b

    monkeypatch.setattr(b, "_file_version", lambda exe: "153.0.8010.36")
    monkeypatch.setattr(b.sys, "platform", "win32")
    ua = b._native_user_agent("C:/fake/chrome.exe", "chrome")
    assert "Chrome/153.0.0.0" in ua
    assert "Edg/" not in ua


def test_native_user_agent_none_when_version_unknown(monkeypatch):
    from research_assistant.fetch import browser as b

    monkeypatch.setattr(b, "_file_version", lambda exe: None)
    assert b._native_user_agent("C:/fake/msedge.exe", "msedge") is None
    assert b._native_user_agent(None, "msedge") is None


def test_build_dp_options_is_headless_with_real_ua(monkeypatch):
    """DEC-028：启动恒 --headless=new + 真实 UA + screen-info，且无 headed 专用参数。"""
    from pathlib import Path

    from research_assistant.config import Config
    from research_assistant.fetch import browser as b

    monkeypatch.setattr(b, "_resolve_executable", lambda cfg: ("C:/fake/msedge.exe", "test"))
    monkeypatch.setattr(b, "_file_version", lambda exe: "152.0.4191.66")
    monkeypatch.setattr(b.sys, "platform", "win32")
    monkeypatch.setattr(b, "resolve_proxy", lambda u: "")
    co = b._build_dp_options(Config(), Path("C:/tmp/p"))
    args = " ".join(co.arguments)
    assert "--headless=new" in args
    assert "Chrome/152.0.0.0" in args and "Edg/152.0.0.0" in args
    assert "Headless" not in args
    assert "--screen-info={1440x900}" in args
    assert "--window-position" not in args


def test_build_dp_options_still_headless_when_version_unknown(monkeypatch):
    """版本探测失败：降级为不带 --user-agent，但仍 headless（不崩、不回退 headed）。"""
    from pathlib import Path

    from research_assistant.config import Config
    from research_assistant.fetch import browser as b

    monkeypatch.setattr(b, "_resolve_executable", lambda cfg: ("C:/fake/msedge.exe", "test"))
    monkeypatch.setattr(b, "_file_version", lambda exe: None)
    monkeypatch.setattr(b, "resolve_proxy", lambda u: "")
    co = b._build_dp_options(Config(), Path("C:/tmp/p"))
    args = " ".join(co.arguments)
    assert "--headless=new" in args
    assert "--user-agent" not in args
