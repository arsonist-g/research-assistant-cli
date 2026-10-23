# research-assistant

面向 AI agent 的并发 search / fetch / locate CLI 工具集。一次调用扇出到多个 provider 并行采集
候选信源和页面，然后由你自己阅读、得出答案。

阅读：[English](README.md)

> **中间件，不是神谕。** 它采集信源，不替你下结论。每次 search 返回候选 URL，每次 fetch 返回
> 页面正文；综合判断是你的事。

## 为什么需要

agent 常常耗费大量往返和上下文去做"一次并发工具集本来一次调用就能搞定"的事：跨引擎搜索、抓取
Cloudflare 后的页面、取官方文档、在长文件里定位段落。`research-assistant` 把这些采集工作收进
一个 CLI，并提供一个 `researcher` 子 agent 处理大型调研，保持主上下文干净。

## 特性

- **多源聚合搜索** — 一次 `search` 扇出到 `exa`、`tavily`、`firecrawl` 和无头浏览器引擎
  （必应 / 谷歌），合并去重。**零配置可用**：浏览器源不需要 API key，永远在默认集合里。
- **Cloudflare / 反爬绕过** — `fetch` 驱动你**真实的** Edge/Chromium 走**真无头**（`--headless=new`
  + 真实 UA 修复，不被 CF 识别），点击穿过 Turnstile。自动升级：先走便宜的 API，失败才上真实浏览器。
- **一个浏览器，多个 tab** — 抓 N 个 URL 只开一个浏览器进程 + N 个 tab，不是 N 个浏览器。搜索
  按首页结果数智能推断、并发翻页；跨进程实例上限防止浏览器失控。
- **登录态桥接** — 内置 MV3 扩展把日常浏览器的登录 cookie **与页面 Web Storage**
  （localStorage / sessionStorage）一起注入 fetch，登录页无需重输凭据；会话 token 不放在 cookie 的站点也能用。
- **官方文档直取** — `ctx7 docs` 经 Context7 返回最新的库 / SDK / CLI / 云文档，比搜开放网页更快
  更准。
- **web-LLM `ask` + 长文档 `locate`** — `ask` 用自带网页搜索的 LLM 回答自然语言问题并附引用；
  `locate` 在阅读前锁定 10k 行文件里哪些段落重要。
- **GitHub 走 API 而非爬页面** — `fetch` 自动接管 GitHub 文件 / 仓库 URL（`method: "github"`），
  改读 REST API：文件原文精确，仓库返回元数据 + 干净的 README。免 key 可用（公开仓库取文件零配额），
  填 token 解锁 5000 次/小时与私有仓库。`github file` / `github repo` 也可单独调用。
- **站点规则是你自己的数据，不是本项目代码** — 规则放 `~/.research-assistant/rules/*.toml`，
  本项目包里不带任何规则。规则负责 URL 层：匹配哪个地址、把它换成什么地址（加 `.json`、加参数、
  换 host），再引用一个响应适配器（`discourse_topic`、`json_path`、`css_extract`、`html_article`）
  和可选的 HTML 预处理器（`strip_hidden_nodes`、`remove_selectors`）。网站改版只改规则文件，不用给
  本项目提 commit。`rules validate/list/explain/test` 负责校验与回归；规则失败自动落回普通链路。
- **国内网络也走必应国际版** — 浏览器搜索一律请求 `www.bing.com`；若被网络 302 到
  `cn.bing.com`，自动追加 `ensearch=1`（必应的国际结果开关）重取，不接受污染严重的国内结果集。
  `bing-cn` 仅保留为兼容别名。
- **插件式 provider** — 加一个 provider = 一个模块 + 一条配置；注册表自动发现，出现在 `--help`。
  无需改路由或 CLI。
- **可脚本化** — stdout 默认 markdown（人/AI 友好、省 token）；`--output json` 供脚本/jq 解析。语义退出码（`0` 成功 · `1` 内部
  · `2` 参数 · `3` 配置 · `4` 网络 · `5` 反爬）。可作为 skill 装进 Claude Code、Codex 等。

## 站点规则

规则是**用户维护的数据**，不属于本包。把 TOML 文件放进 `~/.research-assistant/rules/`；
一条规则只描述 URL 层：

```toml
schema = 1

[[rule]]
id = "discourse-linuxdo-topic"
hosts = ["linux.do", "www.linux.do"]
path_patterns = [
  '^/t/(?P<id>\d+)(?:/(?P<post>\d+))?(?:\.json)?/?$',
  '^/t/[^/]+/(?P<id>\d+)(?:/(?P<post>\d+))?(?:\.json)?/?$',
]
transport = "browser"                  # browser | http | html（只做预处理）
request = "https://{host}/t/topic/{id}.json"
parser = "discourse_topic"             # 响应适配器
preprocess = [{ name = "strip_hidden_nodes" }]
```

- **URL 层**：`hosts` / `host_patterns`、`path_patterns`（命名捕获组）、`request` 模板、
  `transport`（`browser`/`http` 会改写并请求；`html` 只预处理原页面，忽略 `request`）、
  `login`、`headers`、`allow_cross_host`。
- **响应适配器**（代码提供、规则按名字引用）：`discourse_topic`（Discourse JSON）、
  `json_path`（简单 JSON 字段映射）、`css_extract`（CSS 选择器）、`html_article`（默认 trafilatura）。
- **HTML 预处理器**（代码提供、规则按名字引用）：`strip_hidden_nodes`（隐藏诱饵文本）、
  `remove_selectors`（按 CSS 选择器删节点）。

离线检查与回归：

```sh
research-assistant rules validate
research-assistant rules list
research-assistant rules explain "https://linux.do/t/topic/3066000/1"
research-assistant rules test
```

`fetch` 命中后返回 `method: "rule"` + `rule: "<id>"`；未命中、规则失败或适配器解析不了时，
自动落回普通 API / 浏览器链路。

## 安装

```sh
npm install -g research-assistant
```

需要 **Node ≥ 18** 和 **Python ≥ 3.10**（在 `PATH` 上）。包的 postinstall 会在包内建一个私有
Python 运行时并装齐依赖（有 `uv` 用 `uv`，否则回退到标准库 `venv` + `pip`）。不下载自带浏览器
——`fetch` 直接用本机已装的 Edge/Chrome。

```sh
research-assistant --version             # 确认 CLI 已在 PATH 上
research-assistant doctor                # 连通性 + 各命令可用性

research-assistant setup                 # 交互式配置 provider（key 输入有遮蔽）
research-assistant search "python asyncio" --limit 10
research-assistant fetch https://example.com
```

没有 provider key？`search` 和 `fetch` 仍能用：浏览器源和 Firecrawl 的免 key 层不需要。

### 装进各 agent 平台：skill + 子 agent

包里带两样受管内容：`research-assistant` skill（本 CLI 的操作手册）和 `researcher` 子 agent 定义
（在隔离上下文里跑完整的 search → fetch → locate 流程，报告写盘，只回摘要）。写入各平台的用户级目录：

```sh
research-assistant setup --install-skills all     # 配置 provider，然后安装
research-assistant skills update  --targets all   # 只安装/刷新（非交互）
research-assistant skills status  --targets all   # missing / stale / up-to-date
```

| 目标 | skill | agent |
|---|---|---|
| `claude` | `~/.claude/skills/research-assistant/SKILL.md` | `~/.claude/agents/researcher.md` |
| `cursor` | `~/.cursor/skills/research-assistant/SKILL.md` | `~/.cursor/agents/researcher.md` |
| `codex` | `~/.codex/skills/research-assistant/SKILL.md` | `~/.codex/agents/researcher.toml` |
| `pidesktop` | `~/.agents/skills/research-assistant/SKILL.md` | `~/.agents/subagents/researcher.md` |
| `hermes` | `~/.hermes/skills/research-assistant/SKILL.md` | —（人设并入 skill） |

Codex 使用专用 `~/.codex/skills/`；PI-Desktop 使用 `~/.agents/skills/`。可以只装指定平台（`--targets claude,codex`），
用 `--skills-root PATH` 换安装根目录。

要让 agent 调本 CLI 不再逐次弹审批：

```sh
research-assistant permissions install
```

### 桥接日常浏览器的登录 cookie（可选）

`fetch` 可以复用你**日常浏览器**的登录态，登录后的页面无需重输凭据。桥是一个 Manifest V3 扩展，
CLI 把它放在：

```
~/.research-assistant/extension/
```

（`--install-skills` 与 `skills update` 会把它复制到那里。）也可以从 [GitHub Releases](https://github.com/arsonist-g/research-assistant-cli/releases) 下载仅含扩展的 zip，解压后加载其中的目录，不需要克隆源码仓库。在你日常使用的 Edge / Chrome 配置里装一次：

1. 打开 `edge://extensions`（或 `chrome://extensions`），打开右上角**开发者模式**。
2. 点**加载解压缩的扩展**，选择上面的 `~/.research-assistant/extension/`，或 Release zip 的解压目录。
3. 用 `research-assistant doctor` 确认——扩展连上后其 daemon 行会显示 `extConnected=True`。
   daemon 是按需拉起的，若显示离线先跑一次 `fetch`。

不抓登录页就完全可以跳过；其它功能都不依赖它。

### 更新与卸载

```sh
npm update -g research-assistant
npm uninstall -g research-assistant   # 保留 ~/.research-assistant/（配置 + 扩展）
```

### 源码安装

开发时对着 checkout 跑：

```sh
uv venv .venv
uv pip install -e . --python .venv/Scripts/python.exe
```

## 命令

```sh
# 搜索
research-assistant search "<q>" [--providers exa,tavily,firecrawl,browser]   # 聚合
research-assistant browser search "<q>" [--engine bing-cn|bing-intl|google]  # 直连浏览器引擎
research-assistant exa|tavily search "<q>"                                   # 结构化 API

# 抓取
research-assistant fetch <url> [<url>...]      # API，然后真实浏览器 + CF 绕过（自动）
research-assistant browser fetch <url>...      # 直奔真实浏览器
research-assistant firecrawl scrape <url>...   # 免 key markdown

# 文档 & 问答
research-assistant ctx7 docs /org/repo "<q>"   # 官方文档（Context7）
research-assistant ask "<question>"            # web-LLM 回答 + 引用

# GitHub（走 REST API 而非爬 HTML；fetch 会自动接管这类 URL）
research-assistant github file <url>           # 文件原文；#L10-L20 片段只取该区间
research-assistant github repo <url>           # 仓库元数据 + README（2 次 API 调用，key 可省）

# 长文档
research-assistant locate <md_path> "<q>"      # 锚点式相关性扫描

# 站点规则（用户维护的 TOML，放 ~/.research-assistant/rules/；包内不带任何规则）
research-assistant rules validate              # schema / 正则 / 适配器 / 模板校验
research-assistant rules list                  # 已加载规则、来源、priority
research-assistant rules explain "<url>"       # 命中哪条规则、会请求什么地址
research-assistant rules test [--rule ID]      # 离线 fixture 回归

# 管理
research-assistant setup                       # 配置 + 安装 skill/agent
research-assistant config fields               # 查看各 provider 需填的配置字段
research-assistant doctor [--show-config]      # 连通性诊断
research-assistant skills status               # 受管 skill/agent 新鲜度
research-assistant permissions install         # 把本 CLI 写进各平台放行规则（免逐次审批）
research-assistant permissions status          # 各平台放行规则状态
```

完整列表见 `research-assistant --help`；每条命令也支持 `-h`。

`permissions install` 把 `research-assistant` 的放行规则**幂等合并**进各平台用户级配置（Claude Code
`~/.claude/settings.json`、Cursor `~/.cursor/permissions.json`、Gemini CLI `~/.gemini/settings.json`），
已有键和规则原样保留；装好后 agent 调本 CLI 不再逐次弹审批。Codex / Hermes 无命令级放行机制，
`permissions status` 会说明原因并给出指引。

## 配置

配置在 `~/.research-assistant/config.toml`（环境变量 `RA_<TYPE>_<FIELD>` 覆盖）：

```toml
schema_version = 1

[[provider]]
type     = "exa"
api_key  = "..."
base_url = "https://api.exa.ai"

[[provider]]
type     = "openai_compat"          # 驱动 `ask`
base_url = "https://api.openai.com/v1"  # 默认；用兼容渠道时换成其地址（需含 /v1）
api_key  = "..."
model    = "grok-..."

[[provider]]
type     = "github"                 # 可选：`github file` / `github repo` + fetch 自动接管
api_key  = "ghp_..."                # 可选；不填 = 匿名（60 次/小时，填了 5000 次/小时）
base_url = "https://api.github.com" # 默认；GitHub Enterprise 需填 https://HOST/api/v3

[proxy]
url = ""                            # 空 = 自动检测系统代理

[browser]
channel               = "msedge"    # msedge | chrome
max_browser_instances = 3           # 并发浏览器进程上限（跨 CLI）
```

未配置的 provider 直接跳过。`firecrawl scrape` / `search` 和浏览器源是免 key 的，所以空配置下
`search` 和 `fetch` 也能跑。`github file` / `github repo` 同样免 key（匿名 60 次/小时），且
`fetch` 会在普通 API 之前自动接管 GitHub 文件 / 仓库 URL。

## 工作原理

每条命令是对 provider 插件的一个薄 async 包装。`search` 扇出到各 provider 合并；`fetch` 先走便宜
的 API，失败升级到真实浏览器 + Cloudflare 绕过；`locate` 把文档切块、用小模型打分。输出默认
markdown（可读、省 token）；`--output json` 出结构化 JSON 供解析。

大型调研可分派 `researcher` 子 agent：它在隔离上下文里跑完整的 search → fetch → locate 流程，把报告
写到磁盘，只返回摘要，重活阅读不污染主上下文。

## 许可

MIT
