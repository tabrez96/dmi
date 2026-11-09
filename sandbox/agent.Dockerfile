# The image every agent microVM boots from. Pinned to the versions HQ's harness is tested with.
FROM docker.io/library/node:24-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git python3 python3-venv python3-pip ca-certificates \
      curl ripgrep jq less procps \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-fund --no-audit \
      @earendil-works/pi-coding-agent@0.83.0 \
      @anthropic-ai/claude-code@2.1.291 \
 && npm cache clean --force

# Swift toolchain, so agents can build and test Swift packages offline (the sandbox can't download one).
ARG SWIFT_VERSION=6.0.3
RUN apt-get update \
 && apt-get install -y --no-install-recommends binutils libc6-dev libcurl4-openssl-dev libedit2 libgcc-12-dev \
      libncurses-dev libpython3-dev libsqlite3-0 libstdc++-12-dev libxml2-dev libz3-dev pkg-config tzdata zlib1g-dev \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /opt/swift \
 && curl -fsSL "https://download.swift.org/swift-${SWIFT_VERSION}-release/debian12/swift-${SWIFT_VERSION}-RELEASE/swift-${SWIFT_VERSION}-RELEASE-debian12.tar.gz" \
    | tar xz -C /opt/swift --strip-components=1 \
 && /opt/swift/usr/bin/swift --version
ENV PATH="/opt/swift/usr/bin:${PATH}"

# uv and Python, so agents can run `uv sync` / `uv lock` / `uv run pytest` offline (the sandbox can't download a
# Python). Packages still come through the gate's PyPI allowlist. 3.14 is the current stable release.
ARG UV_VERSION=0.12.17
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR=/tmp/uv-cache UV_LINK_MODE=copy
RUN pip install --no-cache-dir --break-system-packages "uv==${UV_VERSION}" \
 && UV_PYTHON_DOWNLOADS=automatic uv python install 3.14 \
 && chmod -R a+rX /opt/python \
 && uv python list --only-installed \
 && rm -rf /tmp/uv-cache

# Non-root agent user (uid 1000, matching the host user that owns the worktrees).
RUN userdel -r node 2>/dev/null; useradd -m -u 1000 -s /bin/bash agent
COPY tools/ /opt/dmi/tools/
ENV PATH="/opt/dmi/tools:${PATH}" PI_OFFLINE=1 DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
USER agent
WORKDIR /work
