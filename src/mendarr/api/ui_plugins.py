"""Dashboard: plugin versions, diff, promote/rollback/reject, edit-and-test (ephemeral replay) and submit."""

import difflib
import hashlib
import time
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response

from mendarr.api.auth import require_user
from mendarr.core.domains import registrable_domain
from mendarr.plugins import guard
from mendarr.plugins.guard import GuardError
from mendarr.plugins.store import DEFAULT_SCOPE, PluginRecord, StoreError, normalize_scope
from mendarr.rpc import PluginSpec
from mendarr.web import error_page, render

router = APIRouter(dependencies=[Depends(require_user)])
_MAX_SOURCE = 200_000


def _back(scope: str, key: str, msg: str) -> RedirectResponse:
    return RedirectResponse(f"/ui/plugins/{quote(scope, safe='')}?{key}={quote(msg)}", status_code=303)


def _diff_lines(a: PluginRecord, b: PluginRecord) -> list[tuple[str, str]]:
    out = []
    for line in difflib.unified_diff(
        a.source.splitlines(), b.source.splitlines(), a.ref, b.ref, lineterm="", n=3
    ):
        if line.startswith(("+++", "---", "@@")):
            cls = "hunk"
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        else:
            cls = "ctx"
        out.append((cls, line))
    return out


def _default_url(request: Request, scope: str) -> str:
    if scope == DEFAULT_SCOPE:
        return ""
    trace = request.app.state.traces.latest(scope)
    if trace is not None and trace.meta.get("url"):
        return str(trace.meta["url"])
    return f"https://{scope}/"


def _edit_page(
    request: Request,
    scope: str,
    source: str,
    url: str,
    notes: str = "",
    error: str = "",
    violations: list[str] | None = None,
    from_ref: str = "",
    status_code: int = 200,
) -> Response:
    return render(
        request,
        "plugin_edit.html",
        status_code=status_code,
        active="plugins",
        scope=scope,
        source=source,
        url=url,
        notes=notes,
        error=error,
        violations=violations or [],
        from_ref=from_ref,
    )


def _scope_or_none(scope: str) -> str | None:
    try:
        return normalize_scope(scope)
    except StoreError:
        return None


def _bad_scope(request: Request, scope: str) -> Response:
    return error_page(request, 404, f"Unknown plugin scope {scope!r}")


@router.get("/plugins")
def plugins_index(request: Request) -> Response:
    store = request.app.state.store
    by_scope: dict[str, list[PluginRecord]] = {}
    for p in store.list():
        by_scope.setdefault(p.scope, []).append(p)
    by_scope.setdefault(DEFAULT_SCOPE, [])
    rows = []
    for scope in sorted(by_scope, key=lambda s: (s != DEFAULT_SCOPE, s)):
        recs = by_scope[scope]
        active = next((p for p in recs if p.status == "active"), None)
        rows.append(
            {
                "scope": scope,
                "active": active,
                "versions": len(recs),
                "candidates": sum(1 for p in recs if p.status == "candidate"),
                "newest": max((p.created for p in recs), default=None),
            }
        )
    return render(request, "plugins.html", active="plugins", rows=rows)


@router.get("/plugins/new")
def plugins_new(scope: str = Query("")) -> Response:
    try:
        s = normalize_scope(scope)
    except StoreError as e:
        return RedirectResponse(f"/ui/plugins?err={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/ui/plugins/{quote(s, safe='')}/edit", status_code=303)


@router.get("/plugins/{scope}")
def plugin_scope(request: Request, scope: str) -> Response:
    s = _scope_or_none(scope)
    if s is None:
        return _bad_scope(request, scope)
    recs = sorted(request.app.state.store.list(s), key=lambda p: p.version, reverse=True)
    active = next((p for p in recs if p.status == "active"), None)
    return render(
        request,
        "plugin_scope.html",
        active="plugins",
        scope=s,
        recs=recs,
        can_rollback=active is not None and active.author != "builtin",
        versions=sorted(p.version for p in recs),
    )


@router.get("/plugins/{scope}/diff")
def plugin_diff(request: Request, scope: str, a: int = Query(...), b: int = Query(...)) -> Response:
    s = _scope_or_none(scope)
    if s is None:
        return _bad_scope(request, scope)
    store = request.app.state.store
    try:
        ra, rb = store.get(s, a), store.get(s, b)
    except StoreError as e:
        return error_page(request, 404, str(e))
    return render(request, "plugin_diff.html", active="plugins", scope=s, a=ra, b=rb, lines=_diff_lines(ra, rb))


@router.get("/plugins/{scope}/edit")
def plugin_edit(request: Request, scope: str, from_: int | None = Query(None, alias="from")) -> Response:
    s = _scope_or_none(scope)
    if s is None:
        return _bad_scope(request, scope)
    store = request.app.state.store
    if from_ is not None:
        try:
            rec = store.get(s, from_)
        except StoreError as e:
            return error_page(request, 404, str(e))
    else:
        rec = store.active(s) or store.resolve(s)
    return _edit_page(request, s, rec.source, _default_url(request, s), from_ref=rec.ref)


@router.get("/plugins/{scope}/v{version:int}")
def plugin_version(request: Request, scope: str, version: int) -> Response:
    s = _scope_or_none(scope)
    if s is None:
        return _bad_scope(request, scope)
    try:
        rec = request.app.state.store.get(s, version)
    except StoreError as e:
        return error_page(request, 404, str(e))
    return render(request, "plugin_version.html", active="plugins", scope=s, rec=rec)


@router.post("/plugins/{scope}/promote")
def plugin_promote(request: Request, scope: str, version: int = Form()) -> Response:
    s = _scope_or_none(scope)
    if s is None:
        return _bad_scope(request, scope)
    try:
        rec = request.app.state.store.promote(s, version)
    except StoreError as e:
        return _back(s, "err", str(e))
    return _back(s, "ok", f"Promoted {rec.ref}")


@router.post("/plugins/{scope}/reject")
def plugin_reject(request: Request, scope: str, version: int = Form()) -> Response:
    s = _scope_or_none(scope)
    if s is None:
        return _bad_scope(request, scope)
    try:
        rec = request.app.state.store.reject(s, version)
    except StoreError as e:
        return _back(s, "err", str(e))
    return _back(s, "ok", f"Rejected {rec.ref}")


@router.post("/plugins/{scope}/rollback")
def plugin_rollback(request: Request, scope: str) -> Response:
    s = _scope_or_none(scope)
    if s is None:
        return _bad_scope(request, scope)
    try:
        rec = request.app.state.store.rollback(s)
    except StoreError as e:
        return _back(s, "err", str(e))
    if rec is None:
        return _back(s, "ok", f"{s} now falls back to {DEFAULT_SCOPE}")
    return _back(s, "ok", f"Rolled back to {rec.ref}")


@router.post("/plugins/{scope}/test")
async def plugin_test(request: Request, scope: str, source: str = Form(), url: str = Form("")) -> Response:
    s = _scope_or_none(scope)
    if s is None:
        return _bad_scope(request, scope)

    def fragment(**ctx: object) -> Response:
        return render(request, "_replay_result.html", **ctx)

    if len(source) > _MAX_SOURCE:
        return fragment(error="Source is too large (limit 200000 characters)")
    try:
        guard.check(source)
    except GuardError as e:
        return fragment(error="Guard rejected it", violations=e.violations)
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return fragment(error="Enter an http(s) URL with a host to test against")
    url = url.strip()
    st = request.app.state
    timeout_s = st.config_file.get().healer.replay_timeout_s
    spec = PluginSpec(scope=s, version=0, source=source, sha256=hashlib.sha256(source.encode()).hexdigest())
    t0 = time.monotonic()
    out = await st.router.replay(url, spec, max_timeout_ms=int(timeout_s * 1000))
    elapsed = time.monotonic() - t0
    tid = None
    if not out.ok:
        tid = st.traces.save(
            domain=registrable_domain(url),
            url=url,
            kind="replay",
            result=out.result,
            failure=out.failure or "",
            detail=out.message,
            plugin=f"{s}@edit",
        )
    return fragment(out=out, elapsed=elapsed, trace_id=tid)


@router.post("/plugins/{scope}/submit")
def plugin_submit(
    request: Request,
    scope: str,
    source: str = Form(),
    notes: str = Form(""),
    promote: str | None = Form(None),
    url: str = Form(""),
) -> Response:
    s = _scope_or_none(scope)
    if s is None:
        return _bad_scope(request, scope)
    store = request.app.state.store
    try:
        rec = store.submit(s, source, author="human", notes=notes or None)
        if promote:
            store.promote(s, rec.version)
    except GuardError as e:
        return _edit_page(
            request, s, source, url, notes, error="Guard rejected it", violations=e.violations, status_code=400
        )
    except StoreError as e:
        return _edit_page(request, s, source, url, notes, error=str(e), status_code=400)
    tail = "and made it active" if promote else "as candidate"
    return _back(s, "ok", f"Stored {rec.ref} {tail}")
