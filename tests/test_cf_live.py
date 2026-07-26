"""Cloudflare 绕过实机测试（live integration）。

默认跳过（marker `live_cf` + pyproject `addopts = "-m 'not live_cf'"`）。手动运行：

    .venv/Scripts/python.exe -m pytest tests/test_cf_live.py -m live_cf -s

测试站点（与原始 camoufox 参考代码一致，见 tmp-doc/cf_bypass_camoufox_original.py）：
  - https://nopecha.com/demo/cloudflare   Cloudflare 5 秒盾
  - https://nopecha.com/captcha/turnstile  Turnstile 验证

验证目标：headed Edge + playwright-stealth + cfbypass.solve 能真实通过 CF 挑战并取回内容。
走 fetch 的 CF 兜底完整路径 `_fetch_with_stealth`（含系统代理解析 + stealth + solve + html2md），
即用户实际用 `fetch <cf-url>` 时的链路。solve 失败会抛 AntibotError（测试据此失败并报详情）。
依赖：playwright-stealth 已装、本地 Edge/Chromium 可执行、外网（含系统代理）可达。
注意：headed 启动会弹出真实浏览器窗口（CF 识别 headless，必须 headed）。
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.live_cf]

from research_assistant.config import BrowserConfig, Config
from research_assistant.fetch import browser, cfbypass

CF_TEST_URLS = [
    "https://nopecha.com/demo/cloudflare",
    "https://nopecha.com/captcha/turnstile",
]


def _cfg() -> Config:
    # channel 默认 msedge（用户日常浏览器引擎，cookie 不跨内核，ADR-0002）
    return Config(browser=BrowserConfig(channel="msedge"))


def _skip_if_unusable() -> None:
    if not cfbypass.HAS_STEALTH:
        pytest.skip("playwright-stealth 未安装（pip install playwright-stealth）")
    if browser._channel_executable("msedge") is None:
        pytest.skip("本地未找到 msedge 浏览器可执行")


@pytest.mark.parametrize("cf_url", CF_TEST_URLS)
async def test_fetch_returns_content_behind_cloudflare(cf_url: str):
    """端到端：_fetch_with_stealth 对 CF 测试站应取回非空内容。

    _fetch_with_stealth 在 CF 挑战未通过时会抛 AntibotError——所以本测试拿到返回值
    即代表 stealth + solve 真实通过了 CF；拿不到则 pytest 直接展示 AntibotError 详情。
    """
    _skip_if_unusable()
    md = await browser._fetch_with_stealth(_cfg(), cf_url, cookies=[])
    # 返回即代表 solve 通过（_fetch_with_stealth 在 CF 未通过时抛 AntibotError）。
    # 不断言长度——某些 CF 测试页（如 Turnstile demo）本身正文极短，是页面真实内容而非被拦；
    # 被拦会拿到 "Just a moment..." 挑战页正文，故用"不含挑战特征"判定。
    assert md, f"CF 绕过后返回空内容: {cf_url}"
    assert "just a moment" not in md.lower(), f"内容仍含 CF 挑战特征，疑似被拦: {cf_url}"
