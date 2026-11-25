#!/usr/bin/env bash
# =============================================================================
# NetPulse NOC - automated end-to-end live demo (~2 minutes)
#
#   Phase 1  Baseline            T+000s .. T+015s   normal RTT to Douala LAN (10.10.1.1) over fiber
#   Phase 2  Proactive detection T+015s .. T+045s   latency-drift -> DEGRADING before 200 ms
#   Phase 3  Fiber cut           T+045s .. T+075s   automatic failover to backup, traffic verified
#   Phase 4  Restoration         T+075s .. ~T+110s  reset -> RECOVERY_HOLD (20 s) -> failback
#
# Watch it live:  http://localhost:3000   (NetPulse NOC Overview, no login)
#
# Usage:   ./scripts/run_demo.sh
# Needs:   the stack up (docker compose up -d --build), docker, curl, python3 on the host.
# Tunables (environment): ENGINE_API CONTROLLER_API PROMETHEUS_URL GRAFANA_URL FAULT_INJECTOR
#   CORE_CONTAINER DRIFT_DURATION DRIFT_TARGET_MS T_BASELINE_END T_DRIFT_END T_CUT_END DEMO_COUNTDOWN
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ENGINE_API="${ENGINE_API:-http://localhost:9200}"
CONTROLLER_API="${CONTROLLER_API:-http://localhost:9300}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
FAULT_INJECTOR="${FAULT_INJECTOR:-$ROOT_DIR/topology/fault_injector.py}"
CORE="${CORE_CONTAINER:-noc-core}"
PYTHON="${PYTHON:-python3}"

SITE="douala"
LAN_IP="10.10.1.1"
PRIMARY_NH="172.20.1.11"
BACKUP_NH="172.20.2.11"

# Timeline in seconds from demo start. DRIFT_TARGET_MS stays below the 200 ms CRITICAL threshold so
# that Phase 2 shows *proactive* detection and the failover in Phase 3 is clearly caused by the cut.
T_BASELINE_END="${T_BASELINE_END:-15}"
T_DRIFT_END="${T_DRIFT_END:-45}"
T_CUT_END="${T_CUT_END:-75}"
DRIFT_DURATION="${DRIFT_DURATION:-30}"
DRIFT_TARGET_MS="${DRIFT_TARGET_MS:-150}"
DEMO_COUNTDOWN="${DEMO_COUNTDOWN:-8}"

if [ -t 1 ]; then
  BOLD=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; CYA=$'\e[36m'; RST=$'\e[0m'
else
  BOLD=""; DIM=""; RED=""; GRN=""; YEL=""; CYA=""; RST=""
fi

DEMO_STARTED=0
FAULT_ACTIVE=0
DRIFT_PID=""
WARNINGS=0
LOGDIR="$(mktemp -d 2>/dev/null || echo /tmp)"

# results collected for the final summary
R_BASE_PING="-"; R_DEGRADE_T="-"; R_DEGRADE_RTT="-"; R_FAILOVER_MS="-"; R_FAILOVER_E2E="-"
R_BACKUP_PING="-"; R_FAILBACK_T="-"; R_PRIMARY_PING="-"

# ------------------------------------------------------------------ output helpers
tlabel() { if [ "$DEMO_STARTED" = 1 ]; then printf 'T+%03ds' "$SECONDS"; else printf ' pre  '; fi; }
info()   { printf '%s %s[%s]%s %s\n' "$(date +%H:%M:%S)" "$DIM" "$(tlabel)" "$RST" "$*"; }
good()   { printf '%s %s[%s]%s %s✔ %s%s\n' "$(date +%H:%M:%S)" "$DIM" "$(tlabel)" "$RST" "$GRN" "$*" "$RST"; }
warn()   { WARNINGS=$((WARNINGS + 1)); printf '%s %s[%s]%s %s⚠ %s%s\n' "$(date +%H:%M:%S)" "$DIM" "$(tlabel)" "$RST" "$YEL" "$*" "$RST"; }
die()    { printf '%s✖ %s%s\n' "$RED" "$*" "$RST" >&2; exit 2; }
banner() {
  printf '\n%s%s════════════════════════════════════════════════════════════════════%s\n' "$BOLD" "$CYA" "$RST"
  printf '%s%s  [%s] %s%s\n' "$BOLD" "$CYA" "$(tlabel)" "$*" "$RST"
  printf '%s%s════════════════════════════════════════════════════════════════════%s\n' "$BOLD" "$CYA" "$RST"
}

# ------------------------------------------------------------------ data helpers
jget()  { curl -fsS -m 3 "$1" 2>/dev/null; }
now_s() { "$PYTHON" -c 'import time; print("%.3f" % time.time())'; }
lt()    { "$PYTHON" -c "import sys; sys.exit(0 if float('$1') < float('$2') else 1)" 2>/dev/null; }

# engine_link <site> <link>  ->  "<state> <anomaly_score> <rtt_mean_ms> <loss_pct> <incident_id|->"
engine_link() {
  jget "$ENGINE_API/api/v1/status" | "$PYTHON" -c '
import sys, json
site, link = sys.argv[1], sys.argv[2]
for l in json.load(sys.stdin)["links"]:
    if l["site"] == site and l["link"] == link:
        t = l["telemetry"]
        print(l["health_state"], l["anomaly_score"], round(t["rtt_mean_ms"], 1), round(t["packet_loss_pct"], 1), l.get("active_incident_id") or "-")
        break
else:
    print("UNKNOWN 0 0 0 -")
' "$1" "$2"
}

# engine_summary -> "<links_not_normal> <links_total> <warmup_progress>"
engine_summary() {
  jget "$ENGINE_API/api/v1/status" | "$PYTHON" -c '
import sys, json
links = json.load(sys.stdin).get("links", [])
bad = [l for l in links if l["health_state"] != "NORMAL"]
prog = ",".join(sorted({l.get("warmup_progress", "") for l in bad if l["health_state"] == "WARMING_UP"}))
print(len(bad), len(links), prog or "-")
'
}

# ctl_site <site> -> "<fsm_state> <active_link> <kernel_via> <hold_remaining_s> <last_failover_ms> <flaps>"
ctl_site() {
  jget "$CONTROLLER_API/api/v1/failover/status" | "$PYTHON" -c '
import sys, json
for s in json.load(sys.stdin)["sites"]:
    if s["site"] == sys.argv[1]:
        r = s.get("verified_kernel_route") or {}
        print(s["fsm_state"], s["active_link"], r.get("via") or "none", s["hold_down_remaining_s"],
              s["last_failover_duration_ms"], s["flap_count_in_window"])
        break
else:
    print("UNKNOWN - none 0 None 0")
' "$1"
}

load_ctl() {  # sets FSM ACTIVE VIA HOLD DUR FLAPS (never leaves them unset)
  FSM=""; ACTIVE=""; VIA=""; HOLD=""; DUR=""; FLAPS=""
  read -r FSM ACTIVE VIA HOLD DUR FLAPS <<<"$(ctl_site "$SITE")"
  : "${FSM:=UNREACHABLE}" "${ACTIVE:=-}" "${VIA:=none}" "${HOLD:=0}" "${DUR:=None}" "${FLAPS:=0}"
}

load_engine() {  # sets EST ESC ERTT ELOSS EINC for douala/primary
  EST=""; ESC=""; ERTT=""; ELOSS=""; EINC=""
  read -r EST ESC ERTT ELOSS EINC <<<"$(engine_link "$SITE" primary)"
  : "${EST:=UNREACHABLE}" "${ESC:=?}" "${ERTT:=?}" "${ELOSS:=?}" "${EINC:=-}"
}

status_line() {
  load_engine; load_ctl
  info "   ${SITE}/primary: ${BOLD}${EST}${RST} score=${ESC} rtt=${ERTT}ms loss=${ELOSS}%  |  FSM=${BOLD}${FSM}${RST} path=${ACTIVE} via ${VIA}"
}

# core_ping <count> : ping the Douala LAN IP from inside noc-core; sets PING_AVG / PING_LOSS
core_ping() {
  local n="$1" out
  info "docker exec $CORE ping -c $n $LAN_IP"
  out="$(docker exec "$CORE" ping -c "$n" -W 2 "$LAN_IP" 2>&1)"
  printf '%s\n' "$out" | grep -E 'packet loss|min/avg/max|round-trip' | sed "s/^/            ${DIM}/; s/\$/${RST}/"
  PING_AVG="$(printf '%s\n' "$out" | grep -oE '= [0-9.]+/[0-9.]+/[0-9.]+' | head -1 | cut -d/ -f2)"
  PING_LOSS="$(printf '%s\n' "$out" | grep -oE '[0-9.]+% packet loss' | head -1 | grep -oE '^[0-9.]+')"
  : "${PING_AVG:=-}" "${PING_LOSS:=100}"
}

wait_until() {  # wait_until <T> : sleep until T seconds into the demo, one status line every 5 s
  local target="$1" last=-100
  while [ "$SECONDS" -lt "$target" ]; do
    if [ $((SECONDS - last)) -ge 5 ]; then status_line; last=$SECONDS; fi
    sleep 1
  done
}

inject() {  # inject <fault> [extra args...]
  local fault="$1"; shift
  "$PYTHON" "$FAULT_INJECTOR" --site "$SITE" --link primary --fault "$fault" "$@"
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if [ -n "$DRIFT_PID" ] && kill -0 "$DRIFT_PID" 2>/dev/null; then kill "$DRIFT_PID" 2>/dev/null; wait "$DRIFT_PID" 2>/dev/null; fi
  if [ "$FAULT_ACTIVE" = 1 ]; then
    printf '\n%sDemo interrupted: clearing injected fault on %s/primary...%s\n' "$YEL" "$SITE" "$RST"
    inject reset >/dev/null 2>&1 || printf '%sCould not reset automatically; run: %s %s --site %s --link primary --fault reset%s\n' "$RED" "$PYTHON" "$FAULT_INJECTOR" "$SITE" "$RST"
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# =============================================================================
# PRE-FLIGHT
# =============================================================================
banner "PRE-FLIGHT CHECKS"

command -v docker  >/dev/null 2>&1 || die "docker not found in PATH"
command -v curl    >/dev/null 2>&1 || die "curl not found in PATH"
command -v "$PYTHON" >/dev/null 2>&1 || die "$PYTHON not found in PATH"
[ -f "$FAULT_INJECTOR" ] || die "fault injector not found: $FAULT_INJECTOR"

missing=0
for c in site-douala site-yaounde site-edge "$CORE" noc-engine noc-controller prometheus grafana; do
  if [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = "true" ]; then
    good "container $c is running"
  else
    printf '%s✖ container %s is not running%s\n' "$RED" "$c" "$RST"; missing=1
  fi
done
[ "$missing" = 0 ] || die "start the stack first:  docker compose up -d --build"

wait_http() {  # wait_http <label> <url> <python-condition on parsed JSON or ''> [timeout]
  local label="$1" url="$2" cond="$3" timeout="${4:-60}" end=$((SECONDS + ${4:-60}))
  while [ "$SECONDS" -lt "$end" ]; do
    if [ -z "$cond" ]; then
      curl -fsS -m 3 -o /dev/null "$url" 2>/dev/null && { good "$label OK"; return 0; }
    else
      jget "$url" | "$PYTHON" -c "import sys, json; d = json.load(sys.stdin); sys.exit(0 if ($cond) else 1)" 2>/dev/null && { good "$label OK"; return 0; }
    fi
    sleep 1
  done
  die "$label not healthy after ${timeout}s ($url)"
}
wait_http "noc-engine /health (prober_connected)"    "$ENGINE_API/health"          "d.get('prober_connected') is True"
wait_http "noc-controller /health (engine+docker)"   "$CONTROLLER_API/health"      "d.get('engine_connected') is True and d.get('docker_connected') is True"
wait_http "prometheus /-/healthy"                    "$PROMETHEUS_URL/-/healthy"   ""
wait_http "grafana /api/health"                      "$GRAFANA_URL/api/health"     "d.get('database') == 'ok'"

up="$(jget "$PROMETHEUS_URL/api/v1/targets" | "$PYTHON" -c 'import sys, json; print(sum(1 for t in json.load(sys.stdin)["data"]["activeTargets"] if t["health"] == "up"))' 2>/dev/null)"
if [ "${up:-0}" = "3" ]; then good "prometheus scraping all 3 targets (prober, engine, controller)"; else warn "prometheus reports ${up:-0}/3 targets UP (see $PROMETHEUS_URL/targets)"; fi

# start from a clean slate: no leftover fault, no operator-pending flap lock, empty flap window
inject reset >/dev/null 2>&1 || warn "initial fault reset failed"
load_ctl
if [ "$FSM" = "FLAP_LOCKED" ]; then
  info "$SITE is FLAP_LOCKED from an earlier run: acknowledging via API"
  curl -fsS -m 3 -X POST "$CONTROLLER_API/api/v1/failover/$SITE/ack" >/dev/null 2>&1 || warn "ack failed"
fi
end=$((SECONDS + 240)); announced=0
while [ "$SECONDS" -lt "$end" ]; do
  load_ctl
  if [ "$FSM" = "HEALTHY_PRIMARY" ] && [ "$FLAPS" = "0" ]; then break; fi
  if [ "$announced" = 0 ] || [ $((SECONDS % 15)) -eq 0 ]; then
    info "waiting for $SITE to settle: FSM=$FSM flap-count-in-window=$FLAPS (flap window is 120 s)"; announced=1
  fi
  sleep 1
done
[ "$FSM" = "HEALTHY_PRIMARY" ] && good "$SITE FSM is HEALTHY_PRIMARY with an empty flap window" || warn "$SITE FSM is $FSM (demo may not behave as expected)"

info "waiting for noc-engine warmup (all 6 links NORMAL)..."
end=$((SECONDS + 240)); last=-100
while [ "$SECONDS" -lt "$end" ]; do
  read -r bad total prog <<<"$(engine_summary)"
  if [ "${total:-0}" = "6" ] && [ "${bad:-1}" = "0" ]; then good "all 6 links NORMAL: warmup complete"; break; fi
  if [ $((SECONDS - last)) -ge 5 ]; then info "   warmup in progress: ${bad:-?}/${total:-?} links not NORMAL (progress ${prog:--})"; last=$SECONDS; fi
  sleep 1
done
[ "${bad:-1}" = "0" ] || warn "engine did not report all links NORMAL within 240 s"

printf '\n%s%s  LIVE DASHBOARD:  %s   (NetPulse NOC Overview, auto-refresh 2s, no login)%s\n\n' "$BOLD" "$GRN" "$GRAFANA_URL" "$RST"
if [ "$DEMO_COUNTDOWN" -gt 0 ] 2>/dev/null; then
  info "Open the dashboard now. Demo starts in ${DEMO_COUNTDOWN}s (Ctrl-C to abort)..."
  sleep "$DEMO_COUNTDOWN"
fi

# =============================================================================
SECONDS=0; DEMO_STARTED=1
# PHASE 1: BASELINE
# =============================================================================
banner "PHASE 1: BASELINE - healthy fiber link (T+0s .. T+${T_BASELINE_END}s)"
info "Douala LAN $LAN_IP is routed over wan_primary via $PRIMARY_NH; expecting ~10 ms RTT."
core_ping 5
R_BASE_PING="$PING_AVG"
if [ "$PING_LOSS" = "0" ] || [ "$PING_LOSS" = "0.0" ]; then good "baseline RTT to $LAN_IP: avg ${PING_AVG} ms, 0% loss"; else warn "baseline ping shows ${PING_LOSS}% loss"; fi
wait_until "$T_BASELINE_END"

# =============================================================================
# PHASE 2: PROACTIVE ANOMALY DETECTION
# =============================================================================
banner "PHASE 2: PROACTIVE DETECTION - latency drift on $SITE/primary (T+${T_BASELINE_END}s .. T+${T_DRIFT_END}s)"
info "Injecting latency-drift: RTT ramps 10 ms -> ${DRIFT_TARGET_MS} ms over ${DRIFT_DURATION}s (hard CRITICAL threshold is 200 ms)."
FAULT_ACTIVE=1
inject latency-drift --duration "$DRIFT_DURATION" --target-delay "$DRIFT_TARGET_MS" >"$LOGDIR/drift.log" 2>&1 &
DRIFT_PID=$!
T_DEGRADING=""; last=-100
while [ "$SECONDS" -lt "$T_DRIFT_END" ]; do
  load_engine
  if [ -z "$T_DEGRADING" ]; then
    if [ "$EST" = "DEGRADING" ] || [ "$EST" = "CRITICAL" ]; then
      T_DEGRADING="$SECONDS"; R_DEGRADE_T="T+${SECONDS}s"; R_DEGRADE_RTT="$ERTT"
      if [ "$EST" = "DEGRADING" ] && lt "$ERTT" 200; then
        good "★ ${SITE}/primary -> DEGRADING at T+${SECONDS}s: mean RTT ${ERTT} ms (< 200 ms CRITICAL threshold), anomaly score ${ESC}, incident #${EINC}"
      else
        warn "${SITE}/primary reached $EST (rtt ${ERTT} ms) before a DEGRADING warning was observed"
      fi
      sleep 1.5; load_ctl
      info "   controller FSM -> ${BOLD}${FSM}${RST} (failover armed, traffic still on ${ACTIVE} via ${VIA})"
    elif [ $((SECONDS - last)) -ge 5 ]; then
      info "   drifting... ${SITE}/primary state=${EST} score=${ESC} rtt=${ERTT}ms (waiting for the detector)"; last=$SECONDS
    fi
  elif [ $((SECONDS - last)) -ge 5 ]; then
    status_line; last=$SECONDS
  fi
  sleep 1
done
wait "$DRIFT_PID" 2>/dev/null; DRIFT_PID=""
[ -n "$T_DEGRADING" ] || warn "no DEGRADING detection within the drift window"

# =============================================================================
# PHASE 3: FIBER CUT & AUTOMATED FAILOVER
# =============================================================================
banner "PHASE 3: FIBER CUT - automatic failover to wan_backup (T+${T_DRIFT_END}s .. T+${T_CUT_END}s)"
info "Cutting the primary fiber (100% loss) on $SITE ..."
inject fiber-cut >/dev/null 2>&1 || warn "fiber-cut injection reported an error"
T_CUT="$(now_s)"
end=$((SECONDS + 20))
while [ "$SECONDS" -lt "$end" ]; do
  load_ctl
  case "$FSM" in FAILED_OVER_BACKUP|FLAP_LOCKED) break ;; esac
  sleep 0.25
done
T_FO="$(now_s)"
R_FAILOVER_E2E="$("$PYTHON" -c "print('%.1f' % (${T_FO} - ${T_CUT}))")"
if [ "$FSM" = "FAILED_OVER_BACKUP" ] || [ "$FSM" = "FLAP_LOCKED" ]; then
  R_FAILOVER_MS="$DUR"
  good "★ controller FSM -> ${FSM} ${R_FAILOVER_E2E}s after the cut (probe detection + engine + controller)"
  info "   failover duration (controller: trigger -> 'ip route replace' done): ${BOLD}${DUR} ms${RST}"
  info "   kernel route in $CORE: ${LAN_IP}/32 via ${BOLD}${VIA}${RST} (active link: ${ACTIVE})"
  [ "$VIA" = "$BACKUP_NH" ] && good "route points at the backup next hop ($BACKUP_NH)" || warn "expected route via $BACKUP_NH, kernel says via $VIA"
  [ "$FSM" = "FLAP_LOCKED" ] && warn "site is FLAP_LOCKED (run the demo again after the 120 s flap window, or POST .../ack)"
else
  warn "controller did not fail over within 20 s (FSM=$FSM)"
fi
info "Proving real traffic flows over the backup link:"
core_ping 3
R_BACKUP_PING="$PING_AVG"
if [ "$PING_LOSS" = "0" ] || [ "$PING_LOSS" = "0.0" ]; then good "$LAN_IP reachable over wan_backup: avg ${PING_AVG} ms (was ${R_BASE_PING} ms on fiber)"; else warn "ping to $LAN_IP shows ${PING_LOSS}% loss after failover"; fi
wait_until "$T_CUT_END"

# =============================================================================
# PHASE 4: RESTORATION & HOLD-DOWN FAILBACK
# =============================================================================
banner "PHASE 4: RESTORATION - reset, hold-down and failback (T+${T_CUT_END}s ..)"
info "Restoring the primary fiber (reset to baseline netem) ..."
inject reset >/dev/null 2>&1 && FAULT_ACTIVE=0 || warn "reset reported an error"
info "noc-engine must see the link clean again, then the controller holds down before failing back."
end=$((SECONDS + 180)); prev=""; last=-100
while [ "$SECONDS" -lt "$end" ]; do
  load_ctl
  if [ "$FSM" != "$prev" ]; then
    if [ -z "$prev" ]; then info "controller FSM: currently ${BOLD}${FSM}${RST} (path=${ACTIVE} via ${VIA})"
    else info "controller FSM: ${prev} -> ${BOLD}${FSM}${RST} (path=${ACTIVE} via ${VIA})"; fi
    [ "$FSM" = "RECOVERY_HOLD" ] && info "   hold-down timer started: ${HOLD}s remaining"
    prev="$FSM"; last=$SECONDS
  elif [ $((SECONDS - last)) -ge 5 ]; then
    if [ "$FSM" = "RECOVERY_HOLD" ]; then info "   hold-down countdown: ${HOLD}s remaining (primary must stay NORMAL)"; else status_line; fi
    last=$SECONDS
  fi
  [ "$FSM" = "HEALTHY_PRIMARY" ] && break
  sleep 1
done
if [ "$FSM" = "HEALTHY_PRIMARY" ]; then
  R_FAILBACK_T="T+${SECONDS}s"
  good "★ automatic failback complete: ${SITE} is HEALTHY_PRIMARY at T+${SECONDS}s"
  [ "$VIA" = "$PRIMARY_NH" ] && good "kernel route restored: ${LAN_IP}/32 via ${PRIMARY_NH} (wan_primary)" || warn "expected route via $PRIMARY_NH, kernel says via $VIA"
  core_ping 3
  R_PRIMARY_PING="$PING_AVG"
  good "$LAN_IP back on fiber: avg ${PING_AVG} ms"
else
  warn "failback to primary not completed within 180 s (FSM=$FSM)"
fi

# =============================================================================
# SUMMARY
# =============================================================================
banner "SUMMARY"
printf '%s  Final incident record  (GET %s/api/v1/incidents?site=%s):%s\n' "$BOLD" "$ENGINE_API" "$SITE" "$RST"
jget "$ENGINE_API/api/v1/incidents?site=$SITE&limit=1" | "$PYTHON" -c '
import sys, json
inc = json.load(sys.stdin).get("incidents", [])
print(("\n".join("    " + l for l in json.dumps(inc[0], indent=2).splitlines())) if inc else "    (no incident found)")
' || warn "could not read incidents"

printf '\n%s  Failover event log  (GET %s/api/v1/failover/events?site=%s):%s\n' "$BOLD" "$CONTROLLER_API" "$SITE" "$RST"
jget "$CONTROLLER_API/api/v1/failover/events?site=$SITE&limit=10" | "$PYTHON" -c '
import sys, json
ev = list(reversed(json.load(sys.stdin).get("events", [])))
if not ev:
    print("    (no events)")
for e in ev:
    dur = "" if e.get("failover_duration_ms") is None else " [%.1f ms]" % e["failover_duration_ms"]
    print("    %s  %-8s %-22s%s" % (e["timestamp"], e["severity"], e["event_type"], dur))
    print("        " + e["message"])
' || warn "could not read events"

printf '\n%s  Results:%s\n' "$BOLD" "$RST"
printf '    Baseline RTT to %s (fiber)     : %s ms\n' "$LAN_IP" "$R_BASE_PING"
printf '    Proactive DEGRADING detected      : %s at mean RTT %s ms (CRITICAL threshold 200 ms)\n' "$R_DEGRADE_T" "$R_DEGRADE_RTT"
printf '    Fiber-cut failover                : %s ms route switch, %s s after the cut end-to-end\n' "$R_FAILOVER_MS" "$R_FAILOVER_E2E"
printf '    RTT over backup after failover    : %s ms\n' "$R_BACKUP_PING"
printf '    Failback to primary               : %s, RTT %s ms\n' "$R_FAILBACK_T" "$R_PRIMARY_PING"
printf '\n    Dashboard: %s    Prometheus: %s    Logs: %s\n' "$GRAFANA_URL" "$PROMETHEUS_URL" "$LOGDIR"

if [ "$WARNINGS" -eq 0 ]; then
  printf '\n%s%s  ✔ DEMO COMPLETED: all 4 phases ran cleanly%s\n\n' "$BOLD" "$GRN" "$RST"
  exit 0
fi
printf '\n%s%s  ⚠ DEMO COMPLETED WITH %d WARNING(S): see messages above%s\n\n' "$BOLD" "$YEL" "$WARNINGS" "$RST"
exit 1