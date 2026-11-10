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


templates.env.filters.update(ts=_ts, ago=_ago)
templates.env.globals.update(version=__version__, pct=_pct)


def render(request: Request, name: str, status_code: int = 200, **context: object) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def error_page(request: Request, status_code: int, message: str) -> HTMLResponse:
    return render(request, "error.html", status_code=status_code, code=status_code, message=message)
