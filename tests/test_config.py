import pytest

from solo_runner.config import ConfigError, load


def test_minimal_pat_config_defaults():
    env = {"GITHUB_TOKEN": "t0k3n"}
    config = load(env)
    assert config.token == "t0k3n"
    assert config.repos == []  # autodiscovery
    assert config.max_concurrent == 1
    assert config.labels == []
    assert config.runner_labels[:2] == ["self-hosted", "linux"]
    assert config.allow_public_repos is False
    # The secret is gone from the environment the runner will inherit.
    assert "GITHUB_TOKEN" not in env


def test_access_token_alias_and_lists():
    env = {"ACCESS_TOKEN": "x", "REPOS": "me/a, me/b me/c", "LABELS": "homelab,gpu", "MAX_CONCURRENT": "3"}
    config = load(env)
    assert config.token == "x"
    assert config.repos == ["me/a", "me/b", "me/c"]
    assert config.labels == ["homelab", "gpu"]
    assert config.runner_labels[-2:] == ["homelab", "gpu"]
    assert config.max_concurrent == 3


def test_token_from_file(tmp_path):
    secret = tmp_path / "token"
    secret.write_text("from-file\n")
    env = {"GITHUB_TOKEN_FILE": str(secret)}
    assert load(env).token == "from-file"
    assert "GITHUB_TOKEN_FILE" not in env


@pytest.mark.parametrize(
    "env, message",
    [
        ({}, "is not set"),
        ({"GITHUB_TOKEN": "a", "GITHUB_APP_ID": "1"}, "not both"),
        ({"GITHUB_APP_ID": "1"}, "PRIVATE_KEY"),
        ({"GITHUB_TOKEN": "a", "REPOS": "justaname"}, "owner/name"),
        ({"GITHUB_TOKEN": "a", "MAX_CONCURRENT": "0"}, "at least 1"),
        ({"GITHUB_TOKEN": "a", "POLL_INTERVAL": "soon"}, "whole number"),
        ({"GITHUB_TOKEN": "a", "ALLOW_PUBLIC_REPOS": "maybe"}, "true or false"),
        ({"GITHUB_TOKEN": "a", "LOG_FORMAT": "xml"}, "text or json"),
    ],
)
def test_invalid(env, message):
    with pytest.raises(ConfigError, match=message):
        load(env)


def test_default_label_not_duplicated():
    config = load({"GITHUB_TOKEN": "a", "LABELS": "self-hosted,homelab"})
    assert config.runner_labels.count("self-hosted") == 1


def test_metrics_port():
    assert load({"GITHUB_TOKEN": "t"}).metrics_port == 0
    config = load({"GITHUB_TOKEN": "t", "METRICS_PORT": "9464", "METRICS_ADDR": "127.0.0.1"})
    assert (config.metrics_port, config.metrics_addr) == (9464, "127.0.0.1")
    with pytest.raises(ConfigError):
        load({"GITHUB_TOKEN": "t", "METRICS_PORT": "70000"})
