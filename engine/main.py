"""noc-engine: async telemetry polling + FastAPI (anomaly detection, SLA, incidents)."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from anomaly import STATE_CODES, WARMING_UP, LinkDetector, Observation
from config import Config
from sla_tracker import IncidentManager, IncidentStore, SlaTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("netpulse.engine")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per poll is just noise


def _f(d: dict, k: str) -> float:
    try:
        return float(d.get(k, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


class Pipeline:
    """Synchronous per-tick processing; `process` runs in a worker thread."""

    def __init__(self, cfg: Config, store: Optional[IncidentStore] = None):
        self.cfg = cfg
        self.store = store or IncidentStore(cfg.db_path)
        orphans = self.store.close_orphans(time.time())
        if orphans:
            log.warning("closed %d incident(s) left OPEN by a previous run", orphans)
        self.sla = SlaTracker(cfg)
        self.incidents = IncidentManager(cfg, self.store)
        self.detectors: Dict[tuple, LinkDetector] = {}
        self.latest: Dict[str, Any] = {"timestamp": None, "updated_at": None, "links": []}
        self.last_success_ts: Optional[float] = None
        self.ticks = 0

    def process(self, payload: dict, now: float) -> None:
        links_in = payload.get("links") if isinstance(payload, dict) else None
        if not isinstance(links_in, list):
            raise ValueError("telemetry payload has no 'links' array")
        out = []
        for t in links_in:
            if not isinstance(t, dict) or "site" not in t or "link" not in t:
                continue
            site, link = str(t["site"]), str(t["link"])
            key = (site, link)
            det = self.detectors.get(key)
            if det is None:
                det = self.detectors[key] = LinkDetector(self.cfg, f"{site}/{link}")
            obs = Observation(
                rtt_mean_ms=_f(t, "rtt_mean_ms"), rtt_p95_ms=_f(t, "rtt_p95_ms"),
                jitter_ms=_f(t, "jitter_ms"), packet_loss_pct=_f(t, "packet_loss_pct"),
                link_up=int(_f(t, "link_up")), window_samples=int(_f(t, "window_samples")))
            d = det.update(obs)
            if obs.window_samples > 0:  # no SLA time until the prober has data
                self.sla.record(site, link, now, d.state != "CRITICAL")
            inc_id = self.incidents.update(site, link, d.state, obs, d.anomaly_score, d.z_rtt, d.z_jitter, now)
            sla = self.sla.snapshot(site, link, now)
            out.append({
                "site": site, "link": link,
                "health_state": d.state,
                "anomaly_score": round(d.anomaly_score, 4),
                "z_score_rtt": round(d.z_rtt, 3),
                "z_score_jitter": round(d.z_jitter, 3),
                "if_score": round(d.if_score, 4),
                "sla_compliance_pct": round(sla["sla_compliance_pct"], 4),
                "error_budget_remaining_pct": round(sla["error_budget_remaining_pct"], 3),
                "burn_rate": round(sla["burn_rate"], 3),
                "downtime_seconds": round(sla["downtime_seconds"], 2),
                "active_incident_id": inc_id,
                "warmup_progress": f"{min(d.warmup_collected, d.warmup_target)}/{d.warmup_target}",
                "telemetry": {
                    "target": t.get("target"), "link_up": obs.link_up,
                    "rtt_last_ms": _f(t, "rtt_last_ms"), "rtt_mean_ms": obs.rtt_mean_ms,
                    "rtt_p95_ms": obs.rtt_p95_ms, "jitter_ms": obs.jitter_ms,
                    "packet_loss_pct": obs.packet_loss_pct, "tcp_rtt_ms": _f(t, "tcp_rtt_ms"),
                    "tcp_ok": t.get("tcp_ok"),
                },
            })
        self.ticks += 1
        self.last_success_ts = now
        self.latest = {"timestamp": payload.get("timestamp"), "updated_at": now, "links": out}  # atomic swap


cfg = Config.from_env()
pipeline = Pipeline(cfg)
prober_connected = False


async def poll_loop() -> None:
    global prober_connected
    loop = asyncio.get_running_loop()
    next_t = loop.time()
    failures = 0
    async with httpx.AsyncClient(timeout=cfg.http_timeout_s) as client:
        while True:
            try:
                resp = await client.get(cfg.prober_url)
                resp.raise_for_status()
                payload = resp.json()
                await asyncio.to_thread(pipeline.process, payload, time.time())
                if not prober_connected:
                    log.info("prober connected: %s", cfg.prober_url)
                prober_connected, failures = True, 0
            except (httpx.HTTPError, ValueError) as exc:
                failures += 1
                prober_connected = False
                if failures == 1 or failures % 30 == 0:
                    log.warning("prober unreachable/invalid (%d consecutive): %s", failures, exc or type(exc).__name__)
            except Exception:  # never let one bad tick kill the loop
                log.exception("unexpected error while processing tick")
            next_t += cfg.poll_interval_s
            delay = next_t - loop.time()
            if delay < 0:
                next_t, delay = loop.time(), 0.0
            await asyncio.sleep(delay)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(poll_loop())
    log.info("noc-engine started: poll=%ss warmup=%d window=%ds target=%.3f%% port=%d",
             cfg.poll_interval_s, cfg.warmup_samples, cfg.sla_window_seconds, cfg.sla_target_pct, cfg.http_port)
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="NetPulse noc-engine", lifespan=lifespan)


def _connected_now() -> bool:
    ts = pipeline.last_success_ts
    return bool(prober_connected and ts is not None and time.time() - ts <= max(3 * cfg.poll_interval_s, 3.0))


@app.get("/health")
def health():
    return {"status": "ok", "prober_connected": _connected_now()}


@app.get("/api/v1/status")
def status():
    snap = pipeline.latest
    now = time.time()
    upd = snap["updated_at"]
    age = None if upd is None else round(now - upd, 2)
    return {
        "timestamp": int(now),
        "telemetry_timestamp": snap["timestamp"],
        "prober_connected": _connected_now(),
        "telemetry_age_s": age,
        "stale": age is None or age > cfg.stale_after_s,
        "warmup_samples": cfg.warmup_samples,
        "links": snap["links"],
    }


@app.get("/api/v1/incidents")
def incidents(status: Optional[str] = Query(None, pattern="^(?i:OPEN|RESOLVED)$"),
              site: Optional[str] = None, limit: int = Query(50, ge=1, le=1000)):
    rows = pipeline.store.list(status=status, site=site, limit=limit)
    return JSONResponse({"count": len(rows), "incidents": rows})


_METRICS = [
    ("netpulse_anomaly_score", "Unified anomaly score (0 normal .. 1 extreme).", lambda e: e["anomaly_score"]),
    ("netpulse_health_state", "Link state: 0=WARMING_UP 1=NORMAL 2=DEGRADING 3=CRITICAL.",
     lambda e: STATE_CODES.get(e["health_state"], 0)),
    ("netpulse_sla_compliance_pct", "Rolling SLA compliance (percent).", lambda e: e["sla_compliance_pct"]),
    ("netpulse_sla_error_budget_remaining_pct", "Remaining SLA error budget (percent).",
     lambda e: e["error_budget_remaining_pct"]),
    ("netpulse_sla_burn_rate", "Error budget burn rate over the last 5 minutes (>1 exhausts budget).",
     lambda e: e["burn_rate"]),
]


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    lines = []
    links = pipeline.latest["links"]
    for name, help_text, getter in _METRICS:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for e in links:
            lines.append(f'{name}{{site="{e["site"]}",link="{e["link"]}"}} {getter(e):g}')
    lines.append("# HELP netpulse_active_incidents_total Currently OPEN incidents.")
    lines.append("# TYPE netpulse_active_incidents_total gauge")
    lines.append(f"netpulse_active_incidents_total {pipeline.store.count_open()}")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=cfg.http_port, log_level="info")