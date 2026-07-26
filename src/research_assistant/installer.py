"""managed 文件安装器（因家异构，ADR-0008 / D7）。

- SKILL.md（bundled asset）相对通用，装各家 skill 目录。
- researcher agent 定义因家而异：Claude/Cursor=md+frontmatter、Codex=toml、Hermes=无（融入 skill）。
- 附带复制 MV3 扩展到 ~/.research-assistant/extension/（供用户在浏览器加载一次）。

幂等：重复 install 覆盖；status 对比 installed 与 bundled 内容判定 missing/stale/up-to-date/extra。
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from typing import Any

from .errors import ResearchAssistantError
from .targets import AGENT_NAME, ALL_FAMILIES, SKILL_NAME, Family

_ASSET_ROOT = ("assets",)
_PERSONA_RESOURCE = ("assets", "researcher_persona.md")
_SKILL_RESOURCE = ("assets", "skills", SKILL_NAME, "SKILL.md")


# ---------------------------------------------------------------------------
# asset 读取
# ---------------------------------------------------------------------------


def _read_text(resource_path: tuple[str, ...]) -> str:
    """优先 importlib.resources（打包后），失败回退文件系统（dev 从 src 运行）。"""
    try:
        root = resources.files("research_assistant")
        for part in resource_path:
            root = root.joinpath(part)
        return root.read_text(encoding="utf-8")
    except Exception:
        fs_root = Path(__file__).resolve().parents[1]
        p = fs_root.joinpath(*resource_path)
        return p.read_text(encoding="utf-8")


def persona_text() -> str:
    return _read_text(_PERSONA_RESOURCE)


def skill_text() -> str:
    return _read_text(_SKILL_RESOURCE)


def extension_source_dir() -> Path:
    """MV3 扩展源目录（bundled）。优先 importlib.resources，回退文件系统。"""
    try:
        from importlib import resources

        root = resources.files("research_assistant").joinpath("loginstate", "extension")
        if root.is_dir():
            with resources.as_file(root) as p:
                return Path(p)
    except Exception:
        pass
    fs_root = Path(__file__).resolve().parent  # src/research_assistant/
    p = fs_root / "loginstate" / "extension"
    if p.is_dir():
        return p
    raise ResearchAssistantError("MV3 扩展源未找到（loginstate/extension 缺失）")


# ---------------------------------------------------------------------------
# 因家生成 agent 定义
# ---------------------------------------------------------------------------


def generate_agent(family: Family) -> str | None:
    """按家生成 researcher agent 定义内容；Hermes 返回 None（无独立文件）。"""
    persona = persona_text()
    if family.agent_format == "none":
        return None
    if family.agent_format == "toml":
        return _generate_codex_toml(persona)
    return _generate_agent_md(family, persona)


def _frontmatter_line(value: Any) -> str:
    if isinstance(value, str) and ("\n" in value or ":" in value):
        # 多行/含冒号的值用引号包裹并转义内部引号（不用 f-string 内反斜杠，兼容 3.10）
        escaped = value.replace('"', '\\"')
        return '"' + escaped + '"'
    return str(value)


def _generate_agent_md(family: Family, persona: str) -> str:
    fields: dict[str, Any] = {
        "name": AGENT_NAME,
        "description": (
            "Use this sub-agent for research that needs external information (web search, docs, "
            "page fetching). It runs in isolation and returns only a summary; the full report is on disk."
        ),
    }
    fields.update(family.md_fields)
    fm_lines = ["---"]
    for k, v in fields.items():
        fm_lines.append(f"{k}: {_frontmatter_line(v)}")
    fm_lines.append("---")
    fm_lines.append("")
    return "\n".join(fm_lines) + persona


def _generate_codex_toml(persona: str) -> str:
    # Codex AgentRoleToml schema（openai/codex 源码确认，deepwiki 2026-07）：
    # 顶层合法字段：name / description / developer_instructions / model / nickname_candidates / config_file。
    # sandbox 经子表配置（[sandbox_workspace_write]），mcp_servers 属 config.toml 顶层 [mcp_servers] 表——
    # 二者均非 agent 文件顶层字段。此前误写的 sandbox_mode / mcp_servers 顶层键已移除，避免 codex 解析告警。
    # model 省略 → 继承 codex 全局 model 配置（不写非法的 "inherit" 字面值）。
    # developer_instructions 用三引号 toml 字符串承载 persona。
    return (
        f'name = "{AGENT_NAME}"\n'
        f'description = "Research sub-agent: web search, docs, page fetch. Runs in isolation, returns summary only."\n'
        f'developer_instructions = """\n{persona.strip()}\n"""\n'
    )


def skill_for_family(family: Family) -> str:
    """SKILL.md 内容。Hermes 把 researcher 人设融入（ADR-0008 D7）。"""
    base = skill_text()
    if family.name == "hermes":
        # Hermes 无独立 agent 文件，把 persona 附到 skill 末尾
        return base.rstrip() + "\n\n## researcher persona (runtime delegation)\n\n" + persona_text().strip() + "\n"
    return base


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def _home(root: str | Path | None) -> Path:
    if root:
        return Path(root).expanduser()
    return Path.home()


def skill_path(family: Family, root: str | Path | None = None) -> Path:
    return _home(root) / family.skill_relative / "SKILL.md"


def agent_path(family: Family, root: str | Path | None = None) -> Path | None:
    if family.agent_relative is None:
        return None
    return _home(root) / family.agent_relative


# ---------------------------------------------------------------------------
# install / status / update
# ---------------------------------------------------------------------------


def install(families: list[str], *, root: str | Path | None = None) -> dict[str, Any]:
    home = _home(root)
    installed: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for fname in families:
        family = ALL_FAMILIES[fname]
        try:
            sp = skill_path(family, home)
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(skill_for_family(family), encoding="utf-8")
            entry: dict[str, Any] = {
                "family": fname,
                "skill_path": str(sp),
            }
            ap = agent_path(family, home)
            if ap is not None:
                ap.parent.mkdir(parents=True, exist_ok=True)
                ap.write_text(generate_agent(family) or "", encoding="utf-8")
                entry["agent_path"] = str(ap)
                entry["agent_format"] = family.agent_format
            else:
                entry["agent_path"] = None
                entry["agent_format"] = "none"
            installed.append(entry)
        except OSError as e:
            failed.append({"family": fname, "error": str(e)})

    # 附带复制 MV3 扩展（登录态层，ADR-0006）
    extension_copied = False
    extension_dest = None
    try:
        extension_dest = _copy_extension()
        extension_copied = True
    except Exception as e:
        failed.append({"family": "extension", "error": str(e)})

    return {
        "root": str(home),
        "installed": installed,
        "failed": failed,
        "extension_copied": extension_copied,
        "extension_path": str(extension_dest) if extension_dest else None,
    }


def status(families: list[str], *, root: str | Path | None = None) -> dict[str, Any]:
    home = _home(root)
    out: list[dict[str, Any]] = []
    for fname in families:
        family = ALL_FAMILIES[fname]
        item = _status_family(family, home)
        item["family"] = fname
        out.append(item)
    counts: dict[str, int] = {}
    for it in out:
        s = it["skill_status"]
        counts[s] = counts.get(s, 0) + 1
    return {"root": str(home), "targets": out, "status_counts": counts}


def update(families: list[str], *, root: str | Path | None = None) -> dict[str, Any]:
    return install(families, root=root)


def _status_family(family: Family, home: Path) -> dict[str, Any]:
    sp = skill_path(family, home)
    bundled_skill = skill_for_family(family)
    skill_status = _compare(sp, bundled_skill)
    result: dict[str, Any] = {
        "skill_path": str(sp),
        "skill_status": skill_status,
    }
    ap = agent_path(family, home)
    if ap is not None:
        bundled_agent = generate_agent(family) or ""
        result["agent_path"] = str(ap)
        result["agent_status"] = _compare(ap, bundled_agent)
    else:
        result["agent_path"] = None
        result["agent_status"] = "none"
    return result


def _compare(path: Path, bundled: str) -> str:
    """对比 installed 与 bundled：missing/stale/up-to-date/extra。"""
    if not path.exists():
        return "missing"
    try:
        installed = path.read_text(encoding="utf-8")
    except OSError:
        return "missing"
    if installed == bundled:
        return "up-to-date"
    return "stale"


# ---------------------------------------------------------------------------
# 扩展复制
# ---------------------------------------------------------------------------


def _copy_extension() -> Path:
    """把 bundled MV3 扩展复制到 ~/.research-assistant/extension/。"""
    import shutil

    from . import config as config_mod

    src = extension_source_dir()
    dest = config_mod.config_dir() / "extension"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return dest
