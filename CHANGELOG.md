# Changelog

All notable changes to this project are listed here. Versions follow
[Semantic Versioning](https://semver.org); runner base image updates are patch
releases.

## [Unreleased]

### Fixed

- Queued jobs in a workflow run that GitHub reports as `pending` or `waiting`
  (concurrency groups, environments) are found and run. Before, such a job
  showed "Waiting for a runner to pick up this job" forever, with nothing in
  solo-runner's logs. Up to 100 instead of 30 runs per state are searched.

## [0.3.1] - 2026-11-24

### Changed

- A queued job that asks for some of the runner's labels but does not match
  (a label this host lacks, or one of `LABELS` missing in `runs-on`) is now
  logged once as `queued job ... not taken`. Before, it stayed "Queued" in
  GitHub with nothing in the logs.

## [0.3.0] - 2026-11-20

### Added

- The image includes the GitHub CLI (`gh`, 2.105.0), as GitHub-hosted runners
  do. Workflows that call `gh release`, `gh api` or `gh run` failed with
  "gh: command not found". `update-tools.yml` (formerly `update-compose.yml`)
  opens a pull request for each new `gh` release, like for the compose plugin.
- Experimental `MODE=scaleset`: GitHub assigns jobs to one runner scale set
  per repo and host, so several hosts can serve the same repos without
  starting two runners for one job. Polling stays the default.

### Changed

- `compose.yaml` and the README's custom image example use the `0.3` image.

## [0.2.2] - 2026-11-19

### Fixed

- The image includes `libatomic1`. Without it, Node from `actions/setup-node`
  and pnpm from `pnpm/action-setup` failed with "libatomic.so.1: cannot open
  shared object file" (exit code 127).

### Added

- A logo (`docs/assets/logo.svg`) and a README banner with light and dark
  variants.

### Changed

- New README section "How it works" with a step-by-step Mermaid diagram of
  one job cycle; the Runner Scale Sets proposal diagram is a Mermaid diagram
  instead of ASCII art.

## [0.2.1] - 2026-11-18

### Changed

- `compose.yaml` and the README's custom image example use the `0.2` image.
- A skipped public repo is logged once, when it is first seen, instead of on
  every repo discovery, plus one line such as `skipped 19 public repos` when
  the set of skipped public repos changes.

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

### Changed

- The runner's internal trace (`[RUNNER ...]`/`[WORKER ...]` lines, about 3,900
  per job) is no longer copied to the log; `RUNNER_TRACE=true` turns it back on.

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
