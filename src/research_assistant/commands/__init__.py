"""commands 包：research-assistant 原生命令（fetch/locate/search/setup/skills/doctor）。

每个模块暴露：
    NAME / ALIASES / HELP
    register(subparsers) —— 向 CLI 注册子命令与参数，set_defaults(_handler=run)
    async run(args_ns, config) -> dict
"""
