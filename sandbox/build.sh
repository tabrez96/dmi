#!/usr/bin/env bash
# Build the agent image and load it into containerd (where Kata runs it).  Usage: sandbox/build.sh [tag]
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="${1:-dmi-agent:2}"
sudo docker build -q -f sandbox/agent.Dockerfile -t "$TAG" . >/dev/null
sudo docker save "$TAG" | sudo nerdctl load -q >/dev/null
# Boot it once under Kata; every tool must run, or the build fails.
sudo nerdctl run --rm --runtime io.containerd.kata.v2 "$TAG" sh -ec '
  uname -r
  pi --version
  claude --version
  swift --version | head -1
  uv --version
  uv python find 3.14
' 2>/dev/null
echo "built and loaded $TAG"
