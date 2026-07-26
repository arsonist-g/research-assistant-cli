---
name: research-assistant
description: Search the open web, fetch official library docs, pull page content past logins or anti-bot challenges, and locate passages in long documents. This skill is a set of concurrent CLI tools that gather candidate sources and pages; it does not synthesize answers. It covers web search for sources, library/framework/SDK/CLI/cloud docs lookup, fetching pages behind Cloudflare or a login, and locating where a topic sits in a long document.
---

# research-assistant

`research-assistant` is a set of concurrent CLI atomic tools for gathering external
information: web search providers, official library docs, cross-tool page fetch, and
anchor-based locate for long local documents. It is **middleware**. It collects sources and
pages; you read them and synthesize your own answer.

## Output boundary

These tools gather sources and pages. They do not synthesize answers, do not rank which source
to trust when sources disagree, and do not decide correctness. You read what they return and
draw your own conclusions.

## How to read this skill

The Capability map and the Commands list describe what each tool is good at and when to use
it. Treat those as guidance for picking a tool, not as a fixed routing order. The tier numbers
(Tier 1/2/3) express a preference for the faster, cheaper tool first, not a hard rule: if you
already know a page needs a browser, go straight to `fetch`. Anything in a command's inline
comment is a strength hint, not a contract.

## Primary tools: fetch, search, ask

Three commands do the heavy lifting and need no configuration to start. Reach for them first.

### `fetch <url>...`: cross-tool page fetch with automatic escalation

`fetch` tries the cheap path first and climbs only on failure: the normal APIs (tavily extract,
firecrawl scrape), then a headed real browser with Cloudflare auto-detect. Login cookies are
injected through the bundled extension bridge, so logged-in pages work without re-entering
credentials. It works out of the box: with zero provider config the normal APIs simply fail and
the browser fallback still returns the page.

### `search "<query>"`: aggregated multi-source search, browser included by default

`search` fans the query across the configured search APIs (exa, tavily) and the browser search
engine, then merges and de-duplicates the candidates. One source failing does not block the
others. The browser source is always in the default set, so `search` works with zero provider
config: with nothing configured it still runs the headless browser search engine and returns
candidates. Pass `--providers exa,tavily` to restrict to the fast APIs only.

### When to reach for browser search

Reach for `browser search`, or the browser source inside `search`, when the APIs come back empty
or shallow. It is slower than the APIs, but it runs against the user's real Edge/Chromium and
searches the way a person does, so sites that hide behind strong anti-bot become reachable, and
those sites usually hide because their content is worth protecting. It carries the user's login
cookies and search history, so the results track their habits and the search engine's
personalization model. When exa and tavily give you nothing useful on a niche or protected topic,
fall back to browser search before you give up. It runs headless, so it does not steal focus from
the user's daily browser.

### `ask "<question>"`: natural-language answer from a web-enabled LLM

`ask` sends your question to the configured `openai_compat` LLM (pick a model with built-in web
search) and returns its markdown answer plus an extracted citations list. Use it for a direct
answer to a small question. Treat the prose as a lead, not a fact: for anything that matters,
`fetch` the cited page and read it yourself.

## Capability map (pick by what the task needs; no hardcoded routing)

Four independent domains. Each tool has a distinct strength; choose per task, not by a fixed
order. Tool selection is YOUR decision.

### A. Web search and page fetch, tiered by anti-bot strength

When you need the open web, prefer the **lowest tier that can do the job**. API tools are
fastest because they skip browser rendering, and they cost the least. Escalate only when a
tier genuinely cannot return the content.

| Tier | Tool | Strength | Use when |
|------|------|----------|----------|
| 1. API | `exa`, `tavily` | Structured search APIs; clean JSON, no rendering. `tavily extract` returns ready markdown, `tavily map`/`crawl` cover a whole site; `exa` adds `similar`, `contents`, `answer`, and async `research`. Fast and cheap. | Simple or public pages; search with snippets; fast fetch of a known URL; site map or crawl. |
| 2. Firecrawl | `firecrawl` | Residential or home-bandwidth infrastructure; `scrape` and `search` are **keyless**, the other endpoints (`map`, `crawl`, `extract`, `interact`, `agent`, `monitor`, `parse`) need a key. More likely than Tier 1 to succeed on lightly protected pages, and the only tier with site-wide crawl and structured extraction. | A page refuses raw HTTP but shows no hard Cloudflare challenge. Batch scrape many URLs, crawl a whole site, or extract structured data with a schema. |
| 3. Browser | `fetch`, `browser` | Drives the user's real Edge/Chromium, same engine as the daily browser so login cookies stay valid. `fetch` auto-escalates: normal API, then a headed browser with Cloudflare auto-detect (no challenge means solve returns instantly). `browser` is the direct browser platform when you already know you want the browser: `browser fetch` (headed + CF auto-detect, skips the API) and `browser search` (headless search engine: Bing CN/intl, Google). | Everything above failed; or the page is behind Cloudflare, needs JS, or needs login cookies; or you want a browser search engine directly. |

Notes:
- Escalation is **automatic inside `fetch`**: normal API, then a headed browser with CF
  auto-detect (no CF challenge means solve returns instantly). You usually just call `fetch`
  and let it climb.
- `browser` is the direct browser platform, used when you already know the page needs a
  browser: `browser fetch` skips the API and goes straight to the headed browser with CF
  auto-detect; `browser search` runs a headless search engine (default Bing international,
  since CN Bing filters some sensitive terms).
- `fetch` and `browser fetch` inject login cookies through the bundled MV3 extension bridge,
  so logged-in pages work without re-entering credentials.
- The Cloudflare bypass runs on DrissionPage against the user's real browser. If that runtime
  dependency is missing, `fetch` and `browser fetch` report a clear install hint (exit code 5,
  antibot) instead of silently failing.

### B. Official docs, Context7 (`ctx7`), an independent domain

`ctx7 library "<name>" "<question>"` resolves a library id. `ctx7 docs /org/repo
"<question>"` returns up-to-date official docs as markdown. It hits the authoritative source
directly, so it is fast and accurate. On Windows git bash / WSL, MSYS rewrites the leading `/`
of `/org/repo` into a Windows path; write `//org/repo` there (PowerShell and cmd are unaffected).

**For any library, framework, SDK, CLI, or cloud-service question, prefer `ctx7` over web
search.** Do not grep the open web for API syntax, config keys, or version-migration notes
that Context7 can serve from the source.

### C. Ask a web-enabled LLM (`ask`), natural-language Q&A, LOW trust on the answer

`ask "<question>"` sends your natural-language question to the configured `openai_compat`
LLM (model must have built-in web search). It returns the model's markdown answer with
inline source links, plus an extracted citations list. Use it for a direct answer to a small
question; this differs from `search`, which aggregates the exa/tavily search APIs and returns
only URLs.

Why low trust on the answer: the LLM is great for **breadth** and for fuzzy or niche
questions, but its synthesized prose is unreliable. Expect context pollution, hallucinated
URLs, and stale facts. Treat the answer as a lead; for any actual fact, `fetch` the cited
page and read it yourself.

### D. Long local documents, `locate`

`locate <md_path> "<query>"` does anchor-based relevance scanning: it chunks the document,
scores chunks with a small model, then matches code strings back to exact line numbers. Use
it to **pre-screen which parts of a long page or doc matter** before reading, so you avoid
blind full reads of 10k-line files.

## Core principle: consume sources, do not inherit synthesis

Every search and locate tool returns **candidate sources or locations**, not a conclusion to
trust verbatim. Read the fetched sources yourself and synthesize your own answer. Never
fabricate a source you did not fetch.

## Use high concurrency; this is the point of the toolkit

These tools are cheap and independent. Fire many in parallel; do not serialize.

- **One question, many sources at once.** In a single turn, issue `exa search`, `tavily
  search`, `ctx7 docs`, and `ask` together (parallel tool calls); merge and de-duplicate
  the candidate URLs, then `fetch` the best ones. This is far faster than search, read,
  search, read.
- **Batch fetch.** `fetch url1 url2 url3` fetches all concurrently (one browser, many pages,
  `--concurrency N`). `firecrawl scrape u1 u2 u3` batches too. Never loop one URL at a time
  when a batch call exists.
- **Many pages, one locate.** Run `locate` once on the whole document to pin the relevant
  sections, then read only those.

Reserve serial work for genuine dependencies (you need URL X's content to formulate the next
query). Otherwise, fan out.

## Commands (JSON on stdout by default; `--output markdown` for readable markdown)

Pick a tool by what the task needs. Each command's comment states its strength and when to
use it. Async endpoints (crawl, extract, research, agent) block until done or `--poll-timeout`,
and accept `--async-submit` to return the job id immediately instead of waiting.

### A. Web search and page fetch, escalate only when the lower tier fails

```sh
# Tier 1: structured search APIs. Clean JSON, fastest, cheapest. Start here for simple or public pages.
research-assistant exa search "<query>" [--num-results N] [--type auto|keyword|neural|fast] [--text] [--highlights] [--category CAT] [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD] [--include-domains D ...] [--exclude-domains D ...]
research-assistant exa similar <url> [--num-results N]              # pages semantically similar to a URL
research-assistant exa contents <id>... [--text] [--highlights]     # fetch body for Exa IDs or URLs
research-assistant exa answer "<question>" [--text]                 # LLM answer grounded in Exa results
research-assistant exa research "<instructions>" [--model exa-research|exa-research-pro] [--output-schema JSON | --infer-schema] [--poll-timeout N] [--async-submit]   # async in-depth research

research-assistant tavily search "<query>" [--depth ultra-fast|fast|basic|advanced] [--max-results N] [--topic general|news|finance] [--time-range day|week|month|year] [--include-answer basic|true|advanced] [--country C] [--start-date YYYY-MM-DD] [--end-date YYYY-MM-DD] [--chunks-per-source N] [--include-raw-content markdown|text] [--include-images] [--include-domains D ...] [--exclude-domains D ...] [--auto-parameters]
research-assistant tavily extract <url>... [--extract-depth basic|advanced] [--format markdown|text]   # ready markdown, no rendering
research-assistant tavily map <url> [--max-depth N] [--max-breadth N] [--limit N] [--select-paths REGEX ...] [--instructions TEXT] [--timeout N]   # list a site's reachable URLs
research-assistant tavily crawl <url> [--max-depth N] [--limit N] [--instructions TEXT] [--extract-depth basic|advanced] [--no-external] [--timeout N]   # recursive crawl, markdown per page

# Tier 2: Firecrawl. Residential bandwidth. scrape and search are keyless; the rest need a key.
# Keyless endpoints are sensitive to IP quality: residential/home IPs work; datacenter IPs may
# hit 403 ("IP looks suspicious") and need a home-bandwidth proxy.
research-assistant firecrawl scrape <url>... [--format markdown|html] [--only-main-content] [--wait-for MS]   # batch markdown, keyless
research-assistant firecrawl search "<query>" [--limit N] [--sources web|news] [--scrape]                      # keyless; --scrape returns full markdown per result
research-assistant firecrawl map <url> [--limit N] [--include-subdomains]                                      # needs key
research-assistant firecrawl crawl <url> [--limit N] [--max-depth N] [--include-paths REGEX ...] [--allow-subdomains] [--prompt TEXT] [--poll-timeout N] [--async-submit]   # async recursive crawl
research-assistant firecrawl extract <url>... [--prompt TEXT | --schema JSON] [--enable-web-search] [--agent] [--poll-timeout N] [--async-submit]   # LLM structured extraction, async
research-assistant firecrawl interact "<prompt>" [--scrape-id ID | --url URL] [--code TEXT --language node|python|bash] [--timeout S] [--stop]   # live browser session bound to a scrape
research-assistant firecrawl agent "<prompt>" [--model spark-1-mini|spark-1-pro] [--urls U ...] [--schema JSON] [--max-credits N] [--strict] [--poll-timeout N] [--async-submit]   # autonomous extraction
research-assistant firecrawl monitor                                                                       # list active crawl jobs
research-assistant firecrawl parse <file> [--format markdown|html|json ...] [--pdf-mode fast|auto|ocr] [--max-pages N]   # parse a local document

# Tier 3: real browser. fetch auto-escalates (normal API, then headed browser with CF auto-detect).
research-assistant fetch <url> [<url>...] [--concurrency N] [--login|--no-login] [--no-browser] [--output PATH]

# browser platform: direct browser, no API attempt. fetch = headed + CF auto-detect; search = headless engine.
research-assistant browser fetch <url>... [--no-login] [--concurrency N] [--write PATH]   # skip the API, straight to the headed browser (--write, not global --output)
research-assistant browser search "<query>" [--engine bing-cn|bing-intl|google] [--limit N] [--max-pages N]   # headless search engine (default bing-intl)

# Aggregate the search sources in one call (default: configured exa,tavily + browser; browser is
# always in the default set and works with zero config). Keywords, not an LLM.
research-assistant search "<query>" [--providers exa,tavily,firecrawl,browser] [--limit N]
```

### B. Official docs, Context7 (authoritative; prefer over web search for library/SDK/CLI/cloud)

```sh
research-assistant ctx7 library "<name>" "<question>"       # resolve a library id
research-assistant ctx7 docs /org/repo "<question>"         # up-to-date official docs (git bash/WSL: use //org/repo)
```

### C. Ask a web-enabled LLM in natural language (`ask`)

`ask` sends your question to the configured `openai_compat` provider (config `[providers.openai_compat]`: base_url, api_key, model). Pick a model with built-in web search; the CLI sends a plain chat request and the model searches on its own, returning a markdown answer with inline source links. The command returns that markdown plus an extracted citations list. Use it for a direct answer to a small question; to gather URLs from the search APIs instead, use `search`.

```sh
research-assistant ask "<natural-language question>" [--system TEXT]
```

### D. Long-doc locating, pre-screen which parts of a 10k-line file matter before reading

```sh
research-assistant locate <md_path> "<query>" [--top N] [--scope lines|paragraph] [--context N] [--concurrency N]
```

### Setup and management

```sh
research-assistant setup [--non-interactive | --config-inline TOML] [--provider TYPE[,k=v,...]] ... [--proxy URL] [--browser-channel msedge|chrome] [--daemon-port N] [--install-skills claude,codex,...]   # configure providers/proxy/browser; --install-skills writes the skill+agent files
research-assistant doctor [--show-config] [--target <name>]              # connectivity diagnostics; --show-config prints config (masked), --target checks one provider or builtin
research-assistant skills status [--targets claude,codex,...] [--skills-root PATH]   # managed skill/agent freshness
research-assistant skills update [--targets claude,codex,...] [--skills-root PATH]   # refresh managed files
```

### Global flags (allowed anywhere on the command line)

```sh
research-assistant [--config PATH] [--output json|markdown] [--proxy URL|none] [--verbose] [--version] <command> ...
```

Every command also takes `-h`/`--help`: `research-assistant --help` lists all commands; `research-assistant <command> [--subcommand] --help` shows that command's arguments.

## Research workflow (you decide the steps)

1. Fan out search and docs (`exa` + `tavily` + `ctx7` + `ask`) to get candidate sources.
2. `fetch` the most promising URLs in one batch to get clean local markdown.
3. For long pages, `locate` to pin relevant passages before reading.
4. Synthesize YOUR answer from what you actually fetched; cite URLs.

For large, multi-source investigations, delegate to the `researcher` sub-agent. It runs this
workflow in an isolated context, writes a full report to `tmp-doc/`, and returns only a
summary, keeping the main context clean.

## Output and errors

Output defaults to JSON on stdout (parseable). Pass `--output markdown` for human or AI
readable markdown. Errors always surface as
`{"error":{"code","message","provider?","details?"}}` on stdout, plus a one-line
`error: ...` on stderr, with exit codes:
`0` ok, `1` internal, `2` args, `3` config, `4` network, `5` antibot.
