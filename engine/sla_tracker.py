"""SLA compliance / error budget / burn rate math and SQLite incident persistence."""
from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple

from anomaly import CRITICAL, DEGRADING, Observation
from config import Config

log = logging.getLogger("netpulse.sla")


class SlaWindow:
    """Rolling healthy/total time over SLA_WINDOW_SECONDS.

    Ticks are aggregated into time buckets (<= ~3600 buckets whatever the window,
    so a 30-day window stays small). Each tick carries the seconds it represents,
    so skipped polls (prober unreachable) are not counted as up or down time.
    """

    def __init__(self, window_s: float):
        self.window_s = float(window_s)
        self.bucket_s = max(1.0, self.window_s / 3600.0)
        self._buckets: Deque[List[float]] = deque()  # [bucket_idx, healthy_s, total_s]

    def add(self, ts: float, healthy: bool, dt: float) -> None:
        idx = int(ts // self.bucket_s)
        if self._buckets and self._buckets[-1][0] == idx:
            b = self._buckets[-1]
        else:
            b = [idx, 0.0, 0.0]
            self._buckets.append(b)
        b[2] += dt
        if healthy:
            b[1] += dt
        cutoff = ts - self.window_s
        while self._buckets and (self._buckets[0][0] + 1) * self.bucket_s <= cutoff:
            self._buckets.popleft()

    def totals(self, now: float, span: Optional[float] = None) -> Tuple[float, float]:
        """(healthy_seconds, total_seconds) over the window, or the last `span` seconds."""
        lo = now - (self.window_s if span is None else span)
        healthy = total = 0.0
        for idx, h, t in reversed(self._buckets):
            if (idx + 1) * self.bucket_s <= lo:
                break
            healthy += h
            total += t
        return healthy, total


class SlaTracker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._windows: Dict[Tuple[str, str], SlaWindow] = {}
        self._last_ts: Dict[Tuple[str, str], float] = {}

    def record(self, site: str, link: str, ts: float, healthy: bool) -> None:
        key = (site, link)
        win = self._windows.get(key)
        if win is None:
            win = self._windows[key] = SlaWindow(self.cfg.sla_window_seconds)
        last = self._last_ts.get(key)
        cap = 2.0 * self.cfg.poll_interval_s
        dt = self.cfg.poll_interval_s if last is None else min(max(ts - last, 0.0), cap)
        self._last_ts[key] = ts
        win.add(ts, healthy, dt)

    def snapshot(self, site: str, link: str, now: float) -> Dict[str, float]:
        cfg = self.cfg
        win = self._windows.get((site, link))
        allowed_frac = 1.0 - cfg.sla_target_pct / 100.0
        budget_total = cfg.sla_window_seconds * allowed_frac
        if win is None:
            return {"sla_compliance_pct": 100.0, "error_budget_remaining_pct": 100.0, "burn_rate": 0.0,
                    "downtime_seconds": 0.0, "error_budget_total_seconds": budget_total}
        healthy, total = win.totals(now)
        downtime = max(total - healthy, 0.0)
        sla = 100.0 if total <= 0 else healthy / total * 100.0
        if budget_total <= 0:
            remaining = 100.0 if downtime <= 0 else 0.0
        else:
            remaining = max(0.0, (budget_total - downtime) / budget_total * 100.0)

        span = max(float(cfg.burn_rate_window_s), 2.0 * win.bucket_s)
        r_healthy, r_total = win.totals(now, span)
        rate = 0.0 if r_total <= 0 else max(r_total - r_healthy, 0.0) / r_total
        if allowed_frac <= 0:
            burn = 0.0 if rate == 0 else 1e6
        else:
            burn = rate / allowed_frac
        return {"sla_compliance_pct": sla, "error_budget_remaining_pct": remaining, "burn_rate": burn,
                "downtime_seconds": downtime, "error_budget_total_seconds": budget_total}


# ============================================================ incident store
def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class IncidentStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site TEXT NOT NULL,
        link TEXT NOT NULL,
        severity TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        resolved_at TEXT,
        duration_seconds REAL,
        peak_rtt_ms REAL,
        peak_loss_pct REAL,
        peak_anomaly_score REAL,
        root_cause TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_incidents_started ON incidents(started_at DESC);
    CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
    """

    def __init__(self, path: str):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._db.executescript(self.SCHEMA)

    def close_orphans(self, now: float) -> int:
        """Incidents left OPEN by a previous run cannot be tracked any more: close them."""
        with self._lock:
            rows = self._db.execute("SELECT id, started_at FROM incidents WHERE status='OPEN'").fetchall()
            for r in rows:
                started = datetime.strptime(r["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                self._db.execute(
                    "UPDATE incidents SET status='RESOLVED', resolved_at=?, duration_seconds=? WHERE id=?",
                    (_iso(now), max(now - started.timestamp(), 0.0), r["id"]))
            return len(rows)

    def create(self, site, link, severity, started_ts, peak_rtt, peak_loss, peak_score, root_cause) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO incidents (site, link, severity, status, started_at, peak_rtt_ms, peak_loss_pct,"
                " peak_anomaly_score, root_cause) VALUES (?,?,?,?,?,?,?,?,?)",
                (site, link, severity, "OPEN", _iso(started_ts), peak_rtt, peak_loss, peak_score, root_cause))
            return int(cur.lastrowid)

    def update(self, inc_id, severity, peak_rtt, peak_loss, peak_score, root_cause) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE incidents SET severity=?, peak_rtt_ms=?, peak_loss_pct=?, peak_anomaly_score=?,"
                " root_cause=? WHERE id=?", (severity, peak_rtt, peak_loss, peak_score, root_cause, inc_id))

    def resolve(self, inc_id, resolved_ts, duration) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE incidents SET status='RESOLVED', resolved_at=?, duration_seconds=? WHERE id=?",
                (_iso(resolved_ts), duration, inc_id))

    def count_open(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM incidents WHERE status='OPEN'").fetchone()[0])

    def list(self, status: Optional[str] = None, site: Optional[str] = None, limit: int = 50) -> List[dict]:
        sql, args = "SELECT * FROM incidents WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status.upper())
        if site:
            sql += " AND site=?"
            args.append(site)
        sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]


# ============================================================ incident lifecycle
# A link_up==0 run this long is a real outage; bursty loss only causes 1-2 s blips
# (3 consecutive losses at 500 ms, cleared by the next success).
_SUSTAINED_DOWN_S = 2.5


@dataclass
class _Open:
    id: int
    severity: str
    started_ts: float
    peak_rtt: float
    peak_loss: float
    peak_score: float
    peak_z_rtt: float
    peak_z_jitter: float
    down_since: Optional[float]   # start of the current link_up==0 run
    long_down_episodes: int       # runs of link_up==0 lasting >= _SUSTAINED_DOWN_S
    run_counted: bool
    root_cause: str


class IncidentManager:
    """One open incident per link: opened on NORMAL->DEGRADING/CRITICAL, escalated in
    place, peaks updated while open, resolved when the link is back to NORMAL."""

    def __init__(self, cfg: Config, store: IncidentStore):
        self.cfg = cfg
        self.store = store
        self._open: Dict[Tuple[str, str], _Open] = {}

    def infer_root_cause(self, o: _Open) -> str:
        z = self.cfg.zscore_threshold
        if o.long_down_episodes >= 2 and o.peak_loss < 90.0:
            return "LINK_FLAPPING"
        if o.long_down_episodes >= 1 or o.peak_loss >= 90.0:
            return "FIBER_CUT_OR_LINK_DOWN"
        if o.peak_z_rtt > z and o.peak_loss < self.cfg.critical_loss_pct:
            return "LATENCY_DRIFT"
        if o.peak_loss >= self.cfg.degrade_loss_pct:
            return "PACKET_LOSS_BURST"
        if o.peak_z_jitter > z:
            return "HIGH_JITTER"
        return "MULTIVARIATE_ANOMALY"

    def active_id(self, site: str, link: str) -> Optional[int]:
        o = self._open.get((site, link))
        return o.id if o else None

    def update(self, site: str, link: str, state: str, obs: Observation, score: float,
               z_rtt: float, z_jitter: float, now: float) -> Optional[int]:
        key = (site, link)
        cur = self._open.get(key)
        faulty = state in (DEGRADING, CRITICAL)

        if not faulty:
            if cur is not None:
                self.store.resolve(cur.id, now, max(now - cur.started_ts, 0.0))
                log.info("incident %d RESOLVED %s/%s after %.1fs (%s)", cur.id, site, link,
                         now - cur.started_ts, cur.root_cause)
                del self._open[key]
            return None

        severity = "CRITICAL" if state == CRITICAL else "WARNING"
        if cur is None:
            cur = _Open(0, severity, now, obs.rtt_mean_ms, obs.packet_loss_pct, score, z_rtt, z_jitter,
                        now if obs.link_up == 0 else None, 0, False, "")
            cur.root_cause = self.infer_root_cause(cur)
            cur.id = self.store.create(site, link, severity, now, cur.peak_rtt, cur.peak_loss,
                                       cur.peak_score, cur.root_cause)
            self._open[key] = cur
            log.warning("incident %d OPEN %s/%s severity=%s cause=%s", cur.id, site, link, severity, cur.root_cause)
            return cur.id

        changed = False
        if severity == "CRITICAL" and cur.severity != "CRITICAL":
            cur.severity = "CRITICAL"
            changed = True
            log.warning("incident %d ESCALATED %s/%s -> CRITICAL", cur.id, site, link)
        if obs.link_up == 0:
            if cur.down_since is None:
                cur.down_since = now
            if not cur.run_counted and now - cur.down_since >= _SUSTAINED_DOWN_S:
                cur.long_down_episodes += 1
                cur.run_counted = True
        else:
            cur.down_since, cur.run_counted = None, False
        for attr, val in (("peak_rtt", obs.rtt_mean_ms), ("peak_loss", obs.packet_loss_pct),
                          ("peak_score", score), ("peak_z_rtt", z_rtt), ("peak_z_jitter", z_jitter)):
            if val > getattr(cur, attr):
                setattr(cur, attr, val)
                changed = changed or attr in ("peak_rtt", "peak_loss", "peak_score")
        cause = self.infer_root_cause(cur)
        if cause != cur.root_cause:
            cur.root_cause = cause
            changed = True
        if changed:
            self.store.update(cur.id, cur.severity, cur.peak_rtt, cur.peak_loss, cur.peak_score, cur.root_cause)
        return cur.id