"""Entry point: python3 -m solo_runner."""

import sys

from .config import ConfigError, load
from .dispatcher import Dispatcher
from .log import log, setup
from .metrics import serve


def main() -> int:
    try:
        config = load()
    except ConfigError as err:
        print(f"solo-runner: {err}", file=sys.stderr)
        return 2
    setup(config.log_format)
    if config.mode == "scaleset":
        from .scaleset_mode import ScaleSetDispatcher

        dispatcher = ScaleSetDispatcher(config)
    else:
        dispatcher = Dispatcher(config)
    if config.metrics_port:
        try:
            serve(dispatcher.metrics, config.metrics_addr, config.metrics_port)
        except OSError as err:
            print(f"solo-runner: metrics on {config.metrics_addr}:{config.metrics_port}: {err}", file=sys.stderr)
            return 2
        log(f"metrics on http://{config.metrics_addr}:{config.metrics_port}/metrics")
    try:
        dispatcher.run()
    except KeyboardInterrupt:  # pragma: no cover - signal handler normally catches it
        log("interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
