"""Dashboard routes for manual solves: start from an incident, watch/cancel a session."""

import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from mendarr.api.auth import require_user
from mendarr.core.manual import ManualError, ManualManager, ManualSession
from mendarr.web import error_page, render

router = APIRouter(prefix="/ui", dependencies=[Depends(require_user)])


def _manager(request: Request) -> ManualManager:
    return request.app.state.manual


def _go(request: Request, url: str) -> Response:
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _session(request: Request, sid: str) -> ManualSession | None:
    return _manager(request).get(sid)


@router.post("/incidents/{iid}/manual")
async def start_manual(request: Request, iid: int) -> Response:
    try:
        s = await _manager(request).start(iid)
    except ManualError as e:
        return _go(request, f"/ui/manual?error={quote(str(e))}")
    return _go(request, f"/ui/manual/{s.id}")


@router.post("/manual/start")
async def start_adhoc_manual(request: Request, url: str = Form(), backend: str = Form("camoufox")) -> Response:
    try:
        s = await _manager(request).start_adhoc(url.strip(), backend=backend.strip())
    except (ManualError, ValueError) as e:
        return _go(request, f"/ui/manual?error={quote(str(e))}")
    return _go(request, f"/ui/manual/{s.id}")


@router.post("/manual/clear")
async def clear_manual_sessions(request: Request) -> Response:
    count = _manager(request).clear_finished()
    return _go(request, f"/ui/manual?ok={quote(f'Cleared {count} finished session(s)')}")


@router.get("/manual", response_class=HTMLResponse)
async def list_manual(request: Request, error: str = "") -> Response:
    return render(request, "manual_list.html", active="manual", sessions=_manager(request).sessions(), error=error)


@router.get("/manual/{sid}", response_class=HTMLResponse)
async def manual_session(request: Request, sid: str) -> Response:
    s = _session(request, sid)
    if s is None:
        return error_page(request, 404, "Unknown manual solve session.")
    return render(request, "manual_session.html", active="manual", s=s, now=time.time())


@router.get("/manual/{sid}/status", response_class=HTMLResponse)
async def manual_status(request: Request, sid: str) -> Response:
    s = _session(request, sid)
    if s is None:
        return error_page(request, 404, "Unknown manual solve session.")
    resp = render(request, "_manual_status.html", active="manual", s=s, now=time.time())
    if not s.active:
        resp.headers["HX-Trigger"] = "manual-ended"
    return resp


@router.post("/manual/{sid}/cancel")
async def cancel_manual(request: Request, sid: str) -> Response:
    if _session(request, sid) is None:
        return error_page(request, 404, "Unknown manual solve session.")
    await _manager(request).cancel(sid)
    return _go(request, f"/ui/manual/{sid}")
