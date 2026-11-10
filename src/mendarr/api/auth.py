"""Dashboard login: one user from MENDARR_UI_USER / MENDARR_UI_PASSWORD, an HMAC-signed session cookie, CSRF tokens."""

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import HTTPException, Request, WebSocketException, status
from starlette.requests import HTTPConnection

from mendarr.config import Settings

log = logging.getLogger(__name__)

COOKIE = "mendarr_session"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf"
_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_FORMS = ("application/x-www-form-urlencoded", "multipart/form-data")


def load_secret(settings: Settings) -> bytes:
    if settings.ui_secret:
        return settings.ui_secret.encode()
    path = Path(settings.data_dir) / "ui_secret"
    try:
        text = path.read_text(encoding="ascii").strip()
        if len(text) >= 32:
            return text.encode()
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError) as e:
        log.warning("dashboard: cannot read %s: %s", path, e)
    secret = secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(secret + "\n")
    except OSError as e:
        log.warning("dashboard: cannot keep the session secret in %s (%s); logins end on restart", path, e)
    return secret.encode()


class Auth:
    def __init__(self, settings: Settings, secret: bytes, clock: Callable[[], float] = time.time) -> None:
        self.user = settings.ui_user
        self._password = settings.ui_password
        self._key = hmac.new(secret, f"key|{settings.ui_password}".encode(), hashlib.sha256).digest()
        self.ttl_s = settings.ui_session_hours * 3600
        self._clock = clock

    @classmethod
    def from_settings(cls, settings: Settings) -> "Auth":
        auth = cls(settings, load_secret(settings))
        if not auth.configured:
            log.warning("dashboard login disabled: set MENDARR_UI_USER and MENDARR_UI_PASSWORD")
        return auth

    @property
    def configured(self) -> bool:
        return bool(self.user and self._password)

    def check_password(self, user: str, password: str) -> bool:
        if not self.configured:
            return False
        ok_user = hmac.compare_digest(user.encode(), self.user.encode())
        ok_pw = hmac.compare_digest(password.encode(), self._password.encode())
        return ok_user and ok_pw

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        payload = f"{self.user}|{int(self._clock())}|{secrets.token_hex(16)}"
        b = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return f"{b}.{self._sign(payload)}"

    def session(self, token: str | None) -> tuple[str, str] | None:
        """(user, nonce) for a valid, unexpired cookie of the configured user."""
        if not token or "." not in token or not self.configured:
            return None
        b, sig = token.rsplit(".", 1)
        try:
            payload = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        if not hmac.compare_digest(sig, self._sign(payload)):
            return None
        parts = payload.split("|")
        if len(parts) != 3:
            return None
        user, issued, nonce = parts
        if user != self.user or not issued.isdigit() or self._clock() - int(issued) > self.ttl_s:
            return None
        return user, nonce

    def csrf(self, nonce: str) -> str:
        return self._sign(f"csrf|{nonce}")


def safe_next(target: str) -> str:
    """Only same-app /ui targets after login (no open redirect)."""
    if (target == "/ui" or target.startswith(("/ui/", "/ui?"))) and not any(c in target for c in "\\\r\n"):
        return target
    return "/ui"


def login_url(conn: HTTPConnection) -> str:
    target = conn.url.path + (f"?{conn.url.query}" if conn.url.query else "")
    if conn.scope.get("method", "GET") != "GET":
        target = "/ui"
    return "/ui/login?next=" + quote(safe_next(target), safe="")


def _wants_json(conn: HTTPConnection) -> bool:
    accept = conn.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


async def _check_csrf(conn: HTTPConnection, token: str) -> None:
    sent = conn.headers.get(CSRF_HEADER)
    if not sent and isinstance(conn, Request) and conn.headers.get("content-type", "").startswith(_FORMS):
        sent = (await conn.form()).get(CSRF_FIELD)  # Starlette caches the parsed form for the route
    if not isinstance(sent, str) or not hmac.compare_digest(sent, token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad or missing CSRF token")


async def require_user(conn: HTTPConnection) -> str:
    """FastAPI dependency for every /ui route (HTTP and websocket)."""
    auth: Auth = conn.app.state.auth
    sess = auth.session(conn.cookies.get(COOKIE))
    if conn.scope["type"] == "websocket":
        origin = conn.headers.get("origin")
        if sess is None or (origin and urlsplit(origin).netloc != conn.headers.get("host")):
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="login required")
        conn.state.user = sess[0]
        return sess[0]
    if sess is None:
        if conn.headers.get("hx-request"):
            raise HTTPException(401, "login required", headers={"HX-Redirect": login_url(conn)})
        if _wants_json(conn):
            raise HTTPException(401, "login required")
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": login_url(conn)})
    user, nonce = sess
    token = auth.csrf(nonce)
    conn.state.user = user
    conn.state.csrf = token
    if conn.scope.get("method") in _UNSAFE:
        await _check_csrf(conn, token)
    return user
