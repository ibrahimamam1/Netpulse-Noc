"""noc-controller: async control loop (Layer 3 status -> FSM -> ip route) + FastAPI."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

from alerter import AlertDispatcher
from config import Config
from fsm import STATE_CODES, Alert, LinkView, SiteFsm
from router_driver import RouterDriver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("netpulse.controller")

cfg = Config.from_env()
driver = RouterDriver(cfg)
fsms: Dict[str, SiteFsm] = {s.name: SiteFsm(s, cfg, driver) for s in cfg.sites}
alerter = AlertDispatcher(cfg)

engine_ok_ts: Optional[float] = None   # last successful poll of the engine
engine_fresh = False                    # engine reachable AND its telemetry not stale


def _view(entry: dict) -> LinkView:
    tel = entry.get("telemetry") or {}

    def num(v):
        try:
            return float(v or 0.0)
        except (TypeError, ValueError):
            return 0.0
    lu = tel.get("link_up")
    return LinkView(state=str(entry.get("health_state", "UNKNOWN")), rtt_ms=num(tel.get("rtt_mean_ms")),
                    loss_pct=num(tel.get("packet_loss_pct")), anomaly=num(entry.get("anomaly_score")),
                    link_up=None if lu is None else int(lu))


def _group(payload: dict) -> Dict[str, Dict[str, LinkView]]:
    out: Dict[str, Dict[str, LinkView]] = {}
    for e in payload.get("links", []):
        if isinstance(e, dict) and "site" in e and e.get("link") in ("primary", "backup"):
            out.setdefault(str(e["site"]), {})[e["link"]] = _view(e)
    return out


def step(views: Dict[str, Dict[str, LinkView]]) -> List[Alert]:
    """One control tick for all sites (runs in a worker thread: route changes block on docker exec)."""
    alerts: List[Alert] = []
    for name, fsm in fsms.items():
        v = views.get(name, {})
        if "primary" in v and "backup" in v:
            alerts += fsm.evaluate(v["primary"], v["backup"])
    return alerts


def reconcile_all() -> List[Alert]:
    if not driver.ping():
        return []
    if not driver.routes_ready:
        return []
    return [a for a in (f.reconcile() for f in fsms.values()) if a]


async def control_loop() -> None:
    global engine_ok_ts, engine_fresh
    loop = asyncio.get_running_loop()
    next_t = loop.time()
    failures, last_init_try = 0, 0.0
    stale_logged = False
    async with httpx.AsyncClient(timeout=cfg.engine_timeout_s) as client:
        while True:
            try:
                resp = await client.get(cfg.engine_url)
                resp.raise_for_status()
                payload = resp.json()
                if failures:
                    log.info("engine reachable again")
                failures, engine_ok_ts = 0, time.time()
                engine_fresh = not payload.get("stale", False)
                if not engine_fresh and not stale_logged:
                    log.warning("engine telemetry is stale (prober unreachable?): holding current routes")
                elif engine_fresh and stale_logged:
                    log.info("engine telemetry is fresh again")
                stale_logged = not engine_fresh

                if not driver.routes_ready and loop.time() - last_init_try >= 2.0:
                    last_init_try = loop.time()
                    await asyncio.to_thread(driver.init_routes)
                if driver.routes_ready and engine_fresh:
                    for a in await asyncio.to_thread(step, _group(payload)):
                        await alerter.dispatch(a)
            except (httpx.HTTPError, ValueError) as exc:
                failures += 1
                engine_fresh = False
                if failures == 1 or failures % 30 == 0:
                    log.warning("engine unreachable (%d consecutive): holding current routes (%s)",
                                failures, exc or type(exc).__name__)
            except Exception:
                log.exception("unexpected error in control tick")
            next_t += cfg.poll_interval_s
            delay = next_t - loop.time()
            if delay < 0:
                next_t, delay = loop.time(), 0.0
            await asyncio.sleep(delay)


async def reconcile_loop() -> None:
    while True:
        await asyncio.sleep(cfg.reconcile_interval_s)
        try:
            for a in await asyncio.to_thread(reconcile_all):
                await alerter.dispatch(a)
        except Exception:
            log.exception("reconcile failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await alerter.start()
    tasks = [asyncio.create_task(control_loop()), asyncio.create_task(reconcile_loop())]
    log.info("noc-controller started: engine=%s poll=%ss hold-down=%ss flap=%d/%ss dry_run=%s",
             cfg.engine_url, cfg.poll_interval_s, cfg.hold_down_seconds, cfg.flap_max_transitions,
             cfg.flap_window_seconds, cfg.dry_run)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await alerter.stop()


app = FastAPI(title="NetPulse noc-controller", lifespan=lifespan)


def _engine_connected() -> bool:
    return engine_ok_ts is not None and time.time() - engine_ok_ts <= max(3 * cfg.poll_interval_s, 3.0)


@app.get("/health")
def health():
    return {"status": "ok", "engine_connected": _engine_connected(), "docker_connected": bool(driver.connected),
            "telemetry_fresh": bool(engine_fresh and _engine_connected()), "dry_run": cfg.dry_run}


@app.get("/api/v1/failover/status")
async def failover_status():
    routes = await asyncio.gather(*[asyncio.to_thread(driver.get_active_route, n) for n in fsms])
    sites = []
    for (name, fsm), route in zip(fsms.items(), routes):
        snap = fsm.snapshot()
        snap["verified_kernel_route"] = route and {**route, "matches_fsm": route["link"] == snap["active_link"]}
        sites.append(snap)
    return {"timestamp": int(time.time()), "engine_connected": _engine_connected(),
            "docker_connected": bool(driver.connected), "sites": sites}


@app.get("/api/v1/failover/events")
def failover_events(site: Optional[str] = None, limit: int = Query(50, ge=1, le=100)):
    ev = alerter.recent(site=site, limit=limit)
    return {"count": len(ev), "events": ev}


@app.post("/api/v1/failover/{site}/ack")
async def failover_ack(site: str):
    fsm = fsms.get(site)
    if fsm is None:
        raise HTTPException(404, f"unknown site '{site}'")
    try:
        alerts = fsm.ack()
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    for a in alerts:
        await alerter.dispatch(a)
    return {"site": site, "fsm_state": fsm.state, "flap_count_in_window": len(fsm.history)}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    snaps = [f.snapshot() for f in fsms.values()]
    rows = [
        ("netpulse_failover_active_link", "gauge", "Active link per site: 1=primary, 0=backup.",
         lambda s: 1 if s["active_link"] == "primary" else 0),
        ("netpulse_failover_fsm_state", "gauge",
         "FSM state: 0=HEALTHY_PRIMARY 1=DEGRADED_WARNING 2=FAILED_OVER_BACKUP 3=RECOVERY_HOLD 4=FLAP_LOCKED.",
         lambda s: s["fsm_state_code"]),
        ("netpulse_failover_transitions_total", "counter", "Failovers executed since startup.",
         lambda s: s["failover_count"]),
        ("netpulse_failover_last_duration_ms", "gauge", "Execution time of the most recent failover (ms).",
         lambda s: s["last_failover_duration_ms"] or 0),
    ]
    out = []
    for name, typ, help_text, get in rows:
        out += [f"# HELP {name} {help_text}", f"# TYPE {name} {typ}"]
        out += [f'{name}{{site="{s["site"]}"}} {get(s):g}' for s in snaps]
    return PlainTextResponse("\n".join(out) + "\n", media_type="text/plain; version=0.0.4")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=cfg.http_port, log_level="info")