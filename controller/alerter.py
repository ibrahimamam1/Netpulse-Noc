"""Structured alert dispatch: stdout log + in-memory history + optional async webhook."""
from __future__ import annotations

import asyncio
import itertools
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Deque, List, Optional, Set

import httpx

from config import Config
from fsm import Alert

log = logging.getLogger("netpulse.alert")


class AlertDispatcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.url = cfg.alert_webhook_url
        self._history: Deque[dict] = deque(maxlen=cfg.alert_history)
        self._ids = itertools.count(1)
        self._tasks: Set[asyncio.Task] = set()
        self._client: Optional[httpx.AsyncClient] = None
        self._fmt = self._pick_format()
        self.webhook_failures = 0

    def _pick_format(self) -> str:
        if self.cfg.webhook_format != "auto":
            return self.cfg.webhook_format
        u = self.url.lower()
        if "hooks.slack.com" in u:
            return "slack"
        if "discord.com/api/webhooks" in u or "discordapp.com/api/webhooks" in u:
            return "discord"
        return "generic"

    async def start(self) -> None:
        if self.url:
            self._client = httpx.AsyncClient(timeout=self.cfg.webhook_timeout_s)
            log.info("alert webhook enabled (%s format)", self._fmt)
        else:
            log.info("ALERT_WEBHOOK_URL not set: alerts are logged and kept in memory only")

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._client:
            await self._client.aclose()

    def payload(self, a: Alert) -> dict:
        return {
            "id": next(self._ids),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "site": a.site,
            "event_type": a.event_type,
            "severity": a.severity,
            "active_link": a.active_link,
            "fsm_state": a.fsm_state,
            "failover_duration_ms": a.failover_duration_ms,
            "metrics_snapshot": a.metrics_snapshot,
            "message": a.message,
        }

    async def dispatch(self, a: Alert) -> dict:
        p = self.payload(a)
        self._history.append(p)
        dur = "" if p["failover_duration_ms"] is None else f" {p['failover_duration_ms']}ms"
        line = f"ALERT [{p['severity']}] {p['site']} {p['event_type']} active={p['active_link']}{dur} :: {p['message']}"
        (log.warning if p["severity"] == "CRITICAL" else log.info)(line)
        if self._client is not None:
            # fire-and-forget: a slow or dead webhook must never delay routing
            task = asyncio.create_task(self._post(p))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return p

    async def _post(self, p: dict) -> None:
        text = f"[{p['severity']}] {p['site']}: {p['message']}"
        body = {"text": text} if self._fmt == "slack" else {"content": text} if self._fmt == "discord" else p
        try:
            r = await self._client.post(self.url, json=body)
            if r.status_code >= 400:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
        except Exception as exc:
            self.webhook_failures += 1
            log.warning("webhook delivery failed (%s): %s", type(exc).__name__, exc)

    def recent(self, site: Optional[str] = None, limit: int = 50) -> List[dict]:
        items = [a for a in self._history if site is None or a["site"] == site]
        return list(reversed(items))[:limit]