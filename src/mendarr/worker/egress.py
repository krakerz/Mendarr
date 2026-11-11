"""Egress identity: which public IP the worker's traffic leaves from."""

import logging
import time
from collections.abc import Callable

import httpx

from mendarr.proxy import UpstreamProxy, redact
from mendarr.rpc import EgressInfo, ProxySpec

log = logging.getLogger("mendarr.worker.egress")

FAILURE_RETRY_S = 15.0  # how long a failed lookup is remembered before trying again


class Egress:
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
        self._cached: EgressInfo | None = None
        self._failed_until = 0.0
        self._country_retry_at: float | None = None

    async def info(self) -> EgressInfo:
        if not self._url:
            return EgressInfo(ip="default", proxy=self._via)
        now = self._clock()
        if self._cached is not None and now - self._cached.checked_at < self._ttl:
            if self._country_retry_at is not None and now >= self._country_retry_at:
                await self._retry_country(self._cached, now)
            return self._cached
        if now < self._failed_until:  # a dead endpoint: don't make every caller wait for the timeout again
            return EgressInfo(ip="unknown", proxy=self._via)
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
                wants_country = bool(self._country_url) and ip not in ("default", "unknown")
                if not country and wants_country:
                    country = await self._lookup_country(client, ip)
        except Exception as e:
            log.warning("egress lookup failed%s: %s", f" via {self._via}" if self._via else "", e)
            self._failed_until = now + FAILURE_RETRY_S
            return EgressInfo(ip="unknown", proxy=self._via)
        self._failed_until = 0.0
        self._cached = EgressInfo(ip=ip, checked_at=now, proxy=self._via, country=country)
        # keep the IP for the whole TTL, but retry a failed country lookup soon
        self._country_retry_at = now + FAILURE_RETRY_S if not country and wants_country else None
        return self._cached

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

    async def _retry_country(self, cached: EgressInfo, now: float) -> None:
        self._country_retry_at = now + FAILURE_RETRY_S  # until it works
        async with self._client() as client:
            country = await self._lookup_country(client, cached.ip)
        if country:
            self._cached = cached.model_copy(update={"country": country})
            self._country_retry_at = None

    async def exit_ip(self) -> str:
        return (await self.info()).ip

    async def current(self, proxy: ProxySpec | None = None) -> str:
        if proxy is not None:
            return f"proxy:{redact(proxy.url)}"
        return (await self.info()).ip
