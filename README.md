# solo-runner

[![CI](https://github.com/sebiweise/solo-runner/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/sebiweise/solo-runner/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/sebiweise/solo-runner?sort=semver)](https://github.com/sebiweise/solo-runner/releases/latest)
[![Image](https://img.shields.io/badge/image-ghcr.io-blue?logo=docker)](https://github.com/sebiweise/solo-runner/pkgs/container/solo-runner)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Self-hosted GitHub Actions runners for all the private repos of a personal
account, from one container, without a GitHub organisation.**

GitHub only offers shared runners to organisations: on a personal account a
runner belongs to exactly one repository. With ten repos that means ten
runners, all idle most of the time, all able to run a job at once on the same
small server. solo-runner turns that around. It watches every repo for queued
jobs and starts a fresh, single-use runner for whichever job has waited
longest, never more than `MAX_CONCURRENT` at a time.

```
          GitHub                                   your server (outbound HTTPS only)
 ┌──────────────────────┐                     ┌──────────────────────────────────────┐
 │ repo-a  job queued ──┼──── poll (ETag) ───►│ solo-runner                          │
 │ repo-b  job queued   │                     │  1. oldest matching job: repo-a      │
 │ repo-c  (idle)       │◄─ generate-jitconfig│  2. just-in-time runner for repo-a   │
 │                      │◄──── runs job ──────│  3. runner takes 1 job, then exits   │
 └──────────────────────┘                     │  4. clean up, next job (repo-b)      │
                                              └──────────────────────────────────────┘
```

- **No organisation needed.** Works with repos owned by your user account.
- **One container.** `docker compose up -d` with a token. No Kubernetes, no
  cloud account, no webhook, no open port.
- **Global limit.** `MAX_CONCURRENT=1` means one job on this host at a time,
  across all repos. The rest wait in GitHub's queue, as they would for a busy
  runner.
- **Fresh runner per job.** Every job gets a just-in-time runner that GitHub
  removes after one job, and an empty work directory.
- **Finds your repos itself.** `REPOS=auto` serves every private repo the
  token can see. Public repos are refused by default.
- **PAT or GitHub App**, standard library Python only, amd64 and arm64.

## Contents

- [Security](#security) (read this first)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [PAT or GitHub App](#pat-or-github-app)
- [Jobs that use Docker](#jobs-that-use-docker)
- [Monitoring](#monitoring)
- [Bring your own runner image](#bring-your-own-runner-image)
- [How it compares](#how-it-compares)
- [Troubleshooting](#troubleshooting)

## Security

A self-hosted runner runs whatever code the workflow tells it to. Decide what
you trust before you start.

- **Private repos only.** On a public repo, a pull request from a fork can run
  its code on your runner. solo-runner skips public repos unless you set
  `ALLOW_PUBLIC_REPOS=true`, and logs a warning for each one it skips. Public
  repos get GitHub-hosted runners for free anyway.
- **The Docker socket is root on the host.** Without it (the default), a job
  runs as the unprivileged `runner` user inside the container. With
  `/var/run/docker.sock` mounted, any job can start a privileged container and
  own the host. Only mount it on a machine that runs nothing else, such as a
  small VPS used for CI alone. Rootless Docker or Sysbox reduce the risk.
- **Your token stays out of jobs.** The dispatcher reads `GITHUB_TOKEN` and
  removes it from the environment before any runner starts. A job with the
  Docker socket can still read it from the container's configuration
  (`docker inspect`). Prefer `GITHUB_TOKEN_FILE` (a mounted secret), and a
  GitHub App, whose tokens expire after an hour.
- **Least privilege.** A fine-grained PAT limited to the repos you want to
  serve, with *Administration: Read and write* and *Actions: Read*, and an
  expiry date.
- **Shared tool cache.** The optional `toolcache` volume is shared between
  repos. Leave it out if one repo must not see what another one installed.

Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Quick start

1. **Token.** GitHub → Settings → Developer settings → Fine-grained tokens →
   Generate. *Repository access*: only the private repos to serve.
   *Repository permissions*: **Administration: Read and write** (registers the
   runners) and **Actions: Read** (finds queued jobs).
2. **Start it** on a Linux host with Docker:
   ```bash
   mkdir solo-runner && cd solo-runner
   curl -fsSLO https://raw.githubusercontent.com/sebiweise/solo-runner/main/compose.yaml
   curl -fsSL https://raw.githubusercontent.com/sebiweise/solo-runner/main/.env.example -o .env
   chmod 600 .env && vi .env        # GITHUB_TOKEN=..., LABELS=solo
   docker compose up -d
   docker compose logs -f           # "serving N repos"
   ```
3. **Point a workflow at it.** A job is taken when every label it asks for is
   one the runner has, including all of `LABELS`:
   ```yaml
   jobs:
     build:
       runs-on: [self-hosted, solo]   # or just: solo
   ```
   To move repos without touching workflows, use a repository variable with a
   fallback, `runs-on: ${{ vars.LINUX_RUNNER || 'ubuntu-latest' }}`, and set
   `LINUX_RUNNER=solo` in each repo that should use the runner. Deleting the
   variable sends its jobs back to GitHub.

Within `POLL_INTERVAL` seconds of a push, the log shows
`owner/repo: next job ...` and `started runner ...`.

## Configuration

All settings are environment variables. Secrets can also be read from a file
with the `_FILE` suffix (`GITHUB_TOKEN_FILE`, `GITHUB_APP_PRIVATE_KEY_FILE`).

| Variable | Default | Meaning |
| --- | --- | --- |
| `GITHUB_TOKEN` | – | Fine-grained or classic PAT. `ACCESS_TOKEN` works too. |
| `GITHUB_APP_ID` | – | GitHub App instead of a PAT, with `GITHUB_APP_PRIVATE_KEY_FILE`. |
| `GITHUB_APP_INSTALLATION_ID` | auto | Only needed if the app has more than one installation. |
| `REPOS` | `auto` | `auto`, or a list of `owner/repo` separated by spaces or commas. |
| `REPO_EXCLUDE` | – | Glob patterns matched against `owner/repo` and `repo`, e.g. `*-archive`. |
| `ALLOW_PUBLIC_REPOS` | `false` | Serve public repos too. Read [Security](#security) first. |
| `LABELS` | – | Extra labels. Jobs must ask for all of them (strongly recommended, so the runner only takes jobs meant for it). `self-hosted`, `linux` and the architecture (`x64`/`arm64`) are always set. |
| `MAX_CONCURRENT` | `1` | Jobs running at the same time on this host, across all repos. |
| `POLL_INTERVAL` | `30` | Seconds between two scans (minimum 5). |
| `IDLE_TIMEOUT` | `300` | Seconds a runner waits for its job before it is removed. |
| `DISCOVERY_INTERVAL` | `600` | Seconds between two reads of the repo list. |
| `HEARTBEAT_URL` | – | URL fetched after each successful scan, at most once a minute. |
| `LOG_FORMAT` | `text` | `text` or `json` (one object per line, runner output included). |
| `RUNNER_NAME_PREFIX` | `solo-runner` | Runner names are `<prefix>-<repo>-<time>-<slot>`. Give each host its own prefix if several run against the same repos. |
| `WORK_DIR` | `/srv/solo-runner/work` | Job work directories, one per slot, emptied after every job. |
| `RUNNER_DIR` | `/home/runner` | Where the runner installation lives (for custom images). |
| `GITHUB_API_URL` | `https://api.github.com` | For GitHub Enterprise Server. |

**Rate limit.** Each scan costs about two requests per repo, but unchanged
answers come back as `304 Not Modified`, which GitHub does not count. Ten repos
at `POLL_INTERVAL=30` stay far below the 5,000 requests per hour of a PAT.
With many repos, raise `POLL_INTERVAL` or exclude repos without workflows.

## PAT or GitHub App

**Fine-grained PAT**: simplest. It sees exactly the repos you select, and
`REPOS=auto` serves those. It expires: put a reminder in your calendar.

**GitHub App**: tokens are short-lived and the app is not tied to your user
session.

1. Settings → Developer settings → GitHub Apps → New GitHub App. Name it, set
   any homepage URL, **untick Webhook → Active**.
2. *Repository permissions*: **Administration: Read and write**,
   **Actions: Read**. *Where can this app be installed*: only this account.
3. Create it, note the **App ID**, and generate a **private key** (`.pem`).
4. *Install App* → your account → only the repos to serve.
5. Mount the key and configure:
   ```yaml
   environment:
     GITHUB_APP_ID: "123456"
     GITHUB_APP_PRIVATE_KEY_FILE: /run/secrets/app.pem
   volumes:
     - ./app.pem:/run/secrets/app.pem:ro   # readable by uid 1001
   ```

## Jobs that use Docker

Many workflows run `docker build`, `docker run`, `docker compose` or use
`services:`. The image contains the Docker CLI, buildx and the compose plugin;
to use the host's daemon:

```yaml
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /srv/solo-runner/work:/srv/solo-runner/work
    group_add:
      - "999"   # getent group docker | cut -d: -f3
```

```bash
sudo install -d -o 1001 -g 1001 /srv/solo-runner/work
```

The work directory is mounted at the **same path** inside and outside the
container: the host's daemon resolves a job's `docker run -v "$PWD:/src"`
against the host's file system. Jobs with a `container:` key do not work this
way (the runner mounts its own `externals` directory, which exists only inside
the container); keep those on GitHub-hosted runners.

Disk fills up with images. A daily cron job helps:

```bash
17 4 * * * docker system prune -af --filter until=72h >/dev/null
```

## Monitoring

- **Logs**: `docker compose logs`. `LOG_FORMAT=json` for Loki, Elastic and the
  like.
- **Heartbeat**: create a heartbeat monitor (Better Stack, Healthchecks.io,
  Uptime Kuma push monitor, ...) with a 5-minute period and set
  `HEARTBEAT_URL`. It goes quiet when the container is down or hung, or when
  GitHub rejects the token.

## Bring your own runner image

The bundled image is the official `ghcr.io/actions/actions-runner` plus
Python and the Docker compose plugin. Like that image, it has far fewer tools
than GitHub's hosted runners: install what a job needs in the workflow
(`sudo apt-get install -y ...` or a `setup-*` action), so it keeps working on
`ubuntu-latest` too. If your jobs need more preinstalled tools, build on another runner
image and copy the dispatcher in:

```dockerfile
FROM your/runner-image:tag
COPY --from=ghcr.io/sebiweise/solo-runner:0.1 /opt/solo-runner /opt/solo-runner
ENV PYTHONPATH=/opt/solo-runner RUNNER_DIR=/actions-runner
ENTRYPOINT ["python3", "-m", "solo_runner"]
```

It needs `python3` (3.10 or later) and the runner's `run.sh` in `RUNNER_DIR`.

## How it compares

| Project | Fits when |
| --- | --- |
| [actions-runner-controller](https://github.com/actions/actions-runner-controller) | You run Kubernetes. |
| [GARM](https://github.com/cloudbase/garm) | You want VMs or LXD instances per job, across clouds. |
| [terraform-aws-github-runner](https://github.com/github-aws-runners/terraform-aws-github-runner) | You run on AWS. |
| [myoung34/docker-github-actions-runner](https://github.com/myoung34/docker-github-actions-runner) | One runner per repo is fine, without a shared limit. |
| [scalerunnergh](https://github.com/emon5122/scalerunnergh) | You prefer Go and runner scale sets, and list your repos by hand. |
| **solo-runner** | One small host, many personal repos, one container, minimal setup. |

## Troubleshooting

- **Job stays "Queued"**: the job's `runs-on` asks for a label solo-runner does
  not have, or misses one of `LABELS`; the
  repo is public, archived, excluded or not visible to the token (see the
  `serving N repos` line); or all slots are busy.
- **`HTTP 401` / `403`**: the token expired or lacks a permission. `403` with
  "rate limit": raise `POLL_INTERVAL`.
- **`no job after 300s, stopping runner`**: the job was cancelled, or another
  runner took it. Harmless.
- **`Permission denied` under `/srv/solo-runner/work`**: the directory on the
  host must belong to uid 1001.
- **`permission denied ... docker.sock`**: `group_add` must be the host's
  docker group id.
- **Leftover offline runners** in a repo's settings: removed automatically at
  the next start if their name starts with `RUNNER_NAME_PREFIX`.

## Development

```bash
pip install pytest ruff
pytest -q
ruff check . && ruff format --check .
docker build -t solo-runner:dev .
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Releases: [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
