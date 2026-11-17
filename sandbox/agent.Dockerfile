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

# Java, Gradle and the Android SDK, so agents can build and test Android apps (Kotlin, Compose) the way they build
# Swift and Python ones. The VM's filesystem is read-only, so every SDK package a build needs is installed here;
# libraries (Maven, Google, the Gradle plugin portal) still come through the gate's allowlist.
ARG GRADLE_VERSION=8.14.3
ARG ANDROID_CMDLINE_TOOLS=13114758
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 ANDROID_HOME=/opt/android-sdk ANDROID_SDK_ROOT=/opt/android-sdk
RUN apt-get update \
 && apt-get install -y --no-install-recommends openjdk-17-jdk-headless unzip \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" -o /tmp/gradle.zip \
 && unzip -q /tmp/gradle.zip -d /opt && mv "/opt/gradle-${GRADLE_VERSION}" /opt/gradle && rm /tmp/gradle.zip \
 && mkdir -p "${ANDROID_HOME}/cmdline-tools" \
 && curl -fsSL "https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_CMDLINE_TOOLS}_latest.zip" -o /tmp/tools.zip \
 && unzip -q /tmp/tools.zip -d "${ANDROID_HOME}/cmdline-tools" \
 && mv "${ANDROID_HOME}/cmdline-tools/cmdline-tools" "${ANDROID_HOME}/cmdline-tools/latest" && rm /tmp/tools.zip \
 && yes | "${ANDROID_HOME}/cmdline-tools/latest/bin/sdkmanager" --licenses >/dev/null \
 && "${ANDROID_HOME}/cmdline-tools/latest/bin/sdkmanager" "platform-tools" "platforms;android-35" "platforms;android-36" \
      "build-tools;34.0.0" "build-tools;35.0.0" "build-tools;36.0.0" >/dev/null \
 && chmod -R a+rX "${ANDROID_HOME}" /opt/gradle
ENV PATH="/opt/gradle/bin:${ANDROID_HOME}/cmdline-tools/latest/bin:${ANDROID_HOME}/platform-tools:${PATH}"

# Non-root agent user (uid 1000, matching the host user that owns the worktrees).
RUN userdel -r node 2>/dev/null; useradd -m -u 1000 -s /bin/bash agent
COPY tools/ /opt/dmi/tools/
ENV PATH="/opt/dmi/tools:${PATH}" PI_OFFLINE=1 DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
USER agent
WORKDIR /work
