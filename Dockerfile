# syntax=docker/dockerfile:1
# solo-runner: the official runner image plus the dispatcher.
#
# Dependabot bumps the runner version below, and every merged bump is
# released as a new patch version (.github/workflows/auto-release.yml):
# GitHub stops handing jobs to runners that fall too far behind.
FROM ghcr.io/actions/actions-runner:2.338.2

# Set by the release workflow; shows up in the dispatcher's User-Agent.
ARG VERSION=dev
ARG TARGETARCH
# GitHub-hosted runners have `docker compose`, the runner image only the CLI
# and buildx. Likewise they have the GitHub CLI (`gh`), which many workflows
# call for releases and API queries, and the runner image does not.
# Dependabot cannot see these lines: scripts/update_tools.py bumps them (daily,
# .github/workflows/update-tools.yml) with the checksums from the releases'
# checksum files. Keep the ARG names as they are.
ARG COMPOSE_VERSION=5.8.1
ARG COMPOSE_SHA256_AMD64=cf2713394ca63900da5d00e0be12045634cea93a504e1ab2f2ae24eed7f2d359
ARG COMPOSE_SHA256_ARM64=e169103deb35aa6633a78eca0a2a0658275d573dd689a33c839c71badfa00cce
ARG GH_VERSION=2.105.0
ARG GH_SHA256_AMD64=79e7ab5db738c48777b696e374f8b428f0647debfb424f44d2f426370407fe2f
ARG GH_SHA256_ARM64=b1278c75107423d818f1937fd92cafc9e5e9b606631568f68d7fb816f5885a02

USER 0
# python3 runs the dispatcher (standard library only); tini reaps whatever
# processes a job leaves behind and forwards SIGTERM. libatomic1 is on
# GitHub-hosted runners but not in the runner image, and the Node binaries
# that actions/setup-node and pnpm/action-setup install need it
# (libatomic.so.1, exit code 127 without it).
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 tini ca-certificates libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH:-amd64}" in \
         amd64) arch=x86_64 sum="$COMPOSE_SHA256_AMD64" gh_arch=amd64 gh_sum="$GH_SHA256_AMD64" ;; \
         arm64) arch=aarch64 sum="$COMPOSE_SHA256_ARM64" gh_arch=arm64 gh_sum="$GH_SHA256_ARM64" ;; \
         *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
       esac \
    # --proto =https: GitHub redirects release downloads to another host, and
    # curl must not follow a redirect to plain HTTP.
    && curl --proto '=https' --tlsv1.2 -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
       "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-${arch}" \
    && echo "${sum}  /usr/local/lib/docker/cli-plugins/docker-compose" | sha256sum -c - \
    && chmod 755 /usr/local/lib/docker/cli-plugins/docker-compose \
    # Only the binary: the tarball also holds man pages and completions.
    && gh_tar="gh_${GH_VERSION}_linux_${gh_arch}.tar.gz" \
    && curl --proto '=https' --tlsv1.2 -fsSL -o "/tmp/${gh_tar}" \
       "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${gh_tar}" \
    && echo "${gh_sum}  /tmp/${gh_tar}" | sha256sum -c - \
    && tar -xzf "/tmp/${gh_tar}" -C /usr/local/bin --strip-components=2 "gh_${GH_VERSION}_linux_${gh_arch}/bin/gh" \
    && rm "/tmp/${gh_tar}" \
    && gh --version \
    && install -d -o 1001 -g 1001 /opt/hostedtoolcache /srv/solo-runner/work \
    # The Kubernetes container hooks are for actions-runner-controller only;
    # without them the image carries fewer Node packages to keep patched.
    && rm -rf /home/runner/k8s /home/runner/k8s-novolume

COPY solo_runner /opt/solo-runner/solo_runner

# actions/setup-node and friends install here; mount a volume to keep it.
ENV SOLO_RUNNER_VERSION=${VERSION} \
    AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache \
    RUNNER_TOOL_CACHE=/opt/hostedtoolcache \
    PYTHONPATH=/opt/solo-runner \
    PYTHONUNBUFFERED=1 \
    RUNNER_DIR=/home/runner \
    WORK_DIR=/srv/solo-runner/work

USER 1001
WORKDIR /home/runner
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "solo_runner"]

LABEL org.opencontainers.image.title="solo-runner" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.description="Ephemeral self-hosted GitHub Actions runners for all repos of a personal account" \
      org.opencontainers.image.source="https://github.com/sebiweise/solo-runner" \
      org.opencontainers.image.licenses="MIT"
