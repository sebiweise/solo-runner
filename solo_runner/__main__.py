"""Entry point: python3 -m solo_runner."""

import sys

from .config import ConfigError, load
from .dispatcher import Dispatcher
from .log import log, setup


def main() -> int:
    try:
        config = load()
    except ConfigError as err:
        print(f"solo-runner: {err}", file=sys.stderr)
        return 2
    setup(config.log_format)
    dispatcher = Dispatcher(config)
    try:
        dispatcher.run()
    except KeyboardInterrupt:  # pragma: no cover - signal handler normally catches it
        log("interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
