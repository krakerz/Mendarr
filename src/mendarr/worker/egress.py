"""Egress identity: which public IP the worker's traffic leaves from."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

import httpx

from mendarr.proxy import UpstreamProxy, redact
from mendarr.rpc import EgressInfo, ProxySpec

log = logging.getLogger("mendarr.worker.egress")

FAILURE_RETRY_S = 15.0  # retry pause after a failed lookup or a missing country (capped by the TTL)


class Egress:
    """The exit IP, refreshed by a background task (`run`); `info()` only returns what was stored."""

    def __init__(
        self,
        url: str,
        ttl: float = 60.0,
        clock: Callable[[], float] = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
        upstream: UpstreamProxy | None = None,
        country_url: str | None = None,
    ) -> None:
        self._url = url
        self._ttl = ttl
        self._clock = clock
        self._transport = transport
        self._upstream = upstream
        self._via = str(upstream) if upstream else None
        self._country_url = country_url
        self._stored: EgressInfo | None = None
        self._lock = asyncio.Lock()
        self._first_done = asyncio.Event()
        self._last_ok = True
        self._running = False

    async def info(self) -> EgressInfo:
        if not self._url:
            return EgressInfo(ip="default", proxy=self._via)
        if self._stored is not None:
            return self._stored
        return EgressInfo(ip="unknown", proxy=self._via)

    async def run(self) -> None:
        self._running = True
        try:
            while True:
                try:
                    ok = await self.refresh()
                except Exception:
                    log.exception("egress refresh crashed")
                    ok = False
                complete = ok and (self._stored is None or self._stored.country or not self._wants_country(self._stored.ip))
                await asyncio.sleep(self._ttl if complete else min(self._ttl, FAILURE_RETRY_S))
        finally:
            self._running = False

    async def refresh(self) -> bool:
        if not self._url:
            self._first_done.set()
            return True
        if self._lock.locked():  # single flight: share the lookup that is already running
            async with self._lock:
                return self._last_ok
        async with self._lock:
            try:
                self._last_ok = await self._lookup()
            finally:
                self._first_done.set()
            return self._last_ok

    async def _lookup(self) -> bool:
        prev = self._stored
        # through the upstream proxy: the cookie-cache key must be the real exit IP
        try:
            country = None
            async with self._client() as client:
                resp = await client.get(self._url)
                resp.raise_for_status()
                try:
                    data = resp.json()
                    ip = str(data["ip"]).strip()
                    if isinstance(data.get("country"), str):
                        country = data["country"].strip().upper()
                except (ValueError, KeyError, TypeError):
                    ip = resp.text.strip()
                if not ip:
                    raise ValueError("empty egress response")
                if not country and self._wants_country(ip):
                    if prev is not None and prev.ip == ip and prev.country:
                        country = prev.country  # cached per IP
                    else:
                        country = await self._lookup_country(client, ip)
        except Exception as e:
            log.warning("egress lookup failed%s: %s", f" via {self._via}" if self._via else "", e)
            if prev is not None:
                self._stored = prev.model_copy(update={"stale": True})
            return False
        if prev is not None and prev.ip != ip:
            log.info("egress IP changed: %s -> %s", prev.ip, ip)
        log.debug("egress refresh: ip=%s country=%s (was %s)", ip, country, prev.ip if prev else None)
        self._stored = EgressInfo(ip=ip, checked_at=self._clock(), proxy=self._via, country=country)
        return True

    def _wants_country(self, ip: str) -> bool:
        return bool(self._country_url) and ip not in ("default", "unknown")

    def _client(self) -> httpx.AsyncClient:
        proxy = self._upstream.httpx_url() if self._upstream else None
        return httpx.AsyncClient(timeout=5, transport=self._transport, proxy=proxy)

    async def _lookup_country(self, client: httpx.AsyncClient, ip: str) -> str | None:
        try:
            resp = await client.get(self._country_url.format(ip=ip))
            if getattr(resp, "is_success", False) or getattr(resp, "status_code", 0) == 200:
                value = resp.json().get("country")
                if isinstance(value, str) and value.strip():
                    return value.strip().upper()
        except Exception as e:
            log.debug("country lookup failed: %s", e)
        return None

    async def exit_ip(self, wait_s: float = 8.0) -> str:
        if self._url and not self._first_done.is_set():
            if self._running:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._first_done.wait(), wait_s)
            else:
                await self.refresh()
        return (await self.info()).ip

    async def current(self, proxy: ProxySpec | None = None) -> str:
        if proxy is not None:
            return f"proxy:{redact(proxy.url)}"
        return (await self.info()).ip
