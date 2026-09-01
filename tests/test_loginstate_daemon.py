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
