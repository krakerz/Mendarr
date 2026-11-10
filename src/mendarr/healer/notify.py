"""Discord and JSON-webhook notifications. Never raises."""

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import httpx

from mendarr.healer.config import NotifyConfig

log = logging.getLogger(__name__)

Event = Literal["healed", "manual", "rollback", "default_proposed", "gave_up", "agent_wanted"]
TITLES = {
    "healed": "Healed",
    "manual": "Needs a manual solve",
    "rollback": "Plugin rolled back",
    "default_proposed": "New _default proposed",
    "gave_up": "Gave up",
    "agent_wanted": "Waiting for an agent",
}
COLORS = {
    "healed": 0x2E7D32,
    "manual": 0xC62828,
    "rollback": 0xEF6C00,
    "default_proposed": 0x1565C0,
    "gave_up": 0x616161,
    "agent_wanted": 0x6A1B9A,
}


@dataclass(frozen=True)
class Notice:
    event: Event
    domain: str
    message: str
    incident_id: int | None = None
    kind: str = ""
    plugin: str = ""
    screenshot: bytes | None = None


class Notifier:
    def __init__(self, public_url: str = "", transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._public_url = public_url.rstrip("/")
        self._http = httpx.AsyncClient(transport=transport)

    def link(self, n: Notice) -> str:
        if self._public_url and n.incident_id is not None:
            return f"{self._public_url}/ui/incidents/{n.incident_id}"
        return self._public_url

    def _embed(self, n: Notice) -> dict:
        fields = [
            {"name": name, "value": value, "inline": True}
            for name, value in (("Failure", n.kind), ("Plugin", n.plugin))
            if value
        ]
        embed: dict = {
            "title": f"{TITLES[n.event]}: {n.domain}",
            "description": n.message[:2000],
            "color": COLORS[n.event],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "fields": fields,
        }
        if link := self.link(n):
            embed["url"] = link
        if n.screenshot:
            embed["thumbnail"] = {"url": "attachment://screenshot.png"}
        return embed

    async def _discord(self, cfg: NotifyConfig, n: Notice) -> httpx.Response:
        payload: dict = {"username": "Mendarr", "embeds": [self._embed(n)]}
        if cfg.discord_ping.strip():
            payload["content"] = cfg.discord_ping.strip()
        if n.screenshot:
            return await self._http.post(
                cfg.discord_webhook,
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": ("screenshot.png", n.screenshot, "image/png")},
                timeout=cfg.timeout_s,
            )
        return await self._http.post(cfg.discord_webhook, json=payload, timeout=cfg.timeout_s)

    async def _webhook(self, cfg: NotifyConfig, n: Notice) -> httpx.Response:
        body = {
            "event": n.event,
            "domain": n.domain,
            "incident_id": n.incident_id,
            "kind": n.kind,
            "plugin": n.plugin,
            "message": n.message,
            "url": self.link(n),
            "at": time.time(),
        }
        return await self._http.post(cfg.webhook_url, json=body, timeout=cfg.timeout_s)

    async def send(
        self, cfg: NotifyConfig, n: Notice, channels: Iterable[str] = ("discord", "webhook")
    ) -> dict[str, bool]:
        if not getattr(cfg.events, n.event, False):
            return {}
        wanted = set(channels)
        out: dict[str, bool] = {}
        for name, enabled, call in (
            ("discord", bool(cfg.discord_webhook), self._discord),
            ("webhook", bool(cfg.webhook_url), self._webhook),
        ):
            if name not in wanted or not enabled:
                continue
            try:
                r = await call(cfg, n)
                out[name] = r.is_success
                if not r.is_success:
                    log.warning("notify %s via %s failed: HTTP %d", n.event, name, r.status_code)
            except Exception as e:  # never let a notification break the healer
                log.warning("notify %s via %s failed: %s: %s", n.event, name, type(e).__name__, e)
                out[name] = False
        return out

    async def aclose(self) -> None:
        await self._http.aclose()
