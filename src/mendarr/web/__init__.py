"""Jinja2 environment for the dashboard; templates/ and static/ live next to this file."""

import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from mendarr import __version__

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
templates = Jinja2Templates(directory=WEB_DIR / "templates")


def _ts(v: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v)) if v else "—"


def _ago(v: float | None, now: float | None = None) -> str:
    if not v:
        return "—"
    d = (now if now is not None else time.time()) - v
    future, d = d < 0, abs(d)
    for unit, s in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= s:
            n = f"{int(d // s)}{unit}"
            break
    else:
        n = f"{int(d)}s"
    return f"in {n}" if future else f"{n} ago"


def _pct(ok: int, fail: int) -> str:
    n = ok + fail
    return f"{ok * 100 // n}%" if n else "—"


def _short_ua(ua: str | None) -> str:
    if not ua:
        return "—"
    browser = ""
    if "Firefox/" in ua:
        ver = ua.split("Firefox/")[-1].split()[0]
        browser = f"Firefox {ver}"
    elif "Chrome/" in ua or "CriOS/" in ua:
        ver = (ua.split("Chrome/")[-1] if "Chrome/" in ua else ua.split("CriOS/")[-1]).split()[0]
        browser = f"Chrome {ver}"
    elif "Safari/" in ua and "Version/" in ua:
        ver = ua.split("Version/")[-1].split()[0]
        browser = f"Safari {ver}"
    elif "Edge/" in ua or "Edg/" in ua:
        ver = (ua.split("Edg/")[-1] if "Edg/" in ua else ua.split("Edge/")[-1]).split()[0]
        browser = f"Edge {ver}"
    else:
        browser = ua[:24]

    os_name = ""
    if "Windows" in ua:
        os_name = "Win"
    elif "Macintosh" in ua or "Mac OS" in ua:
        os_name = "macOS"
    elif "Linux" in ua:
        os_name = "Linux"
    elif "Android" in ua:
        os_name = "Android"
    elif "iPhone" in ua or "iPad" in ua:
        os_name = "iOS"

    if browser and os_name:
        return f"{browser} ({os_name})"
    return browser or ua[:24]


templates.env.filters.update(ts=_ts, ago=_ago, short_ua=_short_ua)
templates.env.globals.update(version=__version__, pct=_pct)


def render(request: Request, name: str, status_code: int = 200, **context: object) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def error_page(request: Request, status_code: int, message: str) -> HTMLResponse:
    return render(request, "error.html", status_code=status_code, code=status_code, message=message)
