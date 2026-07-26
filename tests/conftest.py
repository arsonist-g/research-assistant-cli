"""共享 pytest fixture。

测试隔离要点：
- proxy 模块有进程级 _override 全局，跨用例会泄漏 → autouse 重置。
- config 真实读取 ~/.research-assistant → 用 RESEARCH_ASSISTANT_HOME 指向临时目录。
- 真实环境可能带 RA_/HTTPS_PROXY 变量 → clean_env 清空。
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _reset_proxy_override() -> None:
    """每个用例前后清空 proxy 模块级覆盖（--proxy 缓存），避免跨用例泄漏。"""
    from research_assistant import proxy

    proxy._override = None
    yield
    proxy._override = None


@pytest.fixture
def clean_env(monkeypatch):
    """清掉所有 RA_* 与代理相关环境变量，确保用例不被真实环境干扰。返回 monkeypatch。"""
    for k in list(os.environ):
        if k.startswith("RA_"):
            monkeypatch.delenv(k, raising=False)
    for k in (
        "RESEARCH_ASSISTANT_HOME",
        "HTTPS_PROXY", "https_proxy",
        "HTTP_PROXY", "http_proxy",
        "ALL_PROXY", "all_proxy",
    ):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


@pytest.fixture
def home(tmp_path, monkeypatch):
    """隔离的配置根目录（RESEARCH_ASSISTANT_HOME）。"""
    h = tmp_path / "ra-home"
    h.mkdir()
    monkeypatch.setenv("RESEARCH_ASSISTANT_HOME", str(h))
    return h
