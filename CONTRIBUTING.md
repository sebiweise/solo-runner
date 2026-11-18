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

## Releases

Maintainers tag `vX.Y.Z` on `main`; the release workflow builds and signs the
image. Runner base image bumps from Dependabot are released automatically as
the next patch version. Add a line to `CHANGELOG.md` with every user-visible
change.

`main` is protected by a ruleset (pull request and green CI required); see
[docs/maintainers/branch-protection.md](docs/maintainers/branch-protection.md).
