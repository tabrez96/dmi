#!/usr/bin/env bash
# Network lockdown for agent microVMs.   Usage: sudo sandbox/network.sh up | check | down
#
# Agent VMs live on their own bridge (dmi-agents, 10.88.77.0/24). Rules in the mangle table's
# PREROUTING chain (evaluated before any Docker/CNI filter rules can ACCEPT a packet) drop every
# packet from that subnet unless it's TCP to the gate (10.88.77.1:8787). The gate's port is also
# closed to everything except the agent subnet and loopback.
#
# Traffic between two VMs on the bridge is switched, not routed, so it only passes through iptables (and these
# rules) when the kernel's bridge filtering (br_netfilter) is on. `up` turns it on and `check` refuses to pass
# without it, so one agent's VM can never reach another's.
set -euo pipefail
NET=dmi-agents SUBNET=10.88.77.0/24 GW=10.88.77.1 PORT=8787 CHAIN=DMI-SANDBOX

# The network must be exactly ours: on another subnet the rules below wouldn't match the VMs' traffic.
network_ok() {
  [ "$(nerdctl network inspect "$NET" --format '{{range .IPAM.Config}}{{.Subnet}} {{.Gateway}}{{end}}')" = "$SUBNET $GW" ]
}

# The Linux bridge behind the network (nerdctl names it br-<first 12 characters of the network ID>).
bridge() {
  echo "br-$(nerdctl network inspect "$NET" --format '{{.Id}}' | cut -c1-12)"
}

rules() {
  echo "-s $SUBNET -d $GW -p tcp --dport $PORT -j RETURN"   # agent -> gate: allowed
  echo "-s $SUBNET -j DROP"                                 # agent -> anything else: dropped
  echo "-p tcp --dport $PORT ! -i lo -j DROP"               # anyone else -> gate port: dropped
}

up() {
  modprobe br_netfilter
  sysctl -qw net.bridge.bridge-nf-call-iptables=1 net.bridge.bridge-nf-call-ip6tables=1
  nerdctl network inspect "$NET" >/dev/null 2>&1 ||
    nerdctl network create "$NET" --subnet "$SUBNET" --gateway "$GW" >/dev/null
  network_ok || { echo "network $NET exists but isn't $SUBNET via $GW; remove it and run up again"; exit 1; }
  iptables -t mangle -N "$CHAIN" 2>/dev/null || iptables -t mangle -F "$CHAIN"
  while read -r r; do iptables -t mangle -A "$CHAIN" $r; done < <(rules)
  iptables -t mangle -C PREROUTING -j "$CHAIN" 2>/dev/null || iptables -t mangle -I PREROUTING 1 -j "$CHAIN"
  # IPv6: agents get no v6 address, but link-local v6 still works on a bridge: drop all v6 from it.
  ip6tables -t mangle -N "$CHAIN" 2>/dev/null || ip6tables -t mangle -F "$CHAIN"
  ip6tables -t mangle -A "$CHAIN" -i "$(bridge)" -j DROP
  ip6tables -t mangle -A "$CHAIN" -p tcp --dport $PORT ! -i lo -j DROP
  ip6tables -t mangle -C PREROUTING -j "$CHAIN" 2>/dev/null || ip6tables -t mangle -I PREROUTING 1 -j "$CHAIN"
  check
}

check() {
  nerdctl network inspect "$NET" >/dev/null 2>&1 || { echo "missing network $NET"; exit 1; }
  network_ok || { echo "network $NET isn't $SUBNET via $GW"; exit 1; }
  [ "$(cat /proc/sys/net/bridge/bridge-nf-call-iptables 2>/dev/null)" = 1 ] ||
    { echo "bridge filtering is off (br_netfilter): VMs could reach each other"; exit 1; }
  iptables -t mangle -C PREROUTING -j "$CHAIN" 2>/dev/null || { echo "missing PREROUTING jump"; exit 1; }
  while read -r r; do iptables -t mangle -C "$CHAIN" $r 2>/dev/null || { echo "missing rule: $r"; exit 1; }; done < <(rules)
  ip6tables -t mangle -C PREROUTING -j "$CHAIN" 2>/dev/null || { echo "missing IPv6 PREROUTING jump"; exit 1; }
  ip6tables -t mangle -C "$CHAIN" -i "$(bridge)" -j DROP 2>/dev/null || { echo "missing IPv6 drop for $(bridge)"; exit 1; }
  echo "sandbox network OK"
}

down() {
  iptables -t mangle -D PREROUTING -j "$CHAIN" 2>/dev/null || true
  iptables -t mangle -F "$CHAIN" 2>/dev/null || true; iptables -t mangle -X "$CHAIN" 2>/dev/null || true
  ip6tables -t mangle -D PREROUTING -j "$CHAIN" 2>/dev/null || true
  ip6tables -t mangle -F "$CHAIN" 2>/dev/null || true; ip6tables -t mangle -X "$CHAIN" 2>/dev/null || true
  echo "sandbox rules removed (network $NET kept)"
}

case "${1:-check}" in
  up|check|down) "${1:-check}" ;;
  *) echo "usage: $0 up|check|down" >&2; exit 2 ;;
esac
