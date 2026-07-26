# research-assistant

Concurrent search / fetch / locate CLI tools for AI agents. Fan out across many providers in
parallel, gather candidate sources and pages, then read them yourself to form the answer.

Read this in: [中文](README-ZH.md)

> **Middleware, not an oracle.** It collects sources. It does not write your conclusion. Every
> search returns candidate URLs, every fetch returns the page body; synthesis is your job.

## Why

Agents burn turns and context doing what one concurrent tool-set can do in a single call: search
across engines, fetch pages past Cloudflare, pull official docs, locate passages in long files.
`research-assistant` does all of that gathering in one CLI, and ships a `researcher` sub-agent
for large investigations that keeps the host context clean.

## Features

- **Aggregated multi-source search** — one `search` call fans out across `exa`, `tavily`,
  `firecrawl`, and a headless browser engine (Bing/Google), then merges and de-duplicates.
  **Zero-config works**: the browser source needs no API key and is always in the default set.
- **Cloudflare & anti-bot bypass** — `fetch` drives your **real** Edge/Chromium (headed, so
  Cloudflare does not flag it) and clicks through Turnstile. It auto-escalates: cheap API first,
  real browser only when the API fails.
- **One browser, many tabs** — fetching N URLs opens one browser process with N tabs, not N
  browsers. Search paginates concurrently with smart page-count inference; a cross-process
  instance cap prevents runaway browsers.
- **Login cookies bridged** — a bundled MV3 extension pipes your daily browser's login cookies
  into fetch, so logged-in pages work without re-entering credentials.
- **Official docs from the source** — `ctx7 docs` returns up-to-date library / SDK / CLI / cloud
  docs via Context7, faster and more accurate than grepping the open web.
- **Web-LLM `ask` and long-doc `locate`** — `ask` gets a natural-language answer with citations
  from a web-enabled LLM; `locate` pins which sections of a 10k-line file matter before you read.
- **Plugin providers** — adding a provider is one module plus one config entry; the registry
  auto-discovers it and it appears in `--help`. No router or CLI changes.
- **Scriptable** — JSON on stdout by default (`--output markdown` for humans); semantic exit
  codes (`0` ok · `1` internal · `2` args · `3` config · `4` network · `5` antibot). Installs as
  a skill into Claude Code, Codex, and friends.

## Quick start

```sh
uv venv .venv
uv pip install -e . --python .venv/Scripts/python.exe
research-assistant setup                 # configure providers + install skill/agent (optional)
research-assistant search "python asyncio" --limit 10
research-assistant fetch https://example.com
```

No provider keys? `search` and `fetch` still work: the browser source and Firecrawl's keyless
tier need none.

## Commands

```sh
# Search
research-assistant search "<q>" [--providers exa,tavily,firecrawl,browser]   # aggregated
research-assistant browser search "<q>" [--engine bing-cn|bing-intl|google]  # direct browser engine
research-assistant exa|tavily search "<q>"                                   # structured APIs

# Fetch
research-assistant fetch <url> [<url>...]      # API, then real browser + CF bypass (auto)
research-assistant browser fetch <url>...      # straight to the real browser
research-assistant firecrawl scrape <url>...   # keyless markdown

# Docs & Q&A
research-assistant ctx7 docs /org/repo "<q>"   # official docs via Context7
research-assistant ask "<question>"            # web-LLM answer + citations

# Long docs
research-assistant locate <md_path> "<q>"      # anchor-based relevance scan

# Management
research-assistant setup                       # configure + install skill/agent
research-assistant config fields               # show each provider's required config fields
research-assistant doctor [--show-config]      # connectivity diagnostics
research-assistant skills status               # managed skill/agent freshness
```

Run `research-assistant --help` for the full list; every command also takes `-h`.

## Configuration

Config lives at `~/.research-assistant/config.toml` (env overrides via `RA_<TYPE>_<FIELD>`):

```toml
schema_version = 1

[[provider]]
type     = "exa"
api_key  = "..."
base_url = "https://api.exa.ai"

[[provider]]
type     = "openai_compat"          # drives `ask`
base_url = "https://api.openai.com/v1"  # default; for a compatible gateway, use its URL with /v1
api_key  = "..."
model    = "grok-..."

[proxy]
url = ""                            # empty = auto-detect system proxy

[browser]
channel               = "msedge"    # msedge | chrome
max_browser_instances = 3           # concurrent browser-process cap (cross-CLI)
```

Unconfigured providers are simply skipped. `firecrawl scrape` / `search` and the browser source
are keyless, so `search` and `fetch` work with an empty config.

## How it works

Each command is a thin async wrapper over a provider plugin. `search` fans out across providers
and merges; `fetch` tries the cheap API then escalates to a real browser with Cloudflare bypass;
`locate` chunks a document and scores chunks with a small model. Output is JSON by default so the
host agent can parse it, with a markdown mode for humans.

For large investigations, dispatch the `researcher` sub-agent: it runs the full
search → fetch → locate workflow in isolation, writes a report to disk, and returns only a
summary, so the heavy reading never pollutes the host context.

## License

MIT
