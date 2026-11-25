"""Environment-driven configuration for noc-controller."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class SiteRoute:
    name: str
    lan_ip: str        # virtual enterprise LAN address (/32) on the site's loopback
    primary_nh: str    # next hop over wan_primary
    backup_nh: str     # next hop over wan_backup


DEFAULT_SITES: Tuple[SiteRoute, ...] = (
    SiteRoute("douala", "10.10.1.1", "172.20.1.11", "172.20.2.11"),
    SiteRoute("yaounde", "10.10.2.1", "172.20.1.12", "172.20.2.12"),
    SiteRoute("edge", "10.10.3.1", "172.20.1.13", "172.20.2.13"),
)


def _num(name: str, default, cast=float):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return cast(raw)
    except ValueError as exc:
        raise ValueError(f"invalid value for {name}: {raw!r}") from exc


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # --- spec parameters ---------------------------------------------------
    engine_url: str = "http://172.20.1.20:9200/api/v1/status"
    poll_interval_s: float = 0.5
    core_container_name: str = "noc-core"
    hold_down_seconds: float = 20.0
    flap_window_seconds: float = 120.0
    flap_max_transitions: int = 3
    alert_webhook_url: str = ""
    http_port: int = 9300

    # --- extras ------------------------------------------------------------
    webhook_format: str = "auto"       # auto | generic | slack | discord
    reconcile_interval_s: float = 5.0  # verify kernel routes match the FSM this often
    engine_timeout_s: float = 1.0
    webhook_timeout_s: float = 2.0
    alert_history: int = 100
    dry_run: bool = False              # simulate routes in memory, never touch Docker
    sites: Tuple[SiteRoute, ...] = DEFAULT_SITES

    @classmethod
    def from_env(cls) -> "Config":
        d = cls()
        sites = d.sites
        if os.environ.get("SITES_JSON"):
            try:
                sites = tuple(SiteRoute(s["name"], s["lan_ip"], s["primary_nh"], s["backup_nh"])
                              for s in json.loads(os.environ["SITES_JSON"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid SITES_JSON: {exc}") from exc
        cfg = cls(
            engine_url=os.environ.get("ENGINE_URL", d.engine_url),
            poll_interval_s=_num("POLL_INTERVAL_S", d.poll_interval_s),
            core_container_name=os.environ.get("CORE_CONTAINER_NAME", d.core_container_name),
            hold_down_seconds=_num("HOLD_DOWN_SECONDS", d.hold_down_seconds),
            flap_window_seconds=_num("FLAP_WINDOW_SECONDS", d.flap_window_seconds),
            flap_max_transitions=_num("FLAP_MAX_TRANSITIONS", d.flap_max_transitions, int),
            alert_webhook_url=os.environ.get("ALERT_WEBHOOK_URL", "").strip(),
            http_port=_num("HTTP_PORT", d.http_port, int),
            webhook_format=os.environ.get("WEBHOOK_FORMAT", d.webhook_format).strip().lower(),
            reconcile_interval_s=_num("RECONCILE_INTERVAL_S", d.reconcile_interval_s),
            engine_timeout_s=_num("ENGINE_TIMEOUT_S", d.engine_timeout_s),
            webhook_timeout_s=_num("WEBHOOK_TIMEOUT_S", d.webhook_timeout_s),
            alert_history=_num("ALERT_HISTORY", d.alert_history, int),
            dry_run=_flag("DRY_RUN", d.dry_run),
            sites=sites,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.poll_interval_s <= 0:
            raise ValueError("POLL_INTERVAL_S must be > 0")
        if self.hold_down_seconds < 0 or self.flap_window_seconds <= 0:
            raise ValueError("HOLD_DOWN_SECONDS must be >= 0 and FLAP_WINDOW_SECONDS > 0")
        if self.flap_max_transitions < 2:
            raise ValueError("FLAP_MAX_TRANSITIONS must be >= 2")
        if not 1 <= self.http_port <= 65535:
            raise ValueError("HTTP_PORT out of range")
        if self.webhook_format not in ("auto", "generic", "slack", "discord"):
            raise ValueError("WEBHOOK_FORMAT must be auto, generic, slack or discord")
        if not self.sites:
            raise ValueError("no sites configured")