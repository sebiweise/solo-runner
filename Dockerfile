# syntax=docker/dockerfile:1
# solo-runner: the official runner image plus the dispatcher.
#
# Dependabot bumps the runner version below, and every merged bump is
# released as a new patch version (.github/workflows/auto-release.yml):
# GitHub stops handing jobs to runners that fall too far behind.
FROM ghcr.io/actions/actions-runner:2.338.2

# Set by the release workflow; shows up in the dispatcher's User-Agent.
ARG VERSION=dev

USER root
# python3 runs the dispatcher (standard library only); tini reaps whatever
# processes a job leaves behind and forwards SIGTERM.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o runner -g runner /opt/hostedtoolcache /srv/solo-runner/work

COPY solo_runner /opt/solo-runner/solo_runner

# actions/setup-node and friends install here; mount a volume to keep it.
ENV SOLO_RUNNER_VERSION=${VERSION} \
    AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache \
    RUNNER_TOOL_CACHE=/opt/hostedtoolcache \
    PYTHONPATH=/opt/solo-runner \
    PYTHONUNBUFFERED=1 \
    RUNNER_DIR=/home/runner \
    WORK_DIR=/srv/solo-runner/work

USER runner
WORKDIR /home/runner
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "solo_runner"]

LABEL org.opencontainers.image.title="solo-runner" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.description="Ephemeral self-hosted GitHub Actions runners for all repos of a personal account" \
      org.opencontainers.image.source="https://github.com/sebiweise/solo-runner" \
      org.opencontainers.image.licenses="MIT"
