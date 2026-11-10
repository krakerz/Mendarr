"""Egress identity: which public IP the worker's traffic leaves from."""

import logging
import time
from collections.abc import Callable

import httpx

from mendarr.proxy import UpstreamProxy, redact
from mendarr.rpc import EgressInfo, ProxySpec

log = logging.getLogger("mendarr.worker.egress")


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

    async def info(self) -> EgressInfo:
        if not self._url:
            return EgressInfo(ip="default", proxy=self._via)
        now = self._clock()
        if self._cached is not None and now - self._cached.checked_at < self._ttl:
            return self._cached
        # through the upstream proxy: the cookie-cache key must be the real exit IP
        proxy = self._upstream.httpx_url() if self._upstream else None
        try:
            country = None
            async with httpx.AsyncClient(timeout=5, transport=self._transport, proxy=proxy) as client:
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
                if not country and self._country_url and ip not in ("default", "unknown"):
                    try:
                        c_resp = await client.get(self._country_url.format(ip=ip))
                        if getattr(c_resp, "is_success", False) or getattr(c_resp, "status_code", 0) == 200:
                            c_data = c_resp.json()
                            if isinstance(c_data.get("country"), str):
                                country = c_data["country"].strip().upper()
                    except Exception:
                        pass
        except Exception as e:
            log.warning("egress lookup failed%s: %s", f" via {self._via}" if self._via else "", e)
            return EgressInfo(ip="unknown", proxy=self._via)
        self._cached = EgressInfo(ip=ip, checked_at=now, proxy=self._via, country=country)
        return self._cached

    async def exit_ip(self) -> str:
        return (await self.info()).ip

    async def current(self, proxy: ProxySpec | None = None) -> str:
        if proxy is not None:
            return f"proxy:{redact(proxy.url)}"
        return (await self.info()).ip
