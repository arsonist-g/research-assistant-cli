"""cookie daemon 的模块入口：python -m research_assistant.loginstate.daemon_cli <port>"""

from __future__ import annotations

import sys

from .daemon import run


def main() -> None:
    port = 17890
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    run(port)


if __name__ == "__main__":
    main()
