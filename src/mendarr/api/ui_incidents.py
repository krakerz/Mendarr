"""Dashboard: incidents list/detail, heal transcripts, retry/close and the failure-trace viewer."""

import json
import re
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from mendarr.api.auth import require_user
from mendarr.plugins.store import OPEN_INCIDENTS
from mendarr.web import error_page, render

router = APIRouter(dependencies=[Depends(require_user)])

_STATES = ("manual", "solved", "gave_up")
_REF = re.compile(r"^heal/\d+/a\d+-(prompt\.json|response\.md)$")
_PLUGIN_REF = re.compile(r"^(?P<scope>.+)@v(?P<version>\d+)$")
_DOM_CSP = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"


def _ref_path(data_dir: str, ref: str) -> Path | None:
    if not _REF.match(ref or ""):
        return None
    root = Path(data_dir).resolve()
    p = (root / ref).resolve()
    return p if p.is_relative_to(root) and p.is_file() else None


def _read(data_dir: str, ref: str) -> str | None:
    p = _ref_path(data_dir, ref)
    if p is None:
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _messages(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except ValueError:
        return [{"role": "prompt", "text": raw}]
    if not isinstance(data, list):
        return [{"role": "prompt", "text": raw}]
    out = []
    for msg in data:
        if not isinstance(msg, dict):
            out.append({"role": "?", "text": str(msg)})
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
                elif isinstance(part, dict) and "text" in part:
                    parts.append(str(part["text"]))
                else:
                    parts.append("[image omitted]")
            content = "\n".join(parts)
        out.append({"role": str(msg.get("role", "?")), "text": str(content)})
    return out


def _redirect(iid: int, ok: str) -> RedirectResponse:
    return RedirectResponse(f"/ui/incidents/{iid}?ok={quote(ok)}", status_code=303)


@router.get("/incidents")
def incidents_page(request: Request, state: str = "open") -> Response:
    st = request.app.state
    if state not in ("open", "all", *_STATES):
        state = "open"
    if state == "all":
        rows = st.store.incidents()
    elif state == "open":
        rows = [i for i in st.store.incidents() if i["state"] in OPEN_INCIDENTS]
    else:
        rows = st.store.incidents(state)
    tabs = [("open", "Open"), ("manual", "Manual"), ("solved", "Solved"), ("gave_up", "Gave up"), ("all", "All")]
    return render(request, "incidents.html", active="incidents", rows=rows, state=state, tabs=tabs)


@router.get("/incidents/{iid:int}")
def incident_page(request: Request, iid: int) -> Response:
    st = request.app.state
    inc = st.store.incident(iid)
    if inc is None:
        return error_page(request, 404, f"No incident #{iid}")
    trace = st.traces.load(inc["trace_id"]) if inc["trace_id"] else None
    mt = _PLUGIN_REF.match(inc["plugin"] or "")
    return render(
        request,
        "incident.html",
        active="incidents",
        inc=inc,
        attempts=list(reversed(st.store.attempts(iid))),
        trace=trace,
        plugin_scope=mt.group("scope") if mt else None,
        can_manual=bool(inc["state"] in OPEN_INCIDENTS and inc["url"]),
        closed=inc["state"] in ("solved", "gave_up"),
    )


@router.get("/incidents/{iid:int}/attempts/{aid:int}/transcript")
def transcript(request: Request, iid: int, aid: int) -> Response:
    st = request.app.state
    att = next((a for a in st.store.attempts(iid) if a["id"] == aid), None)
    if att is None:
        return HTMLResponse('<p class="muted">Transcript not saved.</p>', status_code=404)
    data_dir = st.settings.data_dir
    raw_prompt = _read(data_dir, att["prompt_ref"])
    response = _read(data_dir, att["response_ref"])
    return render(
        request,
        "_transcript.html",
        messages=_messages(raw_prompt) if raw_prompt is not None else None,
        response=response,
    )


@router.post("/incidents/{iid:int}/retry")
def retry(request: Request, iid: int) -> Response:
    st = request.app.state
    if st.store.incident(iid) is None:
        return error_page(request, 404, f"No incident #{iid}")
    st.store.update_incident(iid, state="open", heal_window=0, next_attempt_at=time.time())
    return _redirect(iid, "Queued for healing")


@router.post("/incidents/{iid:int}/close")
def close(request: Request, iid: int, state: str = Form(...)) -> Response:
    st = request.app.state
    if state not in ("solved", "gave_up"):
        return error_page(request, 400, "State must be solved or gave_up")
    if st.store.incident(iid) is None:
        return error_page(request, 404, f"No incident #{iid}")
    st.store.update_incident(iid, state=state, next_attempt_at=None, note="closed from the dashboard")
    return _redirect(iid, f"Closed as {state.replace('_', ' ')}")


@router.post("/incidents/clear")
def clear_closed(request: Request) -> Response:
    count = request.app.state.store.clear_closed_incidents()
    return RedirectResponse(f"/ui/incidents?ok={quote(f'{count} closed incident(s) cleared')}", status_code=303)


@router.post("/incidents/{iid:int}/delete")
def delete_incident(request: Request, iid: int) -> Response:
    st = request.app.state
    if st.store.incident(iid) is None:
        return error_page(request, 404, f"No incident #{iid}")
    manual = getattr(st, "manual", None)
    busy = manual is not None and any(s.active and s.incident_id == iid for s in manual.sessions())
    if busy or not st.store.delete_incident(iid):
        return error_page(
            request, 409, f"Incident #{iid} is still open (or being solved manually); close it before deleting."
        )
    return RedirectResponse(f"/ui/incidents?ok={quote(f'Incident #{iid} deleted')}", status_code=303)


@router.get("/traces/{trace_id}")
def trace_page(request: Request, trace_id: str) -> Response:
    trace = request.app.state.traces.load(trace_id)
    if trace is None:
        return error_page(request, 404, f"No trace {trace_id}")
    return render(request, "trace.html", active="incidents", trace=trace)


@router.get("/traces/{trace_id}/screenshot.png")
def trace_screenshot(request: Request, trace_id: str) -> Response:
    trace = request.app.state.traces.load(trace_id)
    if trace is None or not trace.screenshot:
        return Response("Not found", status_code=404, media_type="text/plain")
    return Response(trace.screenshot, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/traces/{trace_id}/dom")
def trace_dom(request: Request, trace_id: str) -> Response:
    trace = request.app.state.traces.load(trace_id)
    if trace is None:
        return Response("Not found", status_code=404, media_type="text/plain")
    return HTMLResponse(
        trace.dom or "<p>empty</p>",
        headers={
            "Content-Security-Policy": _DOM_CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )
