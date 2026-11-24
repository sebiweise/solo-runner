# CLAUDE.md

Guidance for Claude (and other coding agents) working in this repository.
User-facing documentation is in [README.md](README.md); contribution rules in
[CONTRIBUTING.md](CONTRIBUTING.md). This file adds what an agent needs to work
here without asking.

## What this is

solo-runner is one container that runs ephemeral, just-in-time (JIT)
self-hosted GitHub Actions runners for all private repos of a **personal**
account (no organisation), with a global limit `MAX_CONCURRENT` across all
repos. It polls the GitHub REST API (with ETags) for queued jobs, registers a
JIT runner for the repo with the oldest matching job, runs it in a free slot
and cleans up. No webhooks, no inbound port.

Released: v0.3.2 (`ghcr.io/sebiweise/solo-runner:0.3.2`). An end-to-end test
(`.github/workflows/e2e.yml`) runs real jobs against a private test repo when
the `E2E_GITHUB_TOKEN` secret is set; see CONTRIBUTING.md.

## Layout

```
solo_runner/            the dispatcher, Python standard library only
  __init__.py           __version__ (also the package version in pyproject.toml)
  __main__.py           entry point: python3 -m solo_runner; exit 2 on config errors
  config.py             Config dataclass, load() from env; *_FILE secrets, removed from env;
                        optional CONFIG_FILE with labels/max_concurrent per repo (RepoSettings)
  miniyaml.py           the small YAML subset CONFIG_FILE accepts (no PyYAML)
  dispatcher.py         job_matches, pick_jobs, filter_repos, slots, main loop (Dispatcher)
  scaleset.py           client for the runner scale set API (Actions service, preview)
  scaleset_mode.py      MODE=scaleset: one scale set + listener thread per repo (ScaleSetDispatcher)
  github.py             REST client on urllib: ETag cache, pagination, jobs, JIT config, runners
  appauth.py            GitHub App JWT: own PEM/DER parser + RS256, no `cryptography`
  log.py                text or JSON lines; passthrough() for the runner's own output
  metrics.py            optional Prometheus /metrics on METRICS_PORT; every metric is declared in DEFINITIONS
tests/                  pytest, no network; conftest.py puts the repo on sys.path
e2e/                    workflow the E2E test dispatches in the test repo
Dockerfile              ghcr.io/actions/actions-runner + python3, tini, libatomic1, docker compose plugin, gh
compose.yaml, .env.example   what users copy to their host
.github/workflows/
  ci.yml                ruff, hadolint, pytest (3.10/3.12/3.13), image build + smoke test, Trivy (report only)
  release.yml           build amd64+arm64, push to GHCR, cosign sign, SBOM, GitHub release + tag
  auto-release.yml      on a merged base-image, compose or gh bump in Dockerfile: release next patch version
  update-tools.yml      daily: pull request for a new docker compose plugin or gh release
  e2e.yml               real jobs through the image against a private test repo (skipped without secret)
.github/dependabot.yml  runner base image (daily), GitHub Actions (weekly, grouped)
```

### How the dispatcher works

- `Dispatcher.run()`: list repos (retry until it works), remove stale offline
  runners with our name prefix, then loop `tick()` every `POLL_INTERVAL`
  (earlier when a slot frees up, via `_wake`).
- `tick()`: refresh repos every `DISCOVERY_INTERVAL`; if slots are free, scan
  all repos in parallel (`ThreadPoolExecutor`, max 16), pick the oldest
  matching jobs with `pick_jobs()` and `start()` a slot per job. The heartbeat
  is sent only when every repo answered.
- A queued job can sit in a run that is already `in_progress` (`needs:`), or
  one GitHub reports as `pending` or `waiting` (concurrency, environments), so
  `queued_jobs()` searches runs in all four states (`RUN_STATES`).
- `pick_jobs()` skips as many jobs per repo as that repo has started but idle
  runners, since those will take one of them.
- Each slot runs in its own thread: empty `WORK_DIR/slot-N`, a hard-linked copy
  of the runner installation in `SLOTS_DIR/slot-N`, `generate-jitconfig`, then
  `./run.sh --jitconfig ...`. "Running job:" in the runner's output marks the
  slot busy; without it the runner is stopped after `IDLE_TIMEOUT`. The runner
  is deleted via the API afterwards (404 is fine) and both directories are
  cleared.
- `job_matches()`: every label the job asks for must be one the runner has,
  and the job must ask for all extra `LABELS`. Default labels are
  `self-hosted`, `linux` and the architecture (`x64`/`arm64`).
- Secrets (`GITHUB_TOKEN`, `ACCESS_TOKEN`, `GITHUB_APP_PRIVATE_KEY`, and their
  `_FILE` variants) are popped from `os.environ` in `config.load()`, so runners
  and jobs never inherit them. Keep it that way for any new secret.

## Checks

Run all three before every push; CI runs the same (plus hadolint and an image
build):

```bash
pip install pytest==9.2.0 ruff==0.17.5   # the versions pinned in ci.yml
pytest -q
ruff check .
ruff format --check .
```

- Ruff config is in `pyproject.toml` (line length 120, target py310).
- Tests must not touch the network. Use the fakes in the existing tests
  (a fake `GitHub` object or an `opener` passed to `GitHub(...)`) and
  `tmp_path` for directories.
- When the Dockerfile changes and Docker is available, also run
  `docker build -t solo-runner:dev .`; the CI smoke test expects exit code 2
  and a message naming `GITHUB_TOKEN` when started without a token.

## Rules

- **Standard library only** in `solo_runner/`, Python 3.10 or later. No
  `pip install` at runtime, no new entries in `dependencies`. Tests may use
  pytest only.
- **Language**: code, comments, README, CHANGELOG, commits and PRs in English.
- **Branch + PR**, never commit to `main` directly. One topic per PR.
- **CHANGELOG.md**: one line under `## [Unreleased]` for every user-visible
  change (behaviour, configuration, image contents, documentation users rely
  on). Internal-only changes (tests, CI, this file) need none.
- **Security section**: anything that widens what a job can reach (mounts,
  capabilities, permissions, defaults, new env passed to runners) needs a
  paragraph in the README's Security section, and in `SECURITY.md` if it is a
  known, accepted risk.
- **New settings** are environment variables, documented in the README's
  Configuration table and in `.env.example`, validated in `config.load()` with
  a clear `ConfigError`, and covered by a test in `tests/test_config.py`.
- **Comments explain why.** Behaviour that follows from a GitHub quirk gets a
  comment that names the quirk.
- **Bigger design decisions** (new modes, new files users must write, new
  ports, new dependencies of the image) are proposed to the maintainer first,
  before building them.
- **Action versions**: workflow actions use version tags (major tag, e.g.
  `@v7`, where the action publishes one); keep that for new steps.

## Release process

Versions follow SemVer; runner base image bumps are patch releases.

1. In a PR: move the `## [Unreleased]` entries in `CHANGELOG.md` to a new
   `## [X.Y.Z] - YYYY-MM-DD` section and set `__version__` in
   `solo_runner/__init__.py` to `X.Y.Z`. Merge it.
2. Run the **Release** workflow (`release.yml`) via `workflow_dispatch` on
   `main` with `version: X.Y.Z` (no leading `v`). It builds and pushes
   `ghcr.io/sebiweise/solo-runner:X.Y.Z`, `:X.Y` and `:latest`, signs the
   image with cosign and creates the GitHub release, which creates the tag
   `vX.Y.Z`.
3. Do **not** push tags yourself: tag pushes are not possible from cloud agent
   sessions, and `release.yml` creates the tag anyway.

Automatic patch releases: when a Dependabot bump of the `FROM` line in the
`Dockerfile` is merged, `auto-release.yml` takes the highest `v*.*.*` tag,
increments the patch number and calls `release.yml`. These releases do not
update `CHANGELOG.md` or `__version__`; the image version comes from the
`VERSION` build argument (`SOLO_RUNNER_VERSION` at runtime, used in the
User-Agent).

The docker compose plugin and GitHub CLI versions in the `Dockerfile`
(`COMPOSE_VERSION`, `GH_VERSION` and their SHA-256 sums) are not seen by
Dependabot. `update-tools.yml` (daily, one matrix job per tool, via
`scripts/update_tools.py --tool compose|gh`) opens a `deps/compose-vX.Y.Z` or
`deps/gh-vX.Y.Z` pull request for each new release, and `auto-release.yml`
releases a merged bump as the next patch version too.
