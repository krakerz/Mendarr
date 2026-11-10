"""Upstream proxy: the worker's default egress (MENDARR_PROXY_URL / MENDARR_PROXY_TYPE) and proxy URL helpers."""

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from mendarr.rpc import ProxySpec

TYPES = ("http", "https", "socks5")
_ALIASES = {"socks5h": "socks5"}
_PROXY_ERROR = re.compile(r"proxy|socks|tunnel", re.IGNORECASE)


class ProxyConfigError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class UpstreamProxy:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @property
    def server(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"

    def __repr__(self) -> str:  # logs and errors show scheme://host:port, never the password
        return self.server

    __str__ = __repr__

    def spec(self) -> ProxySpec:
        return ProxySpec(url=self.server, username=self.username, password=self.password)

    def playwright(self) -> dict[str, str]:
        d = {"server": self.server, "username": self.username, "password": self.password}
        return {k: v for k, v in d.items() if v is not None}

    def httpx_url(self) -> str:
        return with_credentials(self.server, self.username, self.password)


def _norm(t: str) -> str:
    t = t.strip().lower()
    return _ALIASES.get(t, t)


def parse_upstream(url: str, type_: str = "", username: str = "", password: str = "") -> UpstreamProxy | None:
    """host:port (type from MENDARR_PROXY_TYPE, default http) or scheme://[user:pass@]host:port."""
    url, type_ = url.strip(), _norm(type_)
    if not url:
        if type_ or username or password:
            raise ProxyConfigError("MENDARR_PROXY_TYPE/USERNAME/PASSWORD are set but MENDARR_PROXY_URL is empty")
        return None
    if "://" in url:
        raw_scheme, rest = url.split("://", 1)
        scheme = _norm(raw_scheme)
        if type_ and type_ != scheme:
            raise ProxyConfigError(f"MENDARR_PROXY_URL says {scheme!r} but MENDARR_PROXY_TYPE says {type_!r}")
    else:
        scheme, rest = type_ or "http", url
    if scheme not in TYPES:
        raise ProxyConfigError(f"unsupported proxy type {scheme!r}: use http, https or socks5")
    parts = urlsplit(f"{scheme}://{rest}")
    try:
        port = parts.port
    except ValueError:
        port = None
    if not parts.hostname or port is None:
        raise ProxyConfigError(f"MENDARR_PROXY_URL must be host:port or scheme://host:port, got {redact(url)!r}")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ProxyConfigError(f"MENDARR_PROXY_URL must not have a path or query, got {redact(url)!r}")
    if parts.username is not None:
        if username or password:
            raise ProxyConfigError("proxy credentials are set both in MENDARR_PROXY_URL and MENDARR_PROXY_USERNAME/PASSWORD")
        user = unquote(parts.username)
        pw = unquote(parts.password) if parts.password is not None else None
    else:
        user, pw = username or None, password or None
    if pw is not None and not user:
        raise ProxyConfigError("MENDARR_PROXY_PASSWORD needs MENDARR_PROXY_USERNAME")
    if scheme == "socks5" and user:
        raise ProxyConfigError(
            "SOCKS5 with a username/password is not supported: Camoufox and Chromium only authenticate HTTP(S) "
            "proxies. Use an HTTP proxy for credentials, or a SOCKS5 proxy without them"
        )
    return UpstreamProxy(scheme, parts.hostname, port, user, pw)


def redact(url: str) -> str:
    """A proxy URL without credentials, path or query."""
    if "://" not in url:
        return url.rsplit("@", 1)[-1]
    parts = urlsplit(url)
    return parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1], path="", query="", fragment="").geturl()


def with_credentials(url: str, username: str | None, password: str | None) -> str:
    if not username:
        return url
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    cred = quote(username, safe="") + (f":{quote(password, safe='')}" if password is not None else "")
    return parts._replace(netloc=f"{cred}@{host}").geturl()


def curl_proxy_url(proxy: ProxySpec) -> str:
    """curl form: credentials in the URL, socks5h so DNS resolves at the proxy, not on the worker."""
    url = with_credentials(proxy.url, proxy.username, proxy.password)
    return re.sub(r"^socks5://", "socks5h://", url, flags=re.IGNORECASE)


def describe_failure(detail: str, proxy: ProxySpec | None) -> str:
    """Name the proxy in a connection error from the proxy hop (a dead proxy fails, it never goes direct)."""
    if proxy is None:
        return detail
    host = urlsplit(proxy.url if "://" in proxy.url else f"http://{proxy.url}").hostname
    if _PROXY_ERROR.search(detail) or (host and f"{host} port" in detail):
        return f"Upstream proxy {redact(proxy.url)} failed: {detail}"
    return detail
