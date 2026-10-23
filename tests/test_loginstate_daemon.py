"""cookie daemon 并发/异常路径单元测试。"""

import asyncio
import json

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


class ReplyingWS:
    """模拟扩展：收到 getCookies 后先回 cookie，再（可选）回 storage。"""

    closed = False

    def __init__(self, daemon, cookies=None, storage=None, delay_storage=0.0):
        self.daemon = daemon
        self.cookies = cookies if cookies is not None else [{"name": "sid", "value": "1", "domain": ".x.com"}]
        self.storage = storage
        self.delay_storage = delay_storage
        self.sent: list[dict] = []

    async def send_str(self, data):
        m = json.loads(data)
        self.sent.append(m)

        async def reply():
            await asyncio.sleep(0)
            await self.daemon._on_ext_message(
                {"type": "cookies", "data": self.cookies, "reqId": m.get("reqId")}
            )
            if m.get("wantStorage") and self.storage is not None:
                if self.delay_storage:
                    await asyncio.sleep(self.delay_storage)
                await self.daemon._on_ext_message(
                    {"type": "storage", "data": self.storage, "reqId": m.get("reqId")}
                )

        asyncio.create_task(reply())


def test_login_state_returns_cookies_and_storage():
    """storageProto=2：请求带 wantStorage，cookie 与 storage 都返回。"""
    d = CookieDaemon(port=0)
    d._storage_proto = 2
    storage = {"origins": {"https://x.com": {"l": {"token": "t"}, "s": {}}}}
    ws = ReplyingWS(d, storage=storage)
    d._ext_ws = ws

    async def run():
        return await d._fetch_login_state(timeout=1, storage_timeout=1)

    cookies, got = asyncio.run(run())
    assert cookies[0]["name"] == "sid"
    assert got == storage
    assert ws.sent[0]["wantStorage"] is True
    assert ws.sent[0]["reqId"]


def test_storage_timeout_does_not_fail_cookies():
    """storage 迟迟不回：cookie 仍成功返回，storage 标注超时。"""
    d = CookieDaemon(port=0)
    d._storage_proto = 2
    d._ext_ws = ReplyingWS(d, storage=None)  # 永不回 storage

    async def run():
        return await d._fetch_login_state(timeout=1, storage_timeout=0.1)

    cookies, storage = asyncio.run(run())
    assert cookies[0]["name"] == "sid"
    assert storage.get("error") == "storage_timeout"
    assert storage.get("origins") == {}


def test_old_extension_does_not_get_want_storage():
    """storageProto<2（旧扩展）：不请求、不等待 storage，保持旧行为。"""
    d = CookieDaemon(port=0)
    d._storage_proto = 0
    ws = ReplyingWS(d)
    d._ext_ws = ws

    async def run():
        return await d._fetch_login_state(timeout=1, storage_timeout=1)

    cookies, storage = asyncio.run(run())
    assert cookies[0]["name"] == "sid"
    assert storage == {}
    assert "wantStorage" not in ws.sent[0]


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
