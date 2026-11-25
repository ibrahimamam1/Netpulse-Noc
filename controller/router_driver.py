"""Applies and verifies real Linux routes inside noc-core via the Docker SDK."""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from config import Config, SiteRoute

log = logging.getLogger("netpulse.router")


@dataclass
class RouteResult:
    ok: bool
    duration_ms: float
    link: str = ""
    error: str = ""


class RouterDriver:
    """`ip route replace <lan>/32 via <next-hop>` / `ip route show <lan>/32` inside noc-core.

    In dry-run mode the same commands are applied to an in-memory table instead, so the
    controller can be exercised without Docker.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sites: Dict[str, SiteRoute] = {s.name: s for s in cfg.sites}
        self.connected = bool(cfg.dry_run)
        self.routes_ready = False
        self._client = None
        self._lock = threading.Lock()
        self._sim: Dict[str, str] = {}  # dry-run: prefix -> next hop

    # ------------------------------------------------------------ plumbing
    def _get_client(self):
        with self._lock:
            if self._client is None:
                import docker  # imported lazily so dry-run needs no Docker SDK
                self._client = docker.from_env()
            return self._client

    def _exec(self, cmd: List[str]) -> Tuple[int, str]:
        if self.cfg.dry_run:
            return self._exec_sim(cmd)
        try:
            container = self._get_client().containers.get(self.cfg.core_container_name)
            res = container.exec_run(cmd)
            self.connected = True
            return res.exit_code, (res.output or b"").decode(errors="replace").strip()
        except Exception as exc:  # docker.errors.*, requests.* ... all mean "cannot reach the router"
            self.connected = False
            self._client = None  # force a fresh client next time
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc

    def _exec_sim(self, cmd: List[str]) -> Tuple[int, str]:
        if cmd[:3] == ["ip", "route", "replace"] and "via" in cmd:
            self._sim[cmd[3]] = cmd[cmd.index("via") + 1]
            return 0, ""
        if cmd[:3] == ["ip", "route", "show"]:
            nh = self._sim.get(cmd[3])
            return 0, (f"{cmd[3].split('/')[0]} via {nh} dev eth0" if nh else "")
        return 1, f"dry-run: unsupported command {cmd}"

    def ping(self) -> bool:
        """Docker daemon reachable and the router container running."""
        if self.cfg.dry_run:
            return True
        try:
            c = self._get_client().containers.get(self.cfg.core_container_name)
            self.connected = (c.status == "running")
        except Exception:
            self.connected = False
            self._client = None
        return self.connected

    # --------------------------------------------------------------- routes
    def set_route(self, site: str, link: str) -> RouteResult:
        s = self.sites[site]
        nh = s.primary_nh if link == "primary" else s.backup_nh
        t0 = time.perf_counter()
        try:
            rc, out = self._exec(["ip", "route", "replace", f"{s.lan_ip}/32", "via", nh])
        except RuntimeError as exc:
            return RouteResult(False, (time.perf_counter() - t0) * 1000.0, link, str(exc))
        ms = (time.perf_counter() - t0) * 1000.0
        if rc != 0:
            return RouteResult(False, ms, link, f"ip route replace exit {rc}: {out}")
        log.info("route %s/32 via %s (%s) applied in %.1fms", s.lan_ip, nh, link, ms)
        return RouteResult(True, ms, link)

    def switch_to_backup(self, site: str) -> RouteResult:
        return self.set_route(site, "backup")

    def switch_to_primary(self, site: str) -> RouteResult:
        return self.set_route(site, "primary")

    def get_active_route(self, site: str) -> Optional[dict]:
        """Parse `ip route show <lan>/32` from noc-core. None if no route / router unreachable."""
        s = self.sites[site]
        try:
            rc, out = self._exec(["ip", "route", "show", f"{s.lan_ip}/32"])
        except RuntimeError:
            return None
        if rc != 0 or not out.strip():
            return None
        via = re.search(r"\bvia\s+(\S+)", out)
        dev = re.search(r"\bdev\s+(\S+)", out)
        nh = via.group(1) if via else None
        link = "primary" if nh == s.primary_nh else "backup" if nh == s.backup_nh else "unknown"
        return {"prefix": f"{s.lan_ip}/32", "via": nh, "dev": dev.group(1) if dev else None,
                "link": link, "raw": out.splitlines()[0]}

    def init_routes(self) -> bool:
        """Install the default primary route for every site (startup)."""
        ok = True
        for name in self.sites:
            res = self.set_route(name, "primary")
            if not res.ok:
                log.warning("init route for %s failed: %s", name, res.error)
                ok = False
        self.routes_ready = ok
        if ok:
            log.info("initial primary routes installed for %d site(s)", len(self.sites))
        return ok