"""Bump the docker compose plugin in the Dockerfile to its latest release.

Dependabot only sees the FROM line, not the plugin we download by URL, so
.github/workflows/update-compose.yml runs this on a schedule and opens a pull
request when it changes something. Standard library only, like the dispatcher.

    python3 scripts/update_compose.py [--version X.Y.Z]

Prints what it did. On GitHub Actions it also writes `previous` and `version`
to $GITHUB_OUTPUT; `version` is empty when the Dockerfile is already current.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

REPO = "docker/compose"
# Dockerfile build arch -> name in the compose release assets.
ARCHES = {"AMD64": "x86_64", "ARM64": "aarch64"}
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(RuntimeError):
    pass


def fetch(url: str) -> bytes:
    headers = {"User-Agent": "solo-runner-update-compose"}
    token = os.environ.get("GITHUB_TOKEN")
    # Only the API needs it (rate limit); release downloads redirect elsewhere.
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
        return resp.read()


def parse_version(value: str) -> tuple[int, ...]:
    value = value.removeprefix("v")
    if not VERSION_RE.match(value):
        raise UpdateError(f"not a release version: {value!r}")
    return tuple(int(part) for part in value.split("."))


def current_version(dockerfile: str) -> str:
    match = re.search(r"^ARG COMPOSE_VERSION=(\S+)$", dockerfile, re.MULTILINE)
    if not match:
        raise UpdateError("ARG COMPOSE_VERSION not found in the Dockerfile")
    return match.group(1)


def latest_version(get=fetch) -> str:
    # /releases/latest skips drafts and pre-releases.
    release = json.loads(get(f"https://api.github.com/repos/{REPO}/releases/latest"))
    version = release["tag_name"].removeprefix("v")
    parse_version(version)
    return version


def checksums(version: str, get=fetch) -> dict[str, str]:
    """SHA-256 per Dockerfile arch, from the release's .sha256 files."""
    sums = {}
    for arch, asset in ARCHES.items():
        url = f"https://github.com/{REPO}/releases/download/v{version}/docker-compose-linux-{asset}.sha256"
        # Format: "<hex> *docker-compose-linux-x86_64"
        fields = get(url).decode().split()
        if not fields or not SHA256_RE.match(fields[0]):
            raise UpdateError(f"unexpected checksum file at {url}")
        sums[arch] = fields[0]
    return sums


def _replace_arg(text: str, name: str, value: str) -> str:
    new, count = re.subn(rf"^ARG {name}=\S+$", f"ARG {name}={value}", text, flags=re.MULTILINE)
    if count != 1:
        raise UpdateError(f"expected one ARG {name} in the Dockerfile, found {count}")
    return new


def bump_dockerfile(dockerfile: str, version: str, sums: dict[str, str]) -> str:
    text = _replace_arg(dockerfile, "COMPOSE_VERSION", version)
    for arch in ARCHES:
        text = _replace_arg(text, f"COMPOSE_SHA256_{arch}", sums[arch])
    return text


def add_changelog_entry(changelog: str, previous: str, version: str) -> str:
    """Add a line under "### Changed" of the Unreleased section."""
    entry = f"- Docker compose plugin {version} (was {previous})."
    head, sep, rest = changelog.partition("## [Unreleased]\n")
    if not sep:
        raise UpdateError("no '## [Unreleased]' section in CHANGELOG.md")
    # The Unreleased section ends where the next release starts.
    match = re.search(r"^## \[", rest, re.MULTILINE)
    section, after = (rest[: match.start()], rest[match.start() :]) if match else (rest, "")
    # An earlier, unmerged bump of ours would otherwise stack up.
    section = re.sub(r"^- Docker compose plugin .*\n", "", section, flags=re.MULTILINE)
    if "### Changed\n" in section:
        section = re.sub(r"### Changed\n+", f"### Changed\n\n{entry}\n", section, count=1)
    else:
        body = section.strip("\n")
        section = (f"\n{body}\n" if body else "") + f"\n### Changed\n\n{entry}\n\n"
    return head + sep + section + after


def main(argv=None, root: Path | None = None, get=fetch) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", help="target version instead of the latest release")
    args = parser.parse_args(argv)
    root = root or Path(__file__).resolve().parent.parent
    dockerfile_path, changelog_path = root / "Dockerfile", root / "CHANGELOG.md"

    dockerfile = dockerfile_path.read_text()
    previous = current_version(dockerfile)
    target = (args.version or latest_version(get)).removeprefix("v")
    updated = parse_version(target) > parse_version(previous)
    if updated:
        dockerfile_path.write_text(bump_dockerfile(dockerfile, target, checksums(target, get)))
        changelog_path.write_text(add_changelog_entry(changelog_path.read_text(), previous, target))
        print(f"docker compose {previous} -> {target}")
    else:
        print(f"docker compose {previous} is current (latest: {target})")

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as fh:
            fh.write(f"previous={previous}\nversion={target if updated else ''}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (UpdateError, OSError, ValueError, KeyError) as err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)
