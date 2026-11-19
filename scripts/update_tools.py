"""Bump a tool the Dockerfile downloads by URL to its latest release.

Dependabot only sees the FROM line, not the docker compose plugin or the GitHub
CLI we download from their GitHub releases, so .github/workflows/update-tools.yml
runs this on a schedule (once per tool) and opens a pull request when it changes
something. Standard library only, like the dispatcher.

    python3 scripts/update_tools.py --tool compose|gh [--version X.Y.Z]

Says what it did on stderr and prints `previous=X.Y.Z` and `version=X.Y.Z` on
stdout, for the workflow to append to $GITHUB_OUTPUT; `version` is empty when
the Dockerfile is already current.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DOCKERFILE_ARCHES = ("AMD64", "ARM64")


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Tool:
    repo: str  # GitHub repo that publishes the releases
    arg: str  # prefix of the Dockerfile ARGs: <arg>_VERSION, <arg>_SHA256_AMD64, ...
    title: str  # name in the CHANGELOG line
    asset_arch: dict[str, str]  # Dockerfile arch -> arch in the release asset names
    sums_url: str  # where the checksums are, {version} and {asset_arch} filled in
    asset: str  # file name the checksum belongs to (for files listing several)


TOOLS = {
    # One .sha256 file per binary: "<hex> *docker-compose-linux-x86_64".
    "compose": Tool(
        repo="docker/compose",
        arg="COMPOSE",
        title="Docker compose plugin",
        asset_arch={"AMD64": "x86_64", "ARM64": "aarch64"},
        sums_url="https://github.com/docker/compose/releases/download/v{version}/docker-compose-linux-{asset_arch}.sha256",
        asset="docker-compose-linux-{asset_arch}",
    ),
    # One checksums file per release: "<hex>  gh_2.105.0_linux_amd64.tar.gz" per line.
    "gh": Tool(
        repo="cli/cli",
        arg="GH",
        title="GitHub CLI",
        asset_arch={"AMD64": "amd64", "ARM64": "arm64"},
        sums_url="https://github.com/cli/cli/releases/download/v{version}/gh_{version}_checksums.txt",
        asset="gh_{version}_linux_{asset_arch}.tar.gz",
    ),
}


def fetch(url: str) -> bytes:
    headers = {"User-Agent": "solo-runner-update-tools"}
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


def current_version(dockerfile: str, tool: Tool = TOOLS["compose"]) -> str:
    match = re.search(rf"^ARG {tool.arg}_VERSION=(\S+)$", dockerfile, re.MULTILINE)
    if not match:
        raise UpdateError(f"ARG {tool.arg}_VERSION not found in the Dockerfile")
    return match.group(1)


def latest_version(get=fetch, tool: Tool = TOOLS["compose"]) -> str:
    # /releases/latest skips drafts and pre-releases.
    release = json.loads(get(f"https://api.github.com/repos/{tool.repo}/releases/latest"))
    version = release["tag_name"].removeprefix("v")
    parse_version(version)
    return version


def checksums(version: str, get=fetch, tool: Tool = TOOLS["compose"]) -> dict[str, str]:
    """SHA-256 per Dockerfile arch, from the release's checksum files."""
    sums = {}
    files: dict[str, bytes] = {}
    for arch in DOCKERFILE_ARCHES:
        fill = {"version": version, "asset_arch": tool.asset_arch[arch]}
        url = tool.sums_url.format(**fill)
        if url not in files:
            files[url] = get(url)
        asset = tool.asset.format(**fill)
        found = None
        for line in files[url].decode().splitlines():
            fields = line.split()
            if not fields:
                continue
            # A file with one checksum may name the asset with a leading "*" (binary mode).
            name = fields[1].lstrip("*") if len(fields) > 1 else asset
            if name == asset:
                found = fields[0]
                break
        if not found or not SHA256_RE.match(found):
            raise UpdateError(f"no checksum for {asset} at {url}")
        sums[arch] = found
    return sums


def _replace_arg(text: str, name: str, value: str) -> str:
    new, count = re.subn(rf"^ARG {name}=\S+$", f"ARG {name}={value}", text, flags=re.MULTILINE)
    if count != 1:
        raise UpdateError(f"expected one ARG {name} in the Dockerfile, found {count}")
    return new


def bump_dockerfile(dockerfile: str, version: str, sums: dict[str, str], tool: Tool = TOOLS["compose"]) -> str:
    text = _replace_arg(dockerfile, f"{tool.arg}_VERSION", version)
    for arch in DOCKERFILE_ARCHES:
        text = _replace_arg(text, f"{tool.arg}_SHA256_{arch}", sums[arch])
    return text


def add_changelog_entry(changelog: str, previous: str, version: str, tool: Tool = TOOLS["compose"]) -> str:
    """Add a line under "### Changed" of the Unreleased section."""
    entry = f"- {tool.title} {version} (was {previous})."
    head, sep, rest = changelog.partition("## [Unreleased]\n")
    if not sep:
        raise UpdateError("no '## [Unreleased]' section in CHANGELOG.md")
    # The Unreleased section ends where the next release starts.
    match = re.search(r"^## \[", rest, re.MULTILINE)
    section, after = (rest[: match.start()], rest[match.start() :]) if match else (rest, "")
    # An earlier, unmerged bump of ours would otherwise stack up.
    section = re.sub(rf"^- {re.escape(tool.title)} \d.*\n", "", section, flags=re.MULTILINE)
    if "### Changed\n" in section:
        section = re.sub(r"### Changed\n+", f"### Changed\n\n{entry}\n", section, count=1)
    else:
        body = section.strip("\n")
        section = (f"\n{body}\n" if body else "") + f"\n### Changed\n\n{entry}\n\n"
    return head + sep + section + after


def main(argv=None, root: Path | None = None, get=fetch) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tool", choices=sorted(TOOLS), default="compose", help="which tool to bump")
    parser.add_argument("--version", help="target version instead of the latest release")
    args = parser.parse_args(argv)
    tool = TOOLS[args.tool]
    root = root or Path(__file__).resolve().parent.parent
    dockerfile_path, changelog_path = root / "Dockerfile", root / "CHANGELOG.md"

    dockerfile = dockerfile_path.read_text()
    previous = current_version(dockerfile, tool)
    target = (args.version or latest_version(get, tool)).removeprefix("v")
    updated = parse_version(target) > parse_version(previous)
    if updated:
        # SonarCloud reads the release data as a tainted path (S2083). The paths are fixed
        # repo files; only the content comes from the release, and parse_version() and
        # checksums() have already rejected anything but X.Y.Z and 64 hex digits.
        sums = checksums(target, get, tool)
        dockerfile_path.write_text(bump_dockerfile(dockerfile, target, sums, tool))  # NOSONAR
        changelog_path.write_text(add_changelog_entry(changelog_path.read_text(), previous, target, tool))  # NOSONAR
        print(f"{args.tool} {previous} -> {target}", file=sys.stderr)
    else:
        print(f"{args.tool} {previous} is current (latest: {target})", file=sys.stderr)
    print(f"previous={previous}\nversion={target if updated else ''}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (UpdateError, OSError, ValueError, KeyError) as err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)
