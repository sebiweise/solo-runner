# Protecting `main`

How `main` is protected, and how to set it up again (new fork, new
maintainer, or after a mistake). It uses a repository **ruleset**, the newer
replacement for classic branch protection; both work on public repos of a
personal account.

## What the ruleset does

| Rule | Why |
| --- | --- |
| Pull request required, 0 approvals | Every change goes through a PR, so CI runs before it reaches `main`. With a single maintainer an approval cannot be required: GitHub does not let you approve your own PR. |
| Conversations resolved | Review comments (your own notes, Copilot, Claude) cannot be merged over by accident. |
| Status checks: `lint`, `test (3.10)`, `test (3.12)`, `test (3.13)`, `image` | The jobs of `ci.yml`. They run on every PR, so they can be required. `image` includes the smoke test. Pinned to the GitHub Actions app (`integration_id` 15368) so another app cannot satisfy them with a status of the same name. |
| Branch not required to be up to date | Dependabot opens several PRs at once; requiring each to be rebased before merging would mean a rerun after every merge for little gain in a repo this size. |
| No force pushes, no deletion | History on `main` stays as released. |
| No bypass actors | The rules apply to you as admin too. Releases are not affected: `release.yml` creates the tag and the GitHub release, it never pushes to `main`. |

### Why E2E is not a required check

`e2e.yml` only runs on PRs that touch `solo_runner/`, the `Dockerfile`,
`e2e/` or the workflow itself. A required check that does not run on a PR
stays *Expected — Waiting for status* forever and blocks the merge. Watch it
on the PRs where it runs (including Dependabot's runner bumps, which are
released as soon as they are merged) and do not merge while it is red.

To make it required later, drop the `paths:` filter from its
`pull_request` trigger and add the check `e2e` here. Every PR then waits for
a real job round trip (a few minutes). Jobs skipped because the secret is
missing (PRs from forks) count as passed.

## Set it up

### With the JSON in this repo (recommended)

[`.github/rulesets/main.json`](../../.github/rulesets/main.json) holds the
ruleset above.

1. *Settings → Rules → Rulesets → New ruleset → Import a ruleset*.
2. Choose `.github/rulesets/main.json`, check the summary, *Create*.

Or with the GitHub CLI, as an admin of the repo:

```bash
gh api -X POST repos/sebiweise/solo-runner/rulesets --input .github/rulesets/main.json
```

The JSON is only a template: changing it does not change the live ruleset.
After editing it, update the ruleset in the settings (or delete and import
again).

### By hand

*Settings → Rules → Rulesets → New ruleset → New branch ruleset*:

1. **Ruleset name** `main`, **Enforcement status** *Active*.
2. **Bypass list**: leave empty.
3. **Target branches** → *Add target* → *Include default branch*.
4. Check **Restrict deletions** and **Block force pushes**.
5. Check **Require a pull request before merging**: *Required approvals* `0`,
   check *Require conversation resolution before merging*.
6. Check **Require status checks to pass** → *Add checks*, search and add
   `lint`, `test (3.10)`, `test (3.12)`, `test (3.13)`, `image`, each with
   source *GitHub Actions*. Leave *Require branches to be up to date before
   merging* unchecked. A check only shows up in the search after it has run
   once, which it has on `main`.
7. *Create*.

## Check that it works

- `git push origin main` with a local commit is rejected with
  `GH013: Repository rule violations found`.
- A PR with a failing `lint` shows *Merging is blocked*.
- *Settings → Rules → Rulesets → main → Insights* lists what the rules
  blocked or allowed.

## When something goes wrong

- **A check is renamed** (job renamed, Python version added or dropped in the
  `test` matrix): PRs wait for the old name forever. Update the list of
  required checks in the same PR, and in `.github/rulesets/main.json`.
- **Emergency fix while CI is broken upstream**: temporarily set the
  ruleset's enforcement to *Disabled*, merge, set it back to *Active*. Better
  than adding a permanent bypass.

## Related settings

Not part of the ruleset, but worth checking once under *Settings*:

- *General → Pull Requests*: *Automatically delete head branches* on.
- *Actions → General → Workflow permissions*: *Read repository contents and
  packages permissions* (the workflows ask for more where they need it), and
  *Allow GitHub Actions to create and approve pull requests* off.
- *Actions → General → Fork pull request workflows*: *Require approval for
  all outside collaborators*.
- *Code security*: private vulnerability reporting, Dependabot alerts and
  secret scanning with push protection on (`SECURITY.md` relies on the
  first).
