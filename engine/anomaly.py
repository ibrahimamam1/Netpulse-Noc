"""Two-stage anomaly detection (EWMA/Z-score + Isolation Forest), one detector per link."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
from sklearn.ensemble import IsolationForest

from config import Config

log = logging.getLogger("netpulse.anomaly")

WARMING_UP = "WARMING_UP"
NORMAL = "NORMAL"
DEGRADING = "DEGRADING"
CRITICAL = "CRITICAL"
STATE_CODES = {WARMING_UP: 0, NORMAL: 1, DEGRADING: 2, CRITICAL: 3}

# Isolation Forest input: [rtt_mean_ms, rtt_p95_ms, jitter_ms, packet_loss_pct]
_NOISE_FLOOR = np.array([1.0, 1.5, 1.0, 0.0])
_NOISE_REL = 0.12
_AUG_REPEATS = 8


@dataclass
class Observation:
    rtt_mean_ms: float
    rtt_p95_ms: float
    jitter_ms: float
    packet_loss_pct: float
    link_up: int
    window_samples: int

    @property
    def lost_probes(self) -> int:
        return int(round(self.packet_loss_pct / 100.0 * self.window_samples))

    def vector(self) -> List[float]:
        return [self.rtt_mean_ms, self.rtt_p95_ms, self.jitter_ms, self.packet_loss_pct]


@dataclass
class Detection:
    state: str
    anomaly_score: float
    z_rtt: float
    z_jitter: float
    if_score: float
    raw_hit: bool
    warmup_collected: int
    warmup_target: int


class Ewma:
    """EWMA mean/variance (spec 5.1). z() uses the *previous* estimates."""

    def __init__(self, alpha: float, mean: float, var: float):
        self.alpha = alpha
        self.mean = mean
        self.var = max(var, 0.0)

    def z(self, x: float, floor: float) -> float:
        return (x - self.mean) / max(math.sqrt(self.var), floor)

    def update(self, x: float) -> None:
        a = self.alpha
        prev = self.mean
        self.var = (1.0 - a) * (self.var + a * (x - prev) ** 2)
        self.mean = a * x + (1.0 - a) * prev


def _clip01(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else v


class LinkDetector:
    def __init__(self, cfg: Config, name: str):
        self.cfg = cfg
        self.name = name
        self.active = False
        self.state = WARMING_UP
        self._buffer: List[List[float]] = []
        self._ew_rtt: Optional[Ewma] = None
        self._ew_jit: Optional[Ewma] = None
        self._model: Optional[IsolationForest] = None
        self._d_lo = 0.05
        self._hits = 0
        self._clear = 0

    # ---------------------------------------------------------------- helpers
    def _is_critical(self, o: Observation) -> bool:
        c = self.cfg
        return (o.link_up == 0 or o.packet_loss_pct >= c.critical_loss_pct
                or o.rtt_mean_ms >= c.critical_rtt_ms)

    def _floor(self, mean: float) -> float:
        return max(self.cfg.sigma_floor_ms, self.cfg.sigma_floor_rel * abs(mean))

    def _activate(self) -> None:
        B = np.asarray(self._buffer, dtype=float)
        a = self.cfg.ewma_alpha
        self._ew_rtt = Ewma(a, float(B[:, 0].mean()), float(B[:, 0].var(ddof=1)))
        self._ew_jit = Ewma(a, float(B[:, 2].mean()), float(B[:, 2].var(ddof=1)))

        # Baseline is only ~30 s of heavily overlapping window statistics, so it is
        # padded with noise-augmented copies (sd = max(floor, 12 % of mean)). This
        # makes sub-ms wobble on stable links "normal" for the forest. Loss gets no
        # noise: if baseline loss is all-zero the forest ignores that column.
        rng = np.random.default_rng(42)
        sd = np.maximum(_NOISE_FLOOR, _NOISE_REL * np.abs(B.mean(axis=0)))
        sd[3] = 0.0
        aug = np.repeat(B, _AUG_REPEATS, axis=0) + rng.normal(size=(len(B) * _AUG_REPEATS, 4)) * sd
        aug[:, :3] = np.clip(aug[:, :3], 0.0, None)
        X = np.vstack([B, aug])
        self._model = IsolationForest(n_estimators=50, contamination=0.02, random_state=42).fit(X)
        d_base = self._model.decision_function(B)
        self._d_lo = max(float(np.percentile(d_base, 25)), 0.02)
        self.active = True
        log.info("%s: baseline fitted (rtt=%.1fms jitter=%.1fms, %d samples)",
                 self.name, self._ew_rtt.mean, self._ew_jit.mean, len(B))

    def _if_score(self, vec: List[float]) -> float:
        """Map decision_function (>0 inlier, <0 outlier) to [0, 1].

        d >= 0 maps into [0, 0.3] (0.3 at the model boundary); d < 0 maps into
        [0.3, 1.0], reaching 0.65 at d=-0.075 and 1.0 at d<=-0.15.
        """
        d = float(self._model.decision_function(np.asarray([vec], dtype=float))[0])
        if d >= 0:
            return 0.3 * _clip01((self._d_lo - d) / self._d_lo)
        return 0.3 + 0.7 * _clip01(-d / 0.15)

    # ----------------------------------------------------------------- update
    def update(self, o: Observation) -> Detection:
        c = self.cfg
        target = c.warmup_samples
        critical = self._is_critical(o)

        if o.window_samples == 0:  # prober has not completed a probe yet
            return Detection(WARMING_UP, 0.0, 0.0, 0.0, 0.0, False, len(self._buffer), target)

        if not self.active:
            if critical:
                self.state = CRITICAL
                return Detection(CRITICAL, 1.0, 0.0, 0.0, 0.0, True, len(self._buffer), target)
            if o.link_up == 1 and o.packet_loss_pct < c.warmup_max_loss_pct:
                self._buffer.append(o.vector())
                if len(self._buffer) >= target:
                    self._activate()
                    self.state = NORMAL
                    return Detection(NORMAL, 0.0, 0.0, 0.0, 0.0, False, len(self._buffer), target)
            self.state = WARMING_UP
            return Detection(WARMING_UP, 0.0, 0.0, 0.0, 0.0, False, len(self._buffer), target)

        z_rtt = self._ew_rtt.z(o.rtt_mean_ms, self._floor(self._ew_rtt.mean))
        z_jit = self._ew_jit.z(o.jitter_ms, self._floor(self._ew_jit.mean))
        if_score = self._if_score(o.vector())
        loss_hit = o.packet_loss_pct >= c.degrade_loss_pct and o.lost_probes >= c.loss_min_lost_probes
        raw_hit = max(z_rtt, z_jit) > c.zscore_threshold or if_score >= c.if_threshold or loss_hit

        prev = self.state
        if critical:
            state = CRITICAL
            self._hits = max(self._hits + 1, c.consecutive_hits_k)
            self._clear = 0
        elif raw_hit:
            self._hits += 1
            self._clear = 0
            state = DEGRADING if (self._hits >= c.consecutive_hits_k or prev in (DEGRADING, CRITICAL)) else NORMAL
        else:
            self._hits = 0
            if prev in (DEGRADING, CRITICAL):
                self._clear += 1
                state = NORMAL if self._clear >= c.recovery_polls else DEGRADING
            else:
                state = NORMAL
        self.state = state

        # Baseline freeze: learn only from ticks that are clean and NORMAL.
        if state == NORMAL and not raw_hit:
            self._ew_rtt.update(o.rtt_mean_ms)
            self._ew_jit.update(o.jitter_ms)

        z_comp = _clip01((max(z_rtt, z_jit) - 1.0) / (2.0 * c.zscore_threshold - 1.0))
        loss_comp = _clip01(o.packet_loss_pct / c.critical_loss_pct)
        score = 1.0 if state == CRITICAL else max(if_score, z_comp, loss_comp)
        return Detection(state, score, z_rtt, z_jit, if_score, raw_hit, len(self._buffer), target)