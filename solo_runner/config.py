"""Configuration from environment variables.

Secrets can also come from files (`*_FILE`), so they work with Docker secrets
and never show up in `docker inspect`.
"""

import os
import platform
from dataclasses import dataclass, field


class ConfigError(Exception):
    pass


def _arch_label() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


@dataclass
class Config:
    token: str = ""
    app_id: str = ""
    app_private_key: str = ""
    app_installation_id: str = ""
    repos: list = field(default_factory=list)  # empty = autodiscovery
    repo_exclude: list = field(default_factory=list)
    allow_public_repos: bool = False
    labels: list = field(default_factory=list)
    max_concurrent: int = 1
    poll_interval: int = 30
    idle_timeout: int = 300
    discovery_interval: int = 600
    name_prefix: str = ""
    work_dir: str = "/srv/solo-runner/work"
    runner_dir: str = "/home/runner"
    slots_dir: str = "/home/runner/_slots"
    heartbeat_url: str = ""
    log_format: str = "text"
    api_url: str = "https://api.github.com"
    arch: str = field(default_factory=_arch_label)

    @property
    def default_labels(self) -> list:
        return ["self-hosted", "linux", self.arch]

    @property
    def runner_labels(self) -> list:
        return self.default_labels + [label for label in self.labels if label.lower() not in self.default_labels]


def _secret(env, name: str) -> str:
    """NAME, or the contents of the file named in NAME_FILE. Removed from env either way."""
    value = env.pop(name, "")
    path = env.pop(f"{name}_FILE", "")
    if value and path:
        raise ConfigError(f"set {name} or {name}_FILE, not both")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                value = handle.read().strip()
        except OSError as err:
            raise ConfigError(f"{name}_FILE: {err}") from None
    return value


def _int(env, name: str, default: int, minimum: int) -> int:
    raw = env.get(name, "") or str(default)
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _bool(env, name: str, default: bool) -> bool:
    raw = (env.get(name, "") or str(default)).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _split(raw: str) -> list:
    return [item for item in raw.replace(",", " ").split() if item]


def load(env=None) -> Config:
    """Read the configuration. Secrets are removed from env (default: os.environ),
    so the runner and every job step it starts do not inherit them."""
    env = os.environ if env is None else env
    config = Config()

    # GITHUB_TOKEN is the documented name; ACCESS_TOKEN keeps the
    # myoung34/github-runner name working for people coming from there.
    config.token = _secret(env, "GITHUB_TOKEN") or _secret(env, "ACCESS_TOKEN")
    config.app_id = env.get("GITHUB_APP_ID", "").strip()
    config.app_private_key = _secret(env, "GITHUB_APP_PRIVATE_KEY")
    config.app_installation_id = env.get("GITHUB_APP_INSTALLATION_ID", "").strip()
    if config.token and config.app_id:
        raise ConfigError("set GITHUB_TOKEN or GITHUB_APP_ID, not both")
    if not config.token and not config.app_id:
        raise ConfigError("GITHUB_TOKEN (or GITHUB_APP_ID with GITHUB_APP_PRIVATE_KEY_FILE) is not set")
    if config.app_id and not config.app_private_key:
        raise ConfigError("GITHUB_APP_ID is set, but GITHUB_APP_PRIVATE_KEY_FILE is not")

    repos = env.get("REPOS", "auto").strip()
    if repos.lower() not in ("", "auto"):
        config.repos = _split(repos)
        for repo in config.repos:
            if repo.count("/") != 1 or repo.startswith("/") or repo.endswith("/"):
                raise ConfigError(f"REPOS: {repo!r} is not owner/name")
    config.repo_exclude = _split(env.get("REPO_EXCLUDE", ""))
    config.allow_public_repos = _bool(env, "ALLOW_PUBLIC_REPOS", False)

    config.labels = _split(env.get("LABELS", ""))
    config.max_concurrent = _int(env, "MAX_CONCURRENT", 1, 1)
    config.poll_interval = _int(env, "POLL_INTERVAL", 30, 5)
    config.idle_timeout = _int(env, "IDLE_TIMEOUT", 300, 30)
    config.discovery_interval = _int(env, "DISCOVERY_INTERVAL", 600, 60)
    config.name_prefix = env.get("RUNNER_NAME_PREFIX", "").strip() or "solo-runner"
    config.work_dir = env.get("WORK_DIR", config.work_dir).rstrip("/") or config.work_dir
    config.runner_dir = env.get("RUNNER_DIR", config.runner_dir).rstrip("/") or config.runner_dir
    config.slots_dir = env.get("SLOTS_DIR", config.runner_dir + "/_slots").rstrip("/")
    config.heartbeat_url = env.get("HEARTBEAT_URL", "").strip()
    config.log_format = env.get("LOG_FORMAT", "text").strip().lower()
    if config.log_format not in ("text", "json"):
        raise ConfigError(f"LOG_FORMAT must be text or json, got {config.log_format!r}")
    config.api_url = env.get("GITHUB_API_URL", config.api_url).rstrip("/")
    return config
