"""provider 配置元数据（setup 交互式 + config fields 共享，单源）。

集中描述每个 provider type 在 config.toml 里该填什么：是否必填 api_key / model、
base_url 默认值、免 key 层说明、配置提示。setup 的 _PROVIDER_TYPES 从此派生，避免两处漂移。

字段：
    type         config.toml 的 type 键
    label        人类可读名
    needs_model  是否必填 model
    default_base base_url 默认值（空=用户必填）
    api_key      "required" | "optional"
    keyless      免 key 层说明（空=无免 key 层）
    notes        配置提示
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProviderMeta:
    type: str
    label: str
    needs_model: bool
    default_base: str
    api_key: str  # "required" | "optional"
    keyless: str = ""
    notes: str = ""


# 顺序即展示顺序（LLM 主干 → 搜索 API → 文档 → firecrawl）。
PROVIDER_META: dict[str, ProviderMeta] = {
    "openai_compat": ProviderMeta(
        type="openai_compat",
        label="OpenAI-compatible (drives ask)",
        needs_model=True,
        default_base="https://api.openai.com/v1",
        api_key="required",
        notes="base_url defaults to OpenAI (with /v1/). For a compatible gateway, set its URL including /v1/. Pick a model with built-in web search.",
    ),
    "locate": ProviderMeta(
        type="locate",
        label="locate model (small OpenAI-compatible model for long-doc locate)",
        needs_model=True,
        default_base="https://api.openai.com/v1",
        api_key="required",
        notes="Reuses the OpenAI-compatible protocol; a cheap small model is enough. base_url defaults to OpenAI; a gateway URL must include /v1/.",
    ),
    "exa": ProviderMeta(
        type="exa",
        label="Exa (structured search API)",
        needs_model=False,
        default_base="https://api.exa.ai",
        api_key="required",
    ),
    "tavily": ProviderMeta(
        type="tavily",
        label="Tavily (search + extract + map + crawl)",
        needs_model=False,
        default_base="https://api.tavily.com",
        api_key="required",
    ),
    "firecrawl": ProviderMeta(
        type="firecrawl",
        label="Firecrawl (keyless scrape/search/interact/parse; map/crawl/extract/agent/monitor need key)",
        needs_model=False,
        default_base="https://api.firecrawl.dev/v2",
        api_key="optional",
        keyless="scrape/search/interact/parse are keyless (IP-rate-limited); map/crawl/extract/agent/monitor need a key.",
        notes="Keyless commands default to auto (keyless first, fall back to key on rate-limit/IP failure). Configure a key to unlock map/crawl/extract/agent/monitor.",
    ),
    "context7": ProviderMeta(
        type="context7",
        label="Context7 (library/framework/SDK/CLI/cloud docs)",
        needs_model=False,
        default_base="https://context7.com",
        api_key="required",
    ),
}
