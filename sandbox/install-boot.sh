#!/usr/bin/env bash
# Install the network lockdown as a boot service.  Usage: sudo sandbox/install-boot.sh
# Copies the script to a root-owned location so a file in a user's home never runs as root.
set -euo pipefail
cd "$(dirname "$0")"
install -o root -g root -m 0755 network.sh /usr/local/sbin/dmi-sandbox-network
install -o root -g root -m 0644 dmi-sandbox-network.service /etc/systemd/system/dmi-sandbox-network.service
systemctl daemon-reload
if systemctl is-active --quiet dmi-sandbox-network.service; then
  systemctl reload dmi-sandbox-network.service     # already running: apply the updated rules
fi
systemctl enable --now dmi-sandbox-network.service
echo "installed: $(systemctl is-enabled dmi-sandbox-network) / $(systemctl is-active dmi-sandbox-network)"
