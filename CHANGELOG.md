# Changelog

All notable changes to this project are listed here. Versions follow
[Semantic Versioning](https://semver.org); runner base image updates are patch
releases.

## [Unreleased]

### Added

- Optional `CONFIG_FILE` (YAML) with settings per repo: `labels` and
  `max_concurrent`, for single repos or glob patterns. The environment stays
  the default for everything the file leaves out.

## [0.1.0] - 2026-11-18

First release, extracted from a homelab setup that ran one shared runner for
ten personal repos.

### Added

- Dispatcher that polls all served repos (with ETags) for queued jobs and starts
  one just-in-time runner per job, oldest job first.
- `MAX_CONCURRENT`: global limit of jobs on the host, across all repos; each slot
  gets its own runner copy and an empty work directory.
- `REPOS=auto`: serve every private, non-archived repo the credential can see,
  refreshed every `DISCOVERY_INTERVAL`; `REPO_EXCLUDE` patterns.
- Public repos are refused unless `ALLOW_PUBLIC_REPOS=true`.
- Authentication with a PAT or a GitHub App (RS256 JWT without extra packages);
  secrets from `*_FILE`, removed from the environment before runners start.
- Heartbeat URL, text or JSON logs, removal of stale offline runners at start.
- Image on GHCR for amd64 and arm64, based on `ghcr.io/actions/actions-runner`,
  signed with cosign, with SBOM and provenance.
