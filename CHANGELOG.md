# Changelog

All notable changes to this project are listed here. Versions follow
[Semantic Versioning](https://semver.org); runner base image updates are patch
releases.

## [Unreleased]

## [0.2.0] - 2026-11-18

### Added

- Issue forms (bug report, feature request), a pull request template and
  README badges.
- Ruleset for `main` and a maintainer guide to set it up
  (`docs/maintainers/branch-protection.md`).
- End-to-end test workflow: runs real jobs through the image against a private
  test repo on pull requests, weekly and on demand (`.github/workflows/e2e.yml`).
- The docker compose plugin in the image is kept current: a daily workflow opens
  a pull request for each new compose release, and a merged bump is released as
  the next patch version, like runner base image bumps.
- SonarCloud analysis, with its Quality Gate badge in the README.
- Optional `CONFIG_FILE` (YAML) with settings per repo: `labels` and
  `max_concurrent`, for single repos or glob patterns. The environment stays
  the default for everything the file leaves out.
- Optional Prometheus metrics on `METRICS_PORT` (`/metrics`): free and busy
  slots, runners and jobs started and finished per repo, idle and failed
  runners, job wait time, queued jobs, GitHub API requests, errors and rate
  limit.

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
