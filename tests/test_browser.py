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
