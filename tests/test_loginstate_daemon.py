"""cookie daemon 并发/异常路径单元测试。"""

import asyncio

import pytest

from research_assistant.loginstate.daemon import CookieDaemon


class DeadWS:
    """模拟扩展 WS 恰在发送前死掉:closed=False 但 send_str 抛连接错误。"""

    closed = False

    async def send_str(self, _):
        raise ConnectionError("ws gone")


def test_send_failure_does_not_leak_pending():
    """send_str 失败不得泄漏 _pending——泄漏会让后续所有 /cookies 永久报
    「已有进行中的 cookie 请求」,直到 daemon 重启。"""
    d = CookieDaemon(port=0)
    d._ext_ws = DeadWS()

    async def two_calls():
        with pytest.raises(ConnectionError):
            await d._fetch_cookies(timeout=1)
        # 第二次调用应仍是连接错误,而非 RuntimeError("已有进行中的 cookie 请求")
        with pytest.raises(ConnectionError):
            await d._fetch_cookies(timeout=1)

    asyncio.run(two_calls())


def test_spawn_daemon_hides_console_on_windows(monkeypatch, tmp_path):
    """Windows 上拉起 daemon 不得用 DETACHED_PROCESS:venv 的 python.exe 是
    trampoline,re-exec 到 base 解释器时不继承 creation flags,DETACHED 会让
    base 解释器新建可见控制台窗口;CREATE_NO_WINDOW 则让其继承隐藏控制台。"""
    import subprocess
    import sys

    from research_assistant import config as config_mod
    from research_assistant.loginstate import daemonctl

    class _Proc:
        pid = 4242

        def wait(self, timeout=None):
            return None

        def poll(self):
            return None

    captured = {}

    def _fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(config_mod, "logs_dir", lambda: tmp_path)
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    class _BrowserCfg:
        daemon_port = 17890

    class _Cfg:
        browser = _BrowserCfg()

    daemonctl._spawn_daemon(_Cfg())

    kwargs = captured["kwargs"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    if sys.platform.startswith("win"):
        flags = kwargs["creationflags"]
        assert flags & subprocess.CREATE_NO_WINDOW
        assert not flags & subprocess.DETACHED_PROCESS
