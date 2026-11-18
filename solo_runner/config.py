"""Configuration from environment variables, plus an optional config file.

Secrets can also come from files (`*_FILE`), so they work with Docker secrets
and never show up in `docker inspect`. The environment holds the defaults;
CONFIG_FILE can change `labels` and `max_concurrent` for single repos.
"""

import fnmatch
import os
import platform
from dataclasses import dataclass, field

from . import miniyaml


class ConfigError(Exception):
    pass


def _arch_label() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


@dataclass
class RepoSettings:
    """One entry under `repos:` in the config file. None = use the environment's value."""

    pattern: str
    labels: list = None
    max_concurrent: int = None

    def matches(self, repo: str) -> bool:
        # Like REPO_EXCLUDE: against owner/repo and repo. GitHub names ignore case.
        repo, pattern = repo.lower(), self.pattern.lower()
        return fnmatch.fnmatchcase(repo, pattern) or fnmatch.fnmatchcase(repo.split("/", 1)[-1], pattern)


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
    config_file: str = ""
    repo_settings: list = field(default_factory=list)  # RepoSettings, first match wins

    @property
    def default_labels(self) -> list:
        return ["self-hosted", "linux", self.arch]

    @property
    def runner_labels(self) -> list:
        return self.runner_labels_for(None)

    def settings_for(self, repo):
        """The first config file entry that matches repo, or None."""
        if repo is None:
            return None
        return next((entry for entry in self.repo_settings if entry.matches(repo)), None)

    def labels_for(self, repo) -> list:
        """The extra labels for repo: its config file entry's, else LABELS."""
        entry = self.settings_for(repo)
        return entry.labels if entry is not None and entry.labels is not None else self.labels

    def runner_labels_for(self, repo) -> list:
        return self.default_labels + [
            label for label in self.labels_for(repo) if label.lower() not in self.default_labels
        ]

    def max_concurrent_for(self, repo):
        """The repo's own limit, or None if only MAX_CONCURRENT applies."""
        entry = self.settings_for(repo)
        return entry.max_concurrent if entry is not None else None


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


_REPO_KEYS = {"labels", "max_concurrent"}


def _labels(value, where: str) -> list:
    if isinstance(value, str):
        value = _split(value)
    if not isinstance(value, list) or not all(isinstance(item, (str, int)) and str(item) for item in value):
        raise ConfigError(f"{where}: labels must be a list of names, e.g. [solo, gpu]")
    labels = [str(item).strip() for item in value]
    if any(not label or " " in label or "," in label for label in labels):
        raise ConfigError(f"{where}: a label must not be empty or contain spaces or commas")
    return labels


def parse_repo_settings(text: str, source: str = "config file") -> list:
    """The `repos:` entries of a config file, in file order."""
    try:
        data = miniyaml.loads(text)
    except miniyaml.YAMLError as err:
        raise ConfigError(f"{source}: {err}") from None
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a mapping with 'repos:' at the top")
    unknown = sorted(str(key) for key in data if key != "repos")
    if unknown:
        raise ConfigError(f"{source}: unknown top-level key(s) {', '.join(unknown)}; only 'repos' is supported")
    repos = data.get("repos")
    if repos is None:
        return []
    if not isinstance(repos, dict):
        raise ConfigError(f"{source}: 'repos' must map repo names or patterns to settings")
    result = []
    for pattern, settings in repos.items():
        where = f"{source}: repos.{pattern}"
        if not isinstance(pattern, str) or not pattern.strip() or pattern.count("/") > 1:
            raise ConfigError(f"{where}: key must be owner/repo, repo or a glob pattern")
        if settings is None:
            settings = {}
        if not isinstance(settings, dict):
            raise ConfigError(f"{where}: expected settings (labels, max_concurrent)")
        unknown = sorted(str(key) for key in settings if key not in _REPO_KEYS)
        if unknown:
            raise ConfigError(f"{where}: unknown setting(s) {', '.join(unknown)}; use labels, max_concurrent")
        entry = RepoSettings(pattern.strip())
        if settings.get("labels") is not None:
            entry.labels = _labels(settings["labels"], where)
        if settings.get("max_concurrent") is not None:
            value = settings["max_concurrent"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigError(f"{where}: max_concurrent must be a whole number of at least 1")
            entry.max_concurrent = value
        result.append(entry)
    return result


def load_config_file(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as err:
        raise ConfigError(f"CONFIG_FILE: {err}") from None
    return parse_repo_settings(text, os.path.basename(path))


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
    config.config_file = env.get("CONFIG_FILE", "").strip()
    if config.config_file:
        config.repo_settings = load_config_file(config.config_file)
    return config
