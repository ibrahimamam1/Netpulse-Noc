#!/bin/bash
# Site container startup: resolve interfaces by subnet, apply baseline netem,
# start TCP probe listener, stay in foreground, exit cleanly on SIGTERM.
set -u

SITE_NAME="${SITE_NAME:-unknown}"
TCP_PROBE_PORT="${TCP_PROBE_PORT:-8080}"
LAN_IP="${LAN_IP:-}"   # virtual enterprise LAN address (e.g. 10.10.1.1/32) bound to lo
PRIMARY_DELAY_MS="${PRIMARY_DELAY_MS:-0}"
PRIMARY_JITTER_MS="${PRIMARY_JITTER_MS:-0}"
PRIMARY_LOSS_PCT="${PRIMARY_LOSS_PCT:-0}"
BACKUP_DELAY_MS="${BACKUP_DELAY_MS:-0}"
BACKUP_JITTER_MS="${BACKUP_JITTER_MS:-0}"
BACKUP_LOSS_PCT="${BACKUP_LOSS_PCT:-0}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$SITE_NAME] $*"; }

# iface_for_prefix 172.20.1.  -> prints interface name holding that subnet
iface_for_prefix() {
  ip -o -4 addr show | awk -v p="$1" 'index($4, p) == 1 {print $2; exit}'
}

apply_netem() {  # iface delay jitter loss label
  local iface="$1" delay="$2" jitter="$3" loss="$4" label="$5"
  tc qdisc del dev "$iface" root 2>/dev/null || true
  tc qdisc add dev "$iface" root netem delay "${delay}ms" "${jitter}ms" loss "${loss}%"
  log "$label ($iface): netem delay ${delay}ms +/- ${jitter}ms loss ${loss}%"
}

# Interfaces may take a moment to appear; retry briefly.
PRIMARY_IF=""; BACKUP_IF=""
for _ in $(seq 1 20); do
  PRIMARY_IF="$(iface_for_prefix 172.20.1.)"
  BACKUP_IF="$(iface_for_prefix 172.20.2.)"
  [ -n "$PRIMARY_IF" ] && [ -n "$BACKUP_IF" ] && break
  sleep 0.5
done

if [ -z "$PRIMARY_IF" ] || [ -z "$BACKUP_IF" ]; then
  log "ERROR: could not resolve interfaces (primary='$PRIMARY_IF' backup='$BACKUP_IF')"
  exit 1
fi
log "primary=$PRIMARY_IF backup=$BACKUP_IF"

apply_netem "$PRIMARY_IF" "$PRIMARY_DELAY_MS" "$PRIMARY_JITTER_MS" "$PRIMARY_LOSS_PCT" "primary" || exit 1
apply_netem "$BACKUP_IF"  "$BACKUP_DELAY_MS"  "$BACKUP_JITTER_MS"  "$BACKUP_LOSS_PCT"  "backup"  || exit 1

# Virtual enterprise LAN: a /32 on the loopback that noc-core reaches via primary or backup
# next-hop (Layer 4 switches that route). Replies leave via the interface the request arrived on.
if [ -n "$LAN_IP" ]; then
  ip addr replace "$LAN_IP" dev lo || { log "ERROR: could not assign $LAN_IP to lo"; exit 1; }
  log "enterprise LAN address $LAN_IP assigned to lo"
fi

# TCP probe listener: replies "OK" and closes.
socat TCP-LISTEN:"${TCP_PROBE_PORT}",fork,reuseaddr,bind=0.0.0.0 SYSTEM:'echo OK' &
LISTENER_PID=$!
log "TCP probe listener on 0.0.0.0:${TCP_PROBE_PORT} (pid $LISTENER_PID)"

shutdown() {
  log "SIGTERM received, shutting down"
  kill "$LISTENER_PID" 2>/dev/null
  exit 0
}
trap shutdown TERM INT

# Foreground keep-alive; `wait` is interruptible by trapped signals.
sleep infinity &
wait $!