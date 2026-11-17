"""Minimal logging: one line per event, text or JSON, always flushed.

The runner's own output is passed through unchanged, so text lines use the
same UTC timestamp format as the runner.
"""

import json
import sys
import threading
import time

_format = "text"
_lock = threading.Lock()


def setup(fmt: str) -> None:
    global _format
    _format = fmt


def log(message: str, **fields) -> None:
    now = time.gmtime()
    with _lock:
        if _format == "json":
            record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", now), "msg": message, **fields}
            sys.stdout.write(json.dumps(record, default=str) + "\n")
        else:
            extra = "".join(f" {key}={value}" for key, value in fields.items())
            sys.stdout.write(f"{time.strftime('%Y-%m-%d %H:%M:%SZ', now)} {message}{extra}\n")
        sys.stdout.flush()


def passthrough(line: str, **fields) -> None:
    """A line of runner output. In JSON mode it is wrapped, so every line stays valid JSON."""
    with _lock:
        if _format == "json":
            sys.stdout.write(json.dumps({"runner": line.rstrip("\n"), **fields}) + "\n")
        else:
            sys.stdout.write(line if line.endswith("\n") else line + "\n")
        sys.stdout.flush()
