## What and why

<!-- One change per pull request. What does it change, and why? Link the issue if there is one. -->

## Checklist

- [ ] `pytest -q`, `ruff check .` and `ruff format --check .` pass
- [ ] Tests added or updated where it makes sense
- [ ] The dispatcher still needs only the Python standard library (3.10+)
- [ ] A line in `CHANGELOG.md` under *Unreleased* for every user-visible change
- [ ] Anything that widens what a job can reach (mounts, capabilities, defaults) has a paragraph in the README's *Security* section
- [ ] README updated for new or changed options
