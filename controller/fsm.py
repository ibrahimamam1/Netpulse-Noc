"""Per-site failover finite state machine with hold-down timer and flap dampening."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

from config import Config, SiteRoute

log = logging.getLogger("netpulse.fsm")

HEALTHY_PRIMARY = "HEALTHY_PRIMARY"
DEGRADED_WARNING = "DEGRADED_WARNING"
FAILED_OVER_BACKUP = "FAILED_OVER_BACKUP"
RECOVERY_HOLD = "RECOVERY_HOLD"
FLAP_LOCKED = "FLAP_LOCKED"
STATE_CODES = {HEALTHY_PRIMARY: 0, DEGRADED_WARNING: 1, FAILED_OVER_BACKUP: 2, RECOVERY_HOLD: 3, FLAP_LOCKED: 4}

# Layer 3 health states
NORMAL, DEGRADING, CRITICAL = "NORMAL", "DEGRADING", "CRITICAL"


@dataclass
class LinkView:
    """What the FSM needs to know about one link, taken from the Layer 3 status."""
    state: str = "UNKNOWN"
    rtt_ms: float = 0.0
    loss_pct: float = 0.0
    anomaly: float = 0.0
    link_up: Optional[int] = None


@dataclass
class Alert:
    site: str
    event_type: str
    severity: str  # INFO | WARNING | CRITICAL | RESOLVED
    message: str
    active_link: str
    fsm_state: str
    failover_duration_ms: Optional[float] = None
    metrics_snapshot: dict = field(default_factory=dict)


class SiteFsm:
    def __init__(self, route: SiteRoute, cfg: Config, driver, clock=time.monotonic):
        self.route = route
        self.site = route.name
        self.cfg = cfg
        self.driver = driver
        self.clock = clock
        self.lock = threading.RLock()

        self.state = HEALTHY_PRIMARY
        self.history: Deque[float] = deque()  # flap history (monotonic timestamps)
        self.hold_started: Optional[float] = None
        self.failover_count = 0
        self.last_failover_ms: Optional[float] = None
        self.dual_active = False
        self.router_error_active = False
        self.prev_link_up: Optional[int] = None
        self.last_primary = LinkView()
        self.last_backup = LinkView()
        self.last_error = ""

    # ------------------------------------------------------------ properties
    @property
    def active_link(self) -> str:
        return "primary" if self.state in (HEALTHY_PRIMARY, DEGRADED_WARNING) else "backup"

    def _prune(self, now: float) -> None:
        cutoff = now - self.cfg.flap_window_seconds
        while self.history and self.history[0] <= cutoff:
            self.history.popleft()

    def snapshot(self, now: Optional[float] = None) -> dict:
        with self.lock:
            now = self.clock() if now is None else now
            self._prune(now)
            remaining = 0.0
            if self.state == RECOVERY_HOLD and self.hold_started is not None:
                remaining = max(0.0, self.cfg.hold_down_seconds - (now - self.hold_started))
            return {
                "site": self.site,
                "fsm_state": self.state,
                "fsm_state_code": STATE_CODES[self.state],
                "active_link": self.active_link,
                "lan_ip": self.route.lan_ip,
                "hold_down_remaining_s": round(remaining, 2),
                "flap_count_in_window": len(self.history),
                "failover_count": self.failover_count,
                "last_failover_duration_ms": None if self.last_failover_ms is None else round(self.last_failover_ms, 2),
                "primary_health": self.last_primary.state,
                "backup_health": self.last_backup.state,
                "router_error": self.last_error or None,
            }

    # --------------------------------------------------------------- helpers
    def _metrics(self) -> dict:
        p, b = self.last_primary, self.last_backup
        return {"primary_rtt_ms": round(p.rtt_ms, 2), "primary_loss_pct": round(p.loss_pct, 2),
                "primary_state": p.state, "backup_rtt_ms": round(b.rtt_ms, 2),
                "backup_loss_pct": round(b.loss_pct, 2), "backup_state": b.state,
                "anomaly_score": round(p.anomaly, 3)}

    def _alert(self, event, severity, message, duration=None) -> Alert:
        return Alert(self.site, event, severity, message, self.active_link, self.state,
                     None if duration is None else round(duration, 2), self._metrics())

    def _record_transition(self, now: float) -> bool:
        """Append to flap history; True if the lock threshold is now reached."""
        self.history.append(now)
        self._prune(now)
        return len(self.history) >= self.cfg.flap_max_transitions

    def _lock_alert(self) -> Alert:
        self.state = FLAP_LOCKED
        self.hold_started = None
        n = len(self.history)
        return self._alert("LINK_FLAPPING", "CRITICAL",
                           f"Site {self.site} primary link is flapping ({n} transitions within "
                           f"{self.cfg.flap_window_seconds:g}s); traffic pinned to backup until an operator acknowledges.")

    def _router_failed(self, res, action: str) -> List[Alert]:
        self.last_error = res.error
        log.error("%s: %s failed: %s", self.site, action, res.error)
        if self.router_error_active:
            return []  # already alerted; keep retrying every poll silently
        self.router_error_active = True
        return [self._alert("ROUTER_ERROR", "CRITICAL",
                            f"Site {self.site}: route update ({action}) failed, will retry: {res.error}")]

    # ------------------------------------------------------------- evaluate
    def evaluate(self, primary: LinkView, backup: LinkView, now: Optional[float] = None) -> List[Alert]:
        t0 = time.perf_counter()  # trigger detection time, for failover_duration_ms
        with self.lock:
            now = self.clock() if now is None else now
            alerts: List[Alert] = []
            start_state = self.state
            edge = self.prev_link_up == 1 and primary.link_up == 0
            if primary.link_up is not None:
                self.prev_link_up = primary.link_up
            self.last_primary, self.last_backup = primary, backup
            self._prune(now)
            p, b = primary.state, backup.state

            # Dual-outage safety: never thrash routes, alert once per episode.
            if p == CRITICAL and b == CRITICAL:
                if not self.dual_active:
                    self.dual_active = True
                    alerts.append(self._alert("DUAL_LINK_OUTAGE", "CRITICAL",
                                              f"Site {self.site}: BOTH primary and backup links are CRITICAL; "
                                              f"keeping current route ({self.active_link})."))
            else:
                self.dual_active = False

            s = self.state
            if s == FLAP_LOCKED:
                return alerts

            if s in (HEALTHY_PRIMARY, DEGRADED_WARNING):
                if p == CRITICAL:
                    if b != CRITICAL:
                        alerts += self._failover(now, t0)
                elif s == HEALTHY_PRIMARY and p == DEGRADING:
                    self.state = DEGRADED_WARNING
                    alerts.append(self._alert("LINK_DEGRADING", "WARNING",
                                              f"Site {self.site} primary link is DEGRADING; failover armed, "
                                              f"traffic remains on primary."))
                elif s == DEGRADED_WARNING and p == NORMAL:
                    self.state = HEALTHY_PRIMARY
                    alerts.append(self._alert("DEGRADATION_RESOLVED", "RESOLVED",
                                              f"Site {self.site} primary link returned to NORMAL."))

            elif s == FAILED_OVER_BACKUP:
                if b == CRITICAL and p in (NORMAL, DEGRADING):
                    alerts += self._failback(now, t0, emergency=True)
                elif p == NORMAL:
                    self.state = RECOVERY_HOLD
                    self.hold_started = now
                elif start_state == FAILED_OVER_BACKUP and edge:
                    # primary went down again while we are already on backup: flap evidence
                    if self._record_transition(now):
                        alerts.append(self._lock_alert())

            elif s == RECOVERY_HOLD:
                if p in (DEGRADING, CRITICAL):
                    self.state = FAILED_OVER_BACKUP
                    self.hold_started = None
                    if self._record_transition(now):
                        alerts.append(self._lock_alert())
                elif p == NORMAL:
                    if b == CRITICAL:
                        alerts += self._failback(now, t0, emergency=True)
                    elif self.hold_started is not None and now - self.hold_started >= self.cfg.hold_down_seconds:
                        alerts += self._failback(now, t0, emergency=False)
                else:  # WARMING_UP / UNKNOWN: not continuously NORMAL, restart the timer
                    self.hold_started = now
            return alerts

    # --------------------------------------------------------------- actions
    def _failover(self, now: float, t0: float) -> List[Alert]:
        res = self.driver.switch_to_backup(self.site)
        if not res.ok:
            return self._router_failed(res, "switch to backup")
        self.router_error_active, self.last_error = False, ""
        duration = (time.perf_counter() - t0) * 1000.0
        self.failover_count += 1
        self.last_failover_ms = duration
        locked = self._record_transition(now)
        self.state = FAILED_OVER_BACKUP
        alerts = [self._alert("FAILOVER_TO_BACKUP", "CRITICAL",
                              f"Site {self.site} failed over to backup link in {duration:.1f}ms "
                              f"due to primary link CRITICAL state.", duration)]
        if locked:
            alerts.append(self._lock_alert())
        return alerts

    def _failback(self, now: float, t0: float, emergency: bool) -> List[Alert]:
        res = self.driver.switch_to_primary(self.site)
        if not res.ok:
            return self._router_failed(res, "switch to primary")
        self.router_error_active, self.last_error = False, ""
        duration = (time.perf_counter() - t0) * 1000.0
        self.hold_started = None
        self._record_transition(now)
        self.state = HEALTHY_PRIMARY if self.last_primary.state == NORMAL else DEGRADED_WARNING
        if emergency:
            return [self._alert("EMERGENCY_FAILBACK", "CRITICAL",
                                f"Site {self.site}: backup link went CRITICAL while primary is "
                                f"{self.last_primary.state}; traffic moved back to primary in {duration:.1f}ms.",
                                duration)]
        return [self._alert("FAILBACK_TO_PRIMARY", "INFO",
                            f"Site {self.site} failed back to primary after "
                            f"{self.cfg.hold_down_seconds:g}s hold-down ({duration:.1f}ms).", duration)]

    # ----------------------------------------------------------- operator ack
    def ack(self, now: Optional[float] = None) -> List[Alert]:
        with self.lock:
            if self.state != FLAP_LOCKED:
                raise ValueError(f"site {self.site} is {self.state}, not {FLAP_LOCKED}")
            now = self.clock() if now is None else now
            self.history.clear()
            if self.last_primary.state == NORMAL:
                self.state, self.hold_started = RECOVERY_HOLD, now
            else:
                self.state, self.hold_started = FAILED_OVER_BACKUP, None
            return [self._alert("FLAP_LOCK_CLEARED", "INFO",
                                f"Operator acknowledged flap lock on site {self.site}; "
                                f"state is now {self.state}.")]

    # ------------------------------------------------------------- reconcile
    def reconcile(self) -> Optional[Alert]:
        """Make sure the kernel route matches what the FSM wants (e.g. after noc-core restarts)."""
        with self.lock:  # same lock as evaluate(): no race with an in-flight switch
            want = self.active_link
            actual = self.driver.get_active_route(self.site)
            if actual is not None and actual["link"] == want:
                return None
            res = self.driver.set_route(self.site, want)
            have = "none" if actual is None else actual["link"]
            if not res.ok:
                log.warning("%s: route drift (kernel=%s, want=%s) and repair failed: %s",
                            self.site, have, want, res.error)
                return None
            log.warning("%s: route drift repaired (kernel=%s -> %s)", self.site, have, want)
            return self._alert("ROUTE_RECONCILED", "WARNING",
                               f"Site {self.site}: kernel route was {have}, FSM wants {want}; route re-applied.")