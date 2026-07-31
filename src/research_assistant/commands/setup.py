"""setup 命令：配置 provider/代理/浏览器 + 可选安装 managed skill/agent（api-contract §2.3）。

交互式：逐项问答（key 用 getpass 遮蔽输入）。
非交互式：
    --config-inline "<toml>"              直接写整份 TOML
    --provider type[,k=v,k=v]...          重复添加/更新 provider
    --proxy <url> --browser-channel <c> --browser-executable <path> --daemon-port <n>
幂等：重复 setup 合并更新配置；重复 install 覆盖 managed 文件。
"""

from __future__ import annotations

import argparse
import getpass
from typing import Any

from ..config import Config, ProviderConfig, load, save
from ..errors import ArgsError, ResearchAssistantError
from .. import targets as targets_mod
from .. import installer
from ._provider_meta import PROVIDER_META

NAME = "setup"
ALIASES: list[str] = ["init"]
HELP = "Configure providers/proxy/browser and optionally install managed skill/agent."

# 已知 provider 类型 + 是否需要 model（从共享元数据派生，单源；setup 只用 needs_model/label/default_base）
_PROVIDER_TYPES: dict[str, dict[str, Any]] = {
    t: {"needs_model": m.needs_model, "label": m.label, "default_base": m.default_base}
    for t, m in PROVIDER_META.items()
}


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(NAME, aliases=ALIASES, help=HELP, description=HELP)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--non-interactive", action="store_true", help="Non-interactive (use flags/inline).")
    mode.add_argument("--config-inline", help="Write a full TOML config string (non-interactive).")
    p.add_argument(
        "--provider",
        action="append",
        metavar="TYPE[,k=v,...]",
        help="Add/update a provider, e.g. exa,api_key=... or openai_compat,base_url=...,api_key=...,model=...",
    )
    p.add_argument("--proxy", help="Proxy URL (empty=auto-detect; 'none' to clear).")
    p.add_argument("--browser-channel", choices=["msedge", "chrome"], help="Browser channel for fetch fallback.")
    p.add_argument("--browser-executable", help="Browser executable path (overrides channel auto-detect for non-standard install locations).")
    p.add_argument("--daemon-port", type=int, help="Cookie daemon port.")
    p.add_argument("--install-skills", help="Comma list of families to install (claude,codex,cursor,hermes,all).")
    p.add_argument("--skills-root", help="Override install root (default $HOME).")
    p.set_defaults(_handler=run)


async def run(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    inline = getattr(args, "config_inline", None)
    if inline:
        cfg = _config_from_inline(inline, config.path)
    elif getattr(args, "non_interactive", False):
        cfg = _config_from_flags(args, config)
    else:
        cfg = _interactive(config)

    saved_path = save(cfg, config.path)

    result: dict[str, Any] = {"config_path": str(saved_path), "providers": [p.type for p in cfg.providers]}

    # 可选：安装 managed skill/agent
    if args.install_skills:
        families = _parse_families(args.install_skills)
        root = args.skills_root
        install_result = installer.install(families, root=root)
        result["install"] = install_result
        # 装了扩展桥时标记 browser.extension_status
        if install_result.get("extension_copied"):
            cfg.browser.extension_status = "installed"
            save(cfg, config.path)

    return result


# ---------------------------------------------------------------------------
# 非交互式
# ---------------------------------------------------------------------------


def _config_from_inline(toml_str: str, current_path: str) -> Config:
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover
        import tomli as tomllib
    try:
        raw = tomllib.loads(toml_str)
    except Exception as e:
        raise ArgsError(f"setup: --config-inline TOML 解析失败: {e}") from e
    # 复用 config.load 的解析逻辑：写临时再加载
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(toml_str)
        tmp = f.name
    try:
        return load(tmp)
    finally:
        try:
            import os

            os.unlink(tmp)
        except OSError:
            pass


def _config_from_flags(args: argparse.Namespace, current: Config) -> Config:
    cfg = current  # 在现有基础上合并（幂等）
    if args.provider:
        for spec in args.provider:
            pc = _parse_provider_spec(spec)
            _upsert_provider(cfg, pc)
    if args.proxy is not None:
        cfg.proxy.url = "" if args.proxy.lower() in ("none", "") else args.proxy
    if args.browser_channel:
        cfg.browser.channel = args.browser_channel
    if args.browser_executable is not None:
        cfg.browser.executable_path = args.browser_executable
    if args.daemon_port:
        cfg.browser.daemon_port = args.daemon_port
    return cfg


def _parse_provider_spec(spec: str) -> ProviderConfig:
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        raise ArgsError("setup: --provider 缺少 type")
    ptype = parts[0]
    if ptype not in _PROVIDER_TYPES:
        raise ArgsError(f"setup: 未知 provider type '{ptype}'")
    kv: dict[str, str] = {}
    for token in parts[1:]:
        if "=" not in token:
            raise ArgsError(f"setup: --provider 错误的 k=v 片段 '{token}'")
        k, v = token.split("=", 1)
        kv[k.strip()] = v.strip()
    return ProviderConfig(
        type=ptype,
        base_url=kv.get("base_url", _PROVIDER_TYPES[ptype]["default_base"] or ""),
        api_key=kv.get("api_key", ""),
        model=kv.get("model", ""),
        timeout=int(kv.get("timeout", "30")),
        concurrency=int(kv.get("concurrency", "8")),
    )


def _upsert_provider(cfg: Config, pc: ProviderConfig) -> None:
    for i, existing in enumerate(cfg.providers):
        if existing.type == pc.type:
            cfg.providers[i] = pc
            return
    cfg.providers.append(pc)


def _parse_families(raw: str) -> list[str]:
    tokens = [t.strip().lower() for t in raw.replace(";", ",").split(",") if t.strip()]
    if not tokens:
        return []
    out: list[str] = []
    for t in tokens:
        if t in ("all", "全部"):
            return list(targets_mod.ALL_FAMILIES.keys())
        if t in targets_mod.ALL_FAMILIES:
            out.append(t)
        else:
            raise ArgsError(f"setup: 未知 install 目标 '{t}'（可选: {','.join(targets_mod.ALL_FAMILIES)}）")
    # 去重保序
    seen, dedup = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            dedup.append(t)
    return dedup


# ---------------------------------------------------------------------------
# 交互式
# ---------------------------------------------------------------------------


def _interactive(current: Config) -> Config:
    cfg = current
    print("\nresearch-assistant setup（交互式，回车保留当前值）\n")
    for ptype, meta in _PROVIDER_TYPES.items():
        existing = next((p for p in cfg.providers if p.type == ptype), None)
        if _ask_enable(meta["label"], existing is not None):
            pc = _prompt_provider(ptype, meta, existing)
            _upsert_provider(cfg, pc)
    # 代理
    cur = cfg.proxy.url or "(空=自动检测)"
    val = input(f"\n代理 URL [{cur}]: ").strip()
    if val:
        cfg.proxy.url = "" if val.lower() in ("none", "无") else val
    # 浏览器
    cur_ch = cfg.browser.channel
    ch = input(f"浏览器 channel (msedge|chrome) [{cur_ch}]: ").strip().lower()
    if ch in ("msedge", "chrome"):
        cfg.browser.channel = ch
    cur_exe = cfg.browser.executable_path
    exe = input(f"浏览器可执行路径 (空=按 channel 探测) [{cur_exe or '自动'}]: ").strip()
    if exe:
        cfg.browser.executable_path = exe
    cur_port = cfg.browser.daemon_port
    port = input(f"cookie daemon 端口 [{cur_port}]: ").strip()
    if port.isdigit():
        cfg.browser.daemon_port = int(port)
    return cfg


def _ask_enable(label: str, currently_enabled: bool) -> bool:
    default = "y" if currently_enabled else "n"
    ans = input(f"\n配置 {label}? [y/N] ({default}): ").strip().lower()
    if not ans:
        return currently_enabled
    return ans in ("y", "yes", "是")


def _prompt_provider(ptype: str, meta: dict[str, Any], existing: ProviderConfig | None) -> ProviderConfig:
    cur_base = existing.base_url if existing else meta["default_base"]
    cur_key = existing.api_key if existing else ""
    cur_model = existing.model if existing else ""
    cur_timeout = existing.timeout if existing else 30

    base = input(f"  base_url [{cur_base or '必填'}]: ").strip() or cur_base
    key = getpass.getpass(f"  api_key [{'已配置' if cur_key else '必填'}]: ").strip() or cur_key
    model = ""
    if meta["needs_model"]:
        model = input(f"  model [{cur_model or '必填'}]: ").strip() or cur_model
    timeout_s = input(f"  timeout 秒 [{cur_timeout}]: ").strip()
    timeout = int(timeout_s) if timeout_s.isdigit() else cur_timeout
    concurrency = 8
    if ptype == "locate":
        cc = input(f"  concurrency [{existing.concurrency if existing else 8}]: ").strip()
        if cc.isdigit():
            concurrency = int(cc)
        elif existing:
            concurrency = existing.concurrency

    return ProviderConfig(
        type=ptype,
        base_url=base,
        api_key=key,
        model=model,
        timeout=timeout,
        concurrency=concurrency,
    )
