"""cookie daemon（Python 移植自 cdt bridge/daemon.mjs，ADR-0006）。

单 aiohttp 进程同时承载：
    WS   ws://127.0.0.1:<port>/ws   扩展主动连，保持长连接，响应 getCookies
    HTTP http://127.0.0.1:<port>/status|/cookies   fetch/doctor 按需调
保活：每 25s 向扩展 ping（< service worker 30s 不活动阈值）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
from typing import Any

from aiohttp import web, WSMsgType

log = logging.getLogger("research_assistant.loginstate.daemon")

PING_INTERVAL = 25.0  # 秒


class CookieDaemon:
    def __init__(self, port: int) -> None:
        self.port = port
        self._ext_ws: web.WebSocketResponse | None = None
        self._last_cookies: list[dict[str, Any]] | None = None
        self._last_cookie_at: float = 0.0
        self._last_storage: dict[str, Any] = {}
        self._storage_proto = 0  # 扩展 hello 声明的能力（2 = 支持 Web Storage 分开发送）
        self._pending: asyncio.Future | None = None
        self._pending_storage: asyncio.Future | None = None
        self._lock = asyncio.Lock()

    # ---- WebSocket（扩展连这里）----
    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ext_ws = ws
        log.info("扩展已连接")
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    m = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                await self._on_ext_message(m)
            elif msg.type == WSMsgType.ERROR:
                log.warning("扩展 WS 错误: %s", ws.exception())
        self._ext_ws = None
        log.info("扩展断开，等待重连")
        return ws

    async def _on_ext_message(self, m: dict[str, Any]) -> None:
        t = m.get("type")
        if t == "cookies":
            self._last_cookies = m.get("data") or []
            self._last_cookie_at = asyncio.get_event_loop().time()
            by_proto = int(m.get("storageProto") or 0)
            if by_proto > self._storage_proto:
                self._storage_proto = by_proto
            if self._pending and not self._pending.done():
                self._pending.set_result(self._last_cookies)
                self._pending = None
        elif t == "storage":
            self._last_storage = m.get("data") or {}
            if self._pending_storage and not self._pending_storage.done():
                self._pending_storage.set_result(self._last_storage)
                self._pending_storage = None
        elif t == "hello":
            self._storage_proto = int(m.get("storageProto") or 0)
            log.info("收到扩展 hello（storageProto=%s）", self._storage_proto)
        elif t == "pong":
            pass
        elif t == "error":
            log.warning("扩展报错: %s", m.get("message"))
            if self._pending and not self._pending.done():
                self._pending.set_exception(RuntimeError(m.get("message", "扩展错误")))
                self._pending = None

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            if self._ext_ws is not None and not self._ext_ws.closed:
                try:
                    await self._ext_ws.send_str(json.dumps({"type": "ping"}))
                except Exception:
                    pass

    # ---- HTTP（fetch/doctor 调）----
    async def status_handler(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "extConnected": self._ext_ws is not None and not self._ext_ws.closed,
                "cachedCookieCount": len(self._last_cookies) if self._last_cookies else 0,
                "cachedStorageOrigins": len((self._last_storage or {}).get("origins") or {}),
                "storageProto": self._storage_proto,
                "lastCookieAt": self._last_cookie_at or None,
            }
        )

    async def cookies_handler(self, request: web.Request) -> web.Response:
        try:
            cookies, storage = await self._fetch_login_state(timeout=10.0)
            return web.json_response({"count": len(cookies), "data": cookies, "storage": storage})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=503)

    async def _fetch_cookies(self, timeout: float = 10.0) -> list[dict[str, Any]]:
        """只要 cookie 的兼容入口（测试与旧调用方）。"""
        cookies, _ = await self._fetch_login_state(timeout=timeout, want_storage=False)
        return cookies

    async def _fetch_login_state(
        self, timeout: float = 10.0, storage_timeout: float = 3.5, want_storage: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """取 cookie + Web Storage。

        两半分开发：cookie 到达即返回给调用方逻辑，storage 在独立预算内再等（扩展先回 cookie
        再采集 storage，卡住的 tab 不会拖垮 cookie）。扩展不支持 storage（storageProto < 2）时
        不请求、不等待，保持旧扩展兼容。
        """
        if self._ext_ws is None or self._ext_ws.closed:
            raise RuntimeError("扩展未连接（请确认日常浏览器已加载 research-assistant 扩展且 daemon 在线）")
        async with self._lock:
            if self._pending is not None:
                raise RuntimeError("已有进行中的登录态请求")
            loop = asyncio.get_event_loop()
            self._pending = loop.create_future()
            storage_fut: asyncio.Future | None = None
            if want_storage and self._storage_proto >= 2:
                storage_fut = loop.create_future()
                self._pending_storage = storage_fut
            req: dict[str, Any] = {"type": "getCookies", "reqId": secrets.token_hex(4)}
            if storage_fut is not None:
                req["wantStorage"] = True
            try:
                await self._ext_ws.send_str(json.dumps(req))
                try:
                    cookies = await asyncio.wait_for(self._pending, timeout=timeout)
                except asyncio.TimeoutError:
                    if not self._pending.done():
                        self._pending.set_exception(RuntimeError("扩展响应超时"))
                    raise
                storage: dict[str, Any] = {}
                if storage_fut is not None:
                    try:
                        storage = await asyncio.wait_for(storage_fut, timeout=storage_timeout)
                    except asyncio.TimeoutError:
                        # storage 超时只降低登录态完整度，cookie 已拿到，不判失败
                        storage = {"origins": {}, "error": "storage_timeout"}
                return cookies, storage
            finally:
                # 所有退出路径(成功/超时/send 失败)都清占位,否则泄漏会让后续
                # /cookies 永久报「已有进行中的请求」直到 daemon 重启
                self._pending = None
                if self._pending_storage is not None and not self._pending_storage.done():
                    self._pending_storage.cancel()
                self._pending_storage = None

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_get("/status", self.status_handler)
        app.router.add_get("/cookies", self.cookies_handler)
        # ping 循环用 aiohttp 规范的 async startup/cleanup 生命周期管理
        app.on_startup.append(self._start_ping)
        app.on_cleanup.append(self._stop_ping)
        return app

    async def _start_ping(self, app: web.Application) -> None:
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def _stop_ping(self, app: web.Application) -> None:
        task = getattr(self, "_ping_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def run(port: int) -> None:
    """启动 daemon（前台运行；由 daemonctl.ensure_daemon 以子进程拉起）。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [daemon] %(message)s",
        stream=sys.stderr,
    )
    daemon = CookieDaemon(port=port)
    web.run_app(daemon.build_app(), host="127.0.0.1", port=port, print=lambda *a: None)
