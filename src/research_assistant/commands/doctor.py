"""doctor 命令：连通性诊断（api-contract §2.3 / 场景 E）。

    research-assistant doctor [--show-config] [--target <name>]

对每个已配置 provider 做最小请求；locate 模型 ping；浏览器+cookie daemon 探测。
--show-config 展示配置（密钥遮蔽）。退出码 0=诊断完成（可达性体现在 report）。
响应：{checks[]{target, reachable, latency_ms, error?}, config_complete, config?}
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from .. import providers as providers_pkg
from ..config import Config, mask_secret
from ..errors import ResearchAssistantError
from ..providers import openai_compat

NAME = "doctor"
ALIASES: list[str] = ["diag"]
HELP = "Run connectivity diagnostics across configured providers."


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(NAME, aliases=ALIASES, help=HELP, description=HELP)
    p.add_argument("--show-config", action="store_true", help="Show config (secrets masked).")
    p.add_argument("--target", help="Only check a single provider/builtin target.")
    p.set_defaults(_handler=run)


async def run(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    configured_types = {p.type for p in config.providers}

    targets: list[str] = []
    if args.target:
        targets = [args.target]
    else:
        # 已配置的 provider + 浏览器；firecrawl 免 key 层永远可用，始终检查
        configured = set(configured_types)
        targets = [t for t in ("openai_compat", "locate", "exa", "tavily", "context7") if t in configured]
        targets.append("firecrawl")
        targets.append("browser")

    for target in targets:
        checks.append(await _check(target, config))

    config_complete = _config_complete(config)
    result: dict[str, Any] = {
        "checks": checks,
        "config_complete": config_complete,
        "commands": _commands_status(config),
    }
    if args.show_config:
        result["config"] = _masked_config(config)
    return result


def _config_complete(config: Config) -> bool:
    """配置是否足以做完整调研。

    搜索源：firecrawl 免 key 层永远可用，故搜索能力始终具备（exa/tavily/context7 是增强）。
    LLM：openai_compat（驱动 search）或 locate（驱动 locate）至少一个就绪才算完整。
    """
    pc = config.provider("openai_compat")
    has_llm = bool(pc and pc.api_key and pc.base_url and pc.model)
    lc = config.provider("locate")
    has_locate = bool(lc and lc.api_key and lc.base_url and lc.model)
    return has_llm or has_locate


# 命令 → 依赖 provider（单依赖命令；search/fetch 在 _commands_status 内特殊处理）。
# 这是 doctor 让 AI「跑完就知道哪些命令可用」的关键：未配置依赖的命令标 available=false + missing，
# AI 不必等调用时才在 provider 层撞到 ArgsError（典型场景：没配 openai_compat 却去用 ask）。
_SINGLE_DEP_COMMANDS: list[tuple[str, str]] = [
    ("ask", "openai_compat"),
    ("locate", "locate"),
    ("ctx7", "context7"),
    ("exa", "exa"),
    ("tavily", "tavily"),
    ("openai", "openai_compat"),
]


def _provider_ready(config: Config, ptype: str) -> bool:
    """provider 是否配置齐全可用。

    firecrawl 免 key 层永远可用；其余需 api_key（openai_compat/locate 这类 LLM 还需 model）。
    base_url 允许留空（OpenAI 兼容类有默认值，resolve_cfg 兜底）。
    """
    p = config.provider(ptype)
    if p is None:
        return False
    if ptype == "firecrawl":
        return True
    if not p.api_key:
        return False
    if ptype in ("openai_compat", "locate") and not p.model:
        return False
    return True


def _commands_status(config: Config) -> list[dict[str, Any]]:
    """各命令的可用性 + 缺失依赖，供 AI 在 doctor 阶段就判断能否调用某命令。"""
    out: list[dict[str, Any]] = []
    for name, dep in _SINGLE_DEP_COMMANDS:
        ready = _provider_ready(config, dep)
        item: dict[str, Any] = {"name": name, "available": ready, "requires": dep}
        if not ready:
            need = "api_key + model" if dep in ("openai_compat", "locate") else "api_key"
            item["missing"] = f"{dep} 未配置或不完整（需 {need}）"
        out.append(item)
    # search：任一搜索源可用；firecrawl 免 key 兜底 → 始终基本可用
    api_sources = [s for s in ("exa", "tavily", "context7") if _provider_ready(config, s)]
    sitem: dict[str, Any] = {"name": "search", "available": True, "requires": "exa|tavily|context7|firecrawl"}
    if not api_sources:
        sitem["note"] = "仅 firecrawl 免 key 层可用；配置 exa/tavily/context7 可增强搜索质量"
    out.append(sitem)
    # fetch：普通接口（firecrawl 兜底）总能试，浏览器回退看 channel 可执行是否存在
    from ..fetch import browser_probe

    fetch_ok, fetch_msg = browser_probe(config)
    fitem: dict[str, Any] = {"name": "fetch", "available": fetch_ok, "requires": "browser(普通接口失败时回退)"}
    if not fetch_ok:
        fitem["missing"] = fetch_msg
    out.append(fitem)
    return out


async def _check(target: str, config: Config) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        if target == "browser":
            ok, msg = await _check_browser(config)
        elif target == "openai_compat":
            await openai_compat.chat_completion(
                config, [{"role": "user", "content": "ping"}], provider_type="openai_compat"
            )
            ok, msg = True, "ok"
        elif target == "locate":
            await openai_compat.chat_completion(
                config, [{"role": "user", "content": "ping"}], provider_type="locate"
            )
            ok, msg = True, "ok"
        else:
            ok, msg = await _check_provider(target, config)
    except ResearchAssistantError as e:
        ok, msg = False, e.message
    except Exception as e:  # noqa: BLE001
        ok, msg = False, f"{type(e).__name__}: {e}"
    latency_ms = round((time.perf_counter() - start) * 1000, 1)
    item: dict[str, Any] = {"target": target, "reachable": ok, "latency_ms": latency_ms}
    if not ok:
        item["error"] = msg
    return item


async def _check_provider(ptype: str, config: Config) -> tuple[bool, str]:
    """用 provider 的最小能力做存活探测（复用插件实例与真实代码路径）。"""
    classes = providers_pkg.all_provider_classes()
    pclass = classes.get(ptype)
    if pclass is None:
        return False, f"provider '{ptype}' 未注册"
    provider = pclass(config)
    caps = provider.capabilities()
    # 选一个轻量能力：search / library / map
    preferred = next((c for c in caps if c.name in ("search", "library", "map")), None)
    if preferred is None:
        return False, f"{ptype}: 无可用探测能力"
    ns = _minimal_namespace(preferred.name)
    await preferred.handler(ns)
    return True, "ok"


def _minimal_namespace(cap_name: str) -> argparse.Namespace:
    """构造各 provider 最小查询所需的 args（带全部默认值，避免 AttributeError）。"""
    base = {
        "query": "test",
        "name": "test",
        "library_id": "/facebook/react",
        "num_results": 1,
        "max_results": 1,
        "limit": 1,
        "type": "auto",
        "depth": "basic",
        "topic": "general",
        "text": False,
        "highlights": False,
        "include_domains": None,
        "exclude_domains": None,
        "category": None,
        "start_date": None,
        "time_range": None,
        "include_answer": None,
        "scrape": False,
        "sources": None,
        "include_subdomains": False,
        "only_main_content": False,
        "wait_for": None,
        "extract_depth": "basic",
        "format": "markdown",
        "tokens": 200,
        "ids": [],
        "url": "https://example.com",
        "urls": ["https://example.com"],
        # search provider 完整字段：exa/tavily 等 handler 直接访问 ns.<attr>，缺就 AttributeError。
        # 这里给全 superset，doctor 测任何 provider 的 search 能力都不缺字段。
        "end_date": None,
        "include_text": None,
        "exclude_text": None,
        "include_raw_content": None,
        "chunks_per_source": None,
        "country": None,
        "days": None,
        "include_images": False,
        "include_image_descriptions": False,
        "include_favicon": False,
        "auto_parameters": False,
        "engine": "bing-intl",
        "max_pages": 10,
    }
    return argparse.Namespace(**base)


async def _check_browser(config: Config) -> tuple[bool, str]:
    """探测本地浏览器（Playwright channel）与 cookie daemon。"""
    # cookie daemon 状态（HTTP /status）
    daemon_msg = await _check_daemon(config)
    # 浏览器可启动性（仅探测 channel 可执行是否存在，不真启动以省时）
    from ..fetch import browser_probe

    browser_ok, browser_msg = browser_probe(config)
    msg = f"browser={browser_msg}; daemon={daemon_msg}"
    return browser_ok, msg


async def _check_daemon(config: Config) -> str:
    import aiohttp

    url = f"http://127.0.0.1:{config.browser.daemon_port}/status"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return f"在线(扩展已连={data.get('extConnected', False)})"
                return f"HTTP {resp.status}"
    except Exception:
        return "离线（未启动或扩展未装；fetch 时会按需拉起）"


def _masked_config(config: Config) -> dict[str, Any]:
    return {
        "config_path": config.path,
        "schema_version": config.schema_version,
        "providers": [
            {
                "type": p.type,
                "base_url": p.base_url,
                "api_key": mask_secret(p.api_key),
                "model": p.model,
                "timeout": p.timeout,
                **({"concurrency": p.concurrency} if p.type == "locate" else {}),
            }
            for p in config.providers
        ],
        "proxy": {"url": config.proxy.url or "(空=自动检测)"},
        "browser": {
            "channel": config.browser.channel,
            "executable_path": config.browser.executable_path or "(空=按 channel 探测)",
            "extension_status": config.browser.extension_status,
            "daemon_port": config.browser.daemon_port,
            "profile_strategy": config.browser.profile_strategy,
            "max_browser_instances": config.browser.max_browser_instances,
        },
    }
