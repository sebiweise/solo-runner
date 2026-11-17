# Security policy

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private reporting instead:
*Security* tab → *Report a vulnerability*. You will get an answer within a
week. Fixes are released as a new patch version and noted in the changelog.

## Scope

In scope: the dispatcher (`solo_runner/`), the image as published on GHCR, the
example `compose.yaml` and the documented defaults.

Known and documented, not vulnerabilities: a job on a runner with the Docker
socket mounted has root on the host, and can read the container's
configuration including environment variables. See the "Security" section of
the README.

## Supported versions

Only the latest minor release gets fixes. The image follows the
`actions/runner` releases; run a recent tag.
