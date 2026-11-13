"""Dashboard: login, overview, cookies. Incidents/plugins/settings live in ui_incidents/ui_plugins/ui_settings."""

import asyncio
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response

from mendarr.api import ui_incidents, ui_plugins, ui_settings
from mendarr.api.auth import COOKIE, Auth, require_user, safe_next
from mendarr.api.worker_client import WorkerUnavailable
from mendarr.core.cookies import lifetime_cookie
from mendarr.core.stats import CHECK_MISMATCH
from mendarr.healer.config import refresh_policy
from mendarr.plugins.store import DEFAULT_SCOPE, OPEN_INCIDENTS
from mendarr.web import render

router = APIRouter(prefix="/ui")
AUTH = [Depends(require_user)]
_FAIL_DELAY_S = 1.0  # slows password guessing; tests set 0


def _auth(request: Request) -> Auth:
    return request.app.state.auth


@router.get("/login")
def login_page(request: Request, next_: str = Query("/ui", alias="next")) -> Response:
    auth = _auth(request)
    target = safe_next(next_)
    if auth.session(request.cookies.get(COOKIE)):
        return RedirectResponse(target, status_code=303)
    return render(request, "login.html", configured=auth.configured, next=target, error="")


@router.post("/login")
async def login(
    request: Request,
    user: str = Form(""),
    password: str = Form(""),
    next_: str = Form("/ui", alias="next"),
) -> Response:
    auth = _auth(request)
    target = safe_next(next_)
    if not auth.configured:
        return render(request, "login.html", status_code=503, configured=False, next=target, error="")
    if not auth.check_password(user, password):
        await asyncio.sleep(_FAIL_DELAY_S)
        return render(
            request, "login.html", status_code=401, configured=True, next=target, error="Wrong user or password"
        )
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(
        COOKIE,
        auth.issue(),
        max_age=int(auth.ttl_s),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return resp


@router.post("/logout", dependencies=AUTH)
def logout() -> Response:
    resp = RedirectResponse("/ui/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("", dependencies=AUTH)
def overview(request: Request) -> Response:
    st = request.app.state
    now = time.time()
    day, week = st.stats.summary(now - 86400), st.stats.summary(now - 7 * 86400)
    cookies = st.cache.entries()
    incidents = [i for i in st.store.incidents() if i["state"] in OPEN_INCIDENTS]
    scopes = {p.scope for p in st.store.list() if p.scope != DEFAULT_SCOPE}
    last = st.stats.last()
    mismatch = st.stats.counters(CHECK_MISMATCH)
    # domains that never showed a challenge have no solve stats; the last-outcome table keeps them listed
    quiet = {d for d, (outcome, at) in last.items() if outcome == "no_challenge" and at >= now - 7 * 86400}
    domains = sorted(set(week) | {c.domain for c in cookies} | scopes | {i["domain"] for i in incidents} | quiet)
    rows = []
    for d in domains:
        plugin = st.store.resolve(d)
        rows.append(
            {
                "domain": d,
                "day": day.get(d),
                "week": week.get(d),
                "plugin": plugin.ref,
                "scope": plugin.scope,
                "fallback": plugin.scope == DEFAULT_SCOPE,
                "cookie_expires": max((c.expires_at for c in cookies if c.domain == d), default=None),
                "incidents": [i for i in incidents if i["domain"] == d],
                "last": last.get(d),
                "mismatch": mismatch.get(d, 0),
            }
        )
    cfg = st.config_file.get()
    active_items = st.tracker.items() if hasattr(st, "tracker") and st.tracker else []
    return render(
        request,
        "overview.html",
        active="overview",
        rows=rows,
        incidents=incidents[:10],
        cookies=len(cookies),
        healer_on=st.settings.healer_enabled and cfg.healer.enabled,
        llm_order=cfg.llm.order,
        now=now,
        active_items=active_items,
    )


@router.get("/active", dependencies=AUTH)
def active_solves(request: Request) -> Response:
    """Active in-progress automated solves and healer operations, polled by htmx."""
    st = request.app.state
    active_items = st.tracker.items() if hasattr(st, "tracker") and st.tracker else []
    return render(request, "_active_solves.html", active_items=active_items)


@router.get("/egress", dependencies=AUTH)
async def egress_card(request: Request) -> Response:
    """Overview card, loaded by htmx so a slow egress lookup never delays the page."""
    try:
        info = await request.app.state.worker.egress()
        ip, proxy, country, stale, checked_at = info.ip, info.proxy, info.country, info.stale, info.checked_at
    except (WorkerUnavailable, TimeoutError):
        ip, proxy, country, stale, checked_at = "worker unavailable", None, None, False, 0
    return render(request, "_egress.html", ip=ip, proxy=proxy, country=country, stale=stale, checked_at=checked_at)


@router.get("/cookies", dependencies=AUTH)
def cookies_page(request: Request) -> Response:
    st = request.app.state
    now = time.time()
    interval_s, _ = refresh_policy(st.settings, st.config_file.get())
    rows = []
    for e in st.cache.entries():
        lifetime = lifetime_cookie(e)
        rows.append(
            {
                "entry": e,
                "cookies": [
                    {
                        "name": c.name,
                        "expires": c.expires if c.expires > 0 else None,
                        "session": c.expires <= 0,
                        "expired": 0 < c.expires <= now,
                        "sets_lifetime": c.name == lifetime,
                    }
                    for c in e.cookies
                ],
                "check_at": e.last_check_at or None,
                "check_result": e.last_check_result,
            }
        )
    # every domain is checked in the refresher's next round; without a running refresher, estimate from the policy
    next_at = {}
    if interval_s > 0:
        round_at = getattr(getattr(request.app.state, "refresher", None), "next_at", None)
        for r in rows:
            e = r["entry"]
            next_at[e.domain] = round_at or max(now, (e.last_check_at or now) + interval_s)
    return render(
        request, "cookies.html", active="cookies", rows=rows, entries=[r["entry"] for r in rows], now=now,
        refresh_on=interval_s > 0, next_at=next_at,
    )


@router.post("/cookies/purge", dependencies=AUTH)
def cookies_purge(request: Request, domain: str = Form("")) -> Response:
    n = request.app.state.cache.purge(domain or None)
    msg = f"Purged {n} cookie entr{'y' if n == 1 else 'ies'} for {domain or 'all domains'}"
    return RedirectResponse(f"/ui/cookies?ok={quote(msg)}", status_code=303)


router.include_router(ui_incidents.router)
router.include_router(ui_plugins.router)
router.include_router(ui_settings.router)
