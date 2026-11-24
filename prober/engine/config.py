"""Environment-driven configuration for noc-engine ."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _num(name: str, default: float, cast=float):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return cast(raw)
    except ValueError as exc:
        raise ValueError(f"invalid value for {name}: {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    # --- spec parameters ---------------------------------------------------
    prober_url: str = "http://172.20.1.10:9100/api/v1/telemetry"
    poll_interval_s: float = 1.0
    warmup_samples: int = 30
    ewma_alpha: float = 0.2
    zscore_threshold: float = 3.0
    consecutive_hits_k: int = 3
    sla_target_pct: float = 99.9
    sla_window_seconds: int = 3600
    http_port: int = 9200

    # --- classification thresholds (spec section 5.3) ----------------------
    critical_loss_pct: float = 20.0
    critical_rtt_ms: float = 200.0
    degrade_loss_pct: float = 2.0
    if_threshold: float = 0.65
    warmup_max_loss_pct: float = 5.0

    # --- tuning knobs beyond the spec (see comments) -----------------------
    # Z-score sigma floor: max(sigma_floor_ms, sigma_floor_rel * ewma_mean).
    # The spec's 1 ms floor is kept; the relative part stops high-jitter backup
    # links (45-65 ms +/- 8-12 ms) from tripping Z>3 on ordinary window noise.
    sigma_floor_ms: float = 1.0
    sigma_floor_rel: float = 0.25
    # A loss-based DEGRADING hit also needs this many lost probes in the prober
    # window, so one stray loss on a 0.1-0.2 % backup link (= 5 % of a 20-sample
    # window) is not an incident.
    loss_min_lost_probes: int = 2
    # Consecutive clean polls required to leave DEGRADING (flap damping).
    recovery_polls: int = 3
    burn_rate_window_s: int = 300
    stale_after_s: float = 5.0

    db_path: str = "/app/data/incidents.db"
    http_timeout_s: float = 1.0

    @classmethod
    def from_env(cls) -> "Config":
        d = cls()
        cfg = cls(
            prober_url=os.environ.get("PROBER_URL", d.prober_url),
            poll_interval_s=_num("POLL_INTERVAL_S", d.poll_interval_s),
            warmup_samples=_num("WARMUP_SAMPLES", d.warmup_samples, int),
            ewma_alpha=_num("EWMA_ALPHA", d.ewma_alpha),
            zscore_threshold=_num("ZSCORE_THRESHOLD", d.zscore_threshold),
            consecutive_hits_k=_num("CONSECUTIVE_HITS_K", d.consecutive_hits_k, int),
            sla_target_pct=_num("SLA_TARGET_PCT", d.sla_target_pct),
            sla_window_seconds=_num("SLA_WINDOW_SECONDS", d.sla_window_seconds, int),
            http_port=_num("HTTP_PORT", d.http_port, int),
            critical_loss_pct=_num("CRITICAL_LOSS_PCT", d.critical_loss_pct),
            critical_rtt_ms=_num("CRITICAL_RTT_MS", d.critical_rtt_ms),
            degrade_loss_pct=_num("DEGRADE_LOSS_PCT", d.degrade_loss_pct),
            if_threshold=_num("IF_THRESHOLD", d.if_threshold),
            sigma_floor_ms=_num("SIGMA_FLOOR_MS", d.sigma_floor_ms),
            sigma_floor_rel=_num("SIGMA_FLOOR_REL", d.sigma_floor_rel),
            loss_min_lost_probes=_num("LOSS_MIN_LOST_PROBES", d.loss_min_lost_probes, int),
            recovery_polls=_num("RECOVERY_POLLS", d.recovery_polls, int),
            burn_rate_window_s=_num("BURN_RATE_WINDOW_S", d.burn_rate_window_s, int),
            stale_after_s=_num("STALE_AFTER_S", d.stale_after_s),
            db_path=os.environ.get("DB_PATH", d.db_path),
            http_timeout_s=_num("HTTP_TIMEOUT_S", d.http_timeout_s),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.poll_interval_s <= 0:
            raise ValueError("POLL_INTERVAL_S must be > 0")
        if self.warmup_samples < 5:
            raise ValueError("WARMUP_SAMPLES must be >= 5")
        if not 0 < self.ewma_alpha <= 1:
            raise ValueError("EWMA_ALPHA must be in (0, 1]")
        if self.consecutive_hits_k < 1 or self.recovery_polls < 1:
            raise ValueError("CONSECUTIVE_HITS_K and RECOVERY_POLLS must be >= 1")
        if not 0 < self.sla_target_pct <= 100:
            raise ValueError("SLA_TARGET_PCT must be in (0, 100]")
        if not 1 <= self.sla_window_seconds <= 2_592_000:
            raise ValueError("SLA_WINDOW_SECONDS must be in [1, 2592000]")
        if not 1 <= self.http_port <= 65535:
            raise ValueError("HTTP_PORT out of range")