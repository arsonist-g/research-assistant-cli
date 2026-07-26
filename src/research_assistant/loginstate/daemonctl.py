"""登录态层：cookie daemon 控制 + cookie 获取（ADR-0006）。

- ensure_daemon(config): 若 daemon 未运行则以子进程拉起（与 fetch 同生命周期足够；这里是按需拉起）。
- daemon_status(config): GET /status。
- get_cookies(config): GET /cookies（若 daemon 未运行会自动拉起；扩展未连则抛错）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import aiohttp

from .. import config as config_mod
from . import daemon as daemon_mod


def _base_url(config: Any) -> str:
    port = config.browser.daemon_port
    return f"http://127.0.0.1:{port}"


async def _http_get(config: Any, path: str, timeout: float = 3.0) -> dict[str, Any] | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{_base_url(config)}{path}", timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
    except Exception:
        return None


async def daemon_status(config: Any) -> dict[str, Any] | None:
    return await _http_get(config, "/status")


async def ensure_daemon(config: Any) -> bool:
    """确保 daemon 在线；若离线则后台拉起子进程。返回最终是否在线。"""
    status = await daemon_status(config)
    if status is not None:
        return True
    _spawn_daemon(config)
    # 等待启动
    for _ in range(20):
        await asyncio.sleep(0.3)
        if await daemon_status(config) is not None:
            return True
    return False


def _spawn_daemon(config: Any) -> None:
    """以 detached 子进程启动 daemon（out/err 落 .logs/）。"""
    logs_dir = config_mod.logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "daemon.log"
    port = str(config.browser.daemon_port)
    # sys.executable + 模块入口
    import subprocess

    with log_file.open("a", encoding="utf-8") as f:
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            [sys.executable, "-m", "research_assistant.loginstate.daemon_cli", port],
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )


async def get_cookies(config: Any, *, ensure: bool = True, timeout: float = 12.0) -> list[dict[str, Any]]:
    """从 daemon 取明文 cookie 列表（扩展推送的）。"""
    if ensure:
        if not await ensure_daemon(config):
            raise RuntimeError("cookie daemon 无法启动")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_base_url(config)}/cookies", timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"daemon /cookies 返回 {resp.status}: {body}")
                data = await resp.json()
                return data.get("data") or []
    except aiohttp.ClientConnectorError as e:
        raise RuntimeError(f"cookie daemon 不可达（{e}）") from e


def to_playwright_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 chrome.cookies Cookie → Playwright add_cookies 格式。"""
    same_map = {"strict": "Strict", "lax": "Lax", "no_restriction": "None", "none": "None", "unspecified": "Lax"}
    out: list[dict[str, Any]] = []
    for c in cookies:
        item: dict[str, Any] = {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "path": c.get("path") or "/",
            "httpOnly": bool(c.get("httpOnly")),
            "secure": bool(c.get("secure")),
        }
        if c.get("expirationDate"):
            item["expires"] = float(c["expirationDate"])
        ss = c.get("sameSite")
        if ss and same_map.get(str(ss).lower()):
            item["sameSite"] = same_map[str(ss).lower()]
        out.append(item)
    return out
