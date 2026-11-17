# Contributing

Issues and pull requests are welcome.

- **Small and focused.** One change per pull request, with a test where it
  makes sense.
- **Standard library only.** The dispatcher runs with the `python3` of a runner
  image; it must not need `pip install`. Python 3.10 or later.
- **Checks** (CI runs the same):
  ```bash
  pip install pytest ruff
  pytest -q
  ruff check . && ruff format --check .
  ```
- **Comments explain why.** Behaviour that follows from a GitHub quirk gets a
  comment that names it.
- **Security first.** Anything that widens what a job can reach (mounts,
  capabilities, defaults) needs a note in the README's Security section.

## End-to-end test

`.github/workflows/e2e.yml` runs the image against GitHub: it starts the
container for a private test repo, dispatches
[`e2e/solo-runner-e2e.yml`](e2e/solo-runner-e2e.yml) there with a label only
this container has, and checks that both jobs ran on solo-runner's runners
(one fresh runner each), that the runners are gone afterwards and that the
container stops cleanly. It runs on pull requests that touch the dispatcher,
the Dockerfile or the test, every Monday, and by hand (*Actions → E2E → Run
workflow*, optionally with a published image such as
`ghcr.io/sebiweise/solo-runner:0.1.0`).

One-time setup for a fork or a new maintainer:

1. **Test repo.** Create a **private** repo, `<owner>/solo-runner-e2e` by
   default (another name: set the repository variable `E2E_REPO` to
   `owner/name`). Commit `e2e/solo-runner-e2e.yml` to its default branch as
   `.github/workflows/solo-runner-e2e.yml`. Nothing else is needed in it.
2. **Token.** Fine-grained PAT, *Repository access*: only the test repo.
   *Repository permissions*: **Administration: Read and write** (registers the
   runners), **Actions: Read and write** (finds jobs, dispatches the test
   workflow, cancels it on failure). Give it an expiry date.
3. **Secret.** Store it in this repo as the Actions secret
   `E2E_GITHUB_TOKEN`, and also as a Dependabot secret of the same name so
   runner image bumps are tested before they are released.

Without the secret (pull requests from forks, for example) the test is
skipped. The token reaches only the test repo; anyone who can push a branch
here could read it with a modified workflow, so only maintainers should have
write access.

## Releases

Maintainers tag `vX.Y.Z` on `main`; the release workflow builds and signs the
image. Runner base image bumps from Dependabot are released automatically as
the next patch version. Add a line to `CHANGELOG.md` with every user-visible
change.
