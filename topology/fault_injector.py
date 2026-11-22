#!/usr/bin/env python3
""" Usage Examples:
  fault_injector.py status
  fault_injector.py --site douala --link primary --fault fiber-cut
  fault_injector.py --site douala --link primary --fault latency-drift --target-delay 300 --duration 90
  fault_injector.py --site edge --link backup --fault packet-loss-burst --loss 15
  fault_injector.py --site yaounde --link primary --fault flapping --duration 30
  fault_injector.py --site douala --link primary --fault reset
"""
import argparse
import re
import subprocess
import sys
import time
from datetime import datetime

SITES = {"douala": "site-douala", "yaounde": "site-yaounde", "edge": "site-edge"}
SUBNET_PREFIX = {"primary": "172.20.1.", "backup": "172.20.2."}
FAULTS = ["fiber-cut", "latency-drift", "packet-loss-burst", "flapping", "reset"]
STEP_SECONDS = 5


class InjectorError(Exception):
    pass


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def docker_exec(container, *cmd):
    r = run(["docker", "exec", container, *cmd])
    if r.returncode != 0:
        raise InjectorError(f"{container}: `{' '.join(cmd)}` failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def is_running(container):
    r = run(["docker", "inspect", "-f", "{{.State.Running}}", container])
    return r.returncode == 0 and r.stdout.strip() == "true"


def ensure_running(container):
    if not is_running(container):
        raise InjectorError(f"container '{container}' is not running (try: docker compose up -d --build)")


def resolve_iface(container, link):
    """Find the interface holding the subnet for this link (never assume eth0/eth1)."""
    out = docker_exec(container, "ip", "-o", "-4", "addr", "show")
    prefix = SUBNET_PREFIX[link]
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3].startswith(prefix):
            return parts[1]
    raise InjectorError(f"{container}: no interface with a {prefix}* address")


def read_baseline(container, link):
    """Baseline comes from the container's own env vars so values are never duplicated."""
    env = {}
    for line in docker_exec(container, "printenv").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    key = link.upper()
    try:
        return {
            "delay": float(env.get(f"{key}_DELAY_MS", 0)),
            "jitter": float(env.get(f"{key}_JITTER_MS", 0)),
            "loss": float(env.get(f"{key}_LOSS_PCT", 0)),
        }
    except ValueError as e:
        raise InjectorError(f"{container}: invalid baseline env value: {e}")


def netem(delay=0.0, jitter=0.0, loss=0.0, corr=None):
    args = ["netem"]
    if delay > 0:
        args += ["delay", f"{delay:g}ms"]
        if jitter > 0:
            args += [f"{jitter:g}ms"]
    args += ["loss", f"{loss:g}%"]
    if corr is not None:
        args += [f"{corr:g}%"]
    return args


def apply(container, iface, args, desc):
    docker_exec(container, "tc", "qdisc", "replace", "dev", iface, "root", *args)
    log(f"{container}/{iface}: {desc}  [tc ... {' '.join(args)}]")


def apply_baseline(container, iface, base, desc="baseline restored"):
    apply(container, iface, netem(base["delay"], base["jitter"], base["loss"]), desc)


# ---------------------------------------------------------------- faults
def fault_fiber_cut(c, iface, base, a):
    apply(c, iface, netem(loss=100), "FIBER CUT (100% loss)")


def fault_latency_drift(c, iface, base, a):
    steps = max(1, int(a.duration // STEP_SECONDS))
    log(f"{c}/{iface}: latency drift {base['delay']:g}ms -> {a.target_delay:g}ms "
        f"(jitter {base['jitter']:g} -> {a.jitter:g}ms) over {a.duration}s in {steps} steps")
    for i in range(1, steps + 1):
        f = i / steps
        d = base["delay"] + (a.target_delay - base["delay"]) * f
        j = base["jitter"] + (a.jitter - base["jitter"]) * f
        apply(c, iface, netem(d, j, base["loss"]), f"drift step {i}/{steps}: {d:.1f}ms +/- {j:.1f}ms")
        if i < steps:
            time.sleep(STEP_SECONDS)
    log(f"{c}/{iface}: drift complete, holding at target (use --fault reset to clear)")


def fault_packet_loss_burst(c, iface, base, a):
    apply(c, iface, netem(base["delay"], base["jitter"], a.loss, corr=25),
          f"PACKET LOSS BURST ({a.loss:g}% loss, 25% correlation)")


def fault_flapping(c, iface, base, a):
    log(f"{c}/{iface}: flapping every {STEP_SECONDS}s for {a.duration}s")
    end = time.monotonic() + a.duration
    down = True
    try:
        while time.monotonic() < end:
            if down:
                apply(c, iface, netem(loss=100), "FLAP: link DOWN (100% loss)")
            else:
                apply_baseline(c, iface, base, "FLAP: link UP (baseline)")
            down = not down
            time.sleep(min(STEP_SECONDS, max(0, end - time.monotonic())))
    finally:
        apply_baseline(c, iface, base, "flapping finished, baseline restored")


def fault_reset(c, iface, base, a):
    apply_baseline(c, iface, base, "RESET to baseline")


HANDLERS = {
    "fiber-cut": fault_fiber_cut,
    "latency-drift": fault_latency_drift,
    "packet-loss-burst": fault_packet_loss_burst,
    "flapping": fault_flapping,
    "reset": fault_reset,
}


# ---------------------------------------------------------------- status
def cmd_status():
    rc = 0
    for site, container in SITES.items():
        print(f"\n=== {site} ({container}) ===")
        if not is_running(container):
            print("  NOT RUNNING")
            rc = 1
            continue
        for link in ("primary", "backup"):
            try:
                iface = resolve_iface(container, link)
                link_out = docker_exec(container, "ip", "-o", "link", "show", "dev", iface)
                m = re.search(r"\bstate (\w+)", link_out)
                state = m.group(1) if m else ("UP" if "UP" in link_out.split("<")[1].split(">")[0] else "DOWN")
                qdisc = docker_exec(container, "tc", "qdisc", "show", "dev", iface)
                print(f"  [{link}] {iface}  link={state}")
                for line in qdisc.splitlines():
                    print(f"      {line}")
            except InjectorError as e:
                print(f"  [{link}] ERROR: {e}")
                rc = 1
    print()
    return rc


# ---------------------------------------------------------------- main
def build_parser():
    p = argparse.ArgumentParser(description="NetPulse fault injector", formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    p.add_argument("command", nargs="?", choices=["status"], help="'status' prints tc qdisc + link state for all sites")
    p.add_argument("--site", choices=list(SITES), help="target site")
    p.add_argument("--link", choices=["primary", "backup", "both"], help="target link")
    p.add_argument("--fault", choices=FAULTS, help="fault profile")
    p.add_argument("--duration", type=float, help="seconds (latency-drift default 60, flapping default 30)")
    p.add_argument("--target-delay", type=float, default=250.0, help="latency-drift target delay ms (default 250)")
    p.add_argument("--jitter", type=float, default=20.0, help="latency-drift target jitter ms (default 20)")
    p.add_argument("--loss", type=float, default=10.0, help="packet-loss-burst loss %% (default 10)")
    return p


def main():
    parser = build_parser()
    a = parser.parse_args()

    if a.command == "status":
        return cmd_status()

    if not (a.site and a.link and a.fault):
        parser.error("--site, --link and --fault are required (or use the 'status' command)")
    if a.duration is None:
        a.duration = 60.0 if a.fault == "latency-drift" else 30.0
    if a.duration <= 0 or a.target_delay < 0 or not 0 <= a.loss <= 100:
        parser.error("invalid numeric parameter")

    container = SITES[a.site]
    try:
        ensure_running(container)
        links = ["primary", "backup"] if a.link == "both" else [a.link]
        for link in links:
            iface = resolve_iface(container, link)
            base = read_baseline(container, link)
            log(f"{container}: {a.fault} on {link} link ({iface}), baseline "
                f"{base['delay']:g}ms +/- {base['jitter']:g}ms loss {base['loss']:g}%")
            HANDLERS[a.fault](container, iface, base, a)
    except InjectorError as e:
        log(f"ERROR: {e}")
        return 1
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())