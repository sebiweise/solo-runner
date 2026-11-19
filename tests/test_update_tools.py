import json
from pathlib import Path

import pytest

from scripts.update_tools import (
    TOOLS,
    UpdateError,
    add_changelog_entry,
    bump_dockerfile,
    checksums,
    current_version,
    latest_version,
    main,
)

ROOT = Path(__file__).resolve().parent.parent
SUM_AMD64 = "a" * 64
SUM_ARM64 = "b" * 64


def fake_get(tag="v5.9.1"):
    def get(url):
        if url.endswith("/releases/latest"):
            return json.dumps({"tag_name": tag}).encode()
        if url.endswith("docker-compose-linux-x86_64.sha256"):
            return f"{SUM_AMD64} *docker-compose-linux-x86_64\n".encode()
        if url.endswith("docker-compose-linux-aarch64.sha256"):
            return f"{SUM_ARM64} *docker-compose-linux-aarch64\n".encode()
        raise AssertionError(f"unexpected URL {url}")

    return get


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "Dockerfile").write_text((ROOT / "Dockerfile").read_text())
    (tmp_path / "CHANGELOG.md").write_text((ROOT / "CHANGELOG.md").read_text())
    return tmp_path


def test_real_dockerfile_has_the_args():
    dockerfile = (ROOT / "Dockerfile").read_text()
    version = current_version(dockerfile)
    # Fails if someone renames or duplicates one of the ARGs the script edits.
    bumped = bump_dockerfile(dockerfile, "99.0.0", {"AMD64": SUM_AMD64, "ARM64": SUM_ARM64})
    assert f"ARG COMPOSE_VERSION={version}" not in bumped
    assert "ARG COMPOSE_VERSION=99.0.0" in bumped
    assert f"ARG COMPOSE_SHA256_AMD64={SUM_AMD64}" in bumped
    assert f"ARG COMPOSE_SHA256_ARM64={SUM_ARM64}" in bumped


def test_latest_version_and_checksums():
    assert latest_version(fake_get()) == "5.9.1"
    assert checksums("5.9.1", fake_get()) == {"AMD64": SUM_AMD64, "ARM64": SUM_ARM64}


def test_rejects_odd_tags_and_checksums():
    with pytest.raises(UpdateError):
        latest_version(fake_get(tag="v6.0.0-rc.1"))
    with pytest.raises(UpdateError):
        checksums("5.9.1", lambda url: b"<html>Not Found</html>")


def test_missing_arg_is_an_error():
    with pytest.raises(UpdateError):
        bump_dockerfile("FROM scratch\n", "1.0.0", {"AMD64": SUM_AMD64, "ARM64": SUM_ARM64})


def test_changelog_entry_into_empty_unreleased():
    changelog = "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-11-18\n\n- First.\n"
    assert add_changelog_entry(changelog, "5.8.0", "5.9.1") == (
        "# Changelog\n\n## [Unreleased]\n\n### Changed\n\n"
        "- Docker compose plugin 5.9.1 (was 5.8.0).\n\n## [0.1.0] - 2026-11-18\n\n- First.\n"
    )


def test_changelog_entry_joins_existing_changed_and_replaces_older_bump():
    changelog = (
        "## [Unreleased]\n\n### Added\n\n- Metrics.\n\n### Changed\n\n"
        "- Docker compose plugin 5.9.0 (was 5.8.0).\n- Faster polling.\n\n## [0.1.0]\n"
    )
    assert add_changelog_entry(changelog, "5.9.0", "5.9.1") == (
        "## [Unreleased]\n\n### Added\n\n- Metrics.\n\n### Changed\n\n"
        "- Docker compose plugin 5.9.1 (was 5.9.0).\n- Faster polling.\n\n## [0.1.0]\n"
    )


def test_main_bumps_and_prints_outputs(repo, capsys):
    previous = current_version((repo / "Dockerfile").read_text())
    assert main([], root=repo, get=fake_get("v99.1.0")) == 0
    assert current_version((repo / "Dockerfile").read_text()) == "99.1.0"
    assert f"- Docker compose plugin 99.1.0 (was {previous})." in (repo / "CHANGELOG.md").read_text()
    assert capsys.readouterr().out == f"previous={previous}\nversion=99.1.0\n"


def test_main_leaves_current_or_older_alone(repo, capsys):
    before = (repo / "Dockerfile").read_text(), (repo / "CHANGELOG.md").read_text()
    assert main(["--version", "0.0.1"], root=repo, get=fake_get()) == 0
    assert ((repo / "Dockerfile").read_text(), (repo / "CHANGELOG.md").read_text()) == before
    assert capsys.readouterr().out.endswith("version=\n")


GH = TOOLS["gh"]
GH_SUMS = (
    "1" * 64
    + "  gh_2.106.0_linux_386.tar.gz\n"
    + SUM_AMD64
    + "  gh_2.106.0_linux_amd64.tar.gz\n"
    + "2" * 64
    + "  gh_2.106.0_linux_amd64.deb\n"
    + SUM_ARM64
    + "  gh_2.106.0_linux_arm64.tar.gz\n"
)


def fake_gh_get(tag="v2.106.0"):
    calls = []

    def get(url):
        calls.append(url)
        if url == "https://api.github.com/repos/cli/cli/releases/latest":
            return json.dumps({"tag_name": tag}).encode()
        if url == "https://github.com/cli/cli/releases/download/v2.106.0/gh_2.106.0_checksums.txt":
            return GH_SUMS.encode()
        raise AssertionError(f"unexpected URL {url}")

    get.calls = calls
    return get


def test_real_dockerfile_has_the_gh_args():
    dockerfile = (ROOT / "Dockerfile").read_text()
    version = current_version(dockerfile, GH)
    bumped = bump_dockerfile(dockerfile, "99.0.0", {"AMD64": SUM_AMD64, "ARM64": SUM_ARM64}, GH)
    assert f"ARG GH_VERSION={version}" not in bumped
    assert "ARG GH_VERSION=99.0.0" in bumped
    assert f"ARG GH_SHA256_AMD64={SUM_AMD64}" in bumped
    assert f"ARG GH_SHA256_ARM64={SUM_ARM64}" in bumped
    # The compose ARGs stay as they are.
    assert current_version(bumped) == current_version(dockerfile)


def test_gh_checksums_pick_the_tarballs_from_one_file():
    get = fake_gh_get()
    assert latest_version(get, GH) == "2.106.0"
    assert checksums("2.106.0", get, GH) == {"AMD64": SUM_AMD64, "ARM64": SUM_ARM64}
    # One checksums file for both architectures, fetched once.
    assert sum("checksums.txt" in url for url in get.calls) == 1


def test_gh_checksums_missing_asset_is_an_error():
    with pytest.raises(UpdateError):
        checksums("2.106.0", lambda url: ("1" * 64 + "  gh_2.106.0_linux_amd64.tar.gz\n").encode(), GH)


def test_changelog_entries_of_both_tools_stay_apart():
    changelog = "## [Unreleased]\n\n### Changed\n\n- Docker compose plugin 5.9.0 (was 5.8.0).\n\n## [0.1.0]\n"
    assert add_changelog_entry(changelog, "2.105.0", "2.106.0", GH) == (
        "## [Unreleased]\n\n### Changed\n\n- GitHub CLI 2.106.0 (was 2.105.0).\n"
        "- Docker compose plugin 5.9.0 (was 5.8.0).\n\n## [0.1.0]\n"
    )


def test_main_bumps_gh(repo, capsys):
    previous = current_version((repo / "Dockerfile").read_text(), GH)
    compose = current_version((repo / "Dockerfile").read_text())
    assert main(["--tool", "gh"], root=repo, get=fake_gh_get()) == 0
    dockerfile = (repo / "Dockerfile").read_text()
    assert current_version(dockerfile, GH) == "2.106.0"
    assert current_version(dockerfile) == compose
    assert f"- GitHub CLI 2.106.0 (was {previous})." in (repo / "CHANGELOG.md").read_text()
    assert capsys.readouterr().out == f"previous={previous}\nversion=2.106.0\n"
