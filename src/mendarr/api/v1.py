"""FlareSolverr-compatible /v1 endpoint (plus /v1/egress)."""

import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mendarr import __version__
from mendarr.api.flaresolverr import (
    DEFAULT_MAX_TIMEOUT_MS,
    ERR_CMD_INVALID,
    ERR_CMD_MISSING,
    ERR_GET_WITH_POSTDATA,
    ERR_POSTDATA_MISSING,
    ERR_SESSION_MISSING,
    ERR_URL_MISSING,
    FLARESOLVERR_VERSION,
    MSG_SESSION_CREATED,
    MSG_SESSION_EXISTS,
    MSG_SESSION_REMOVED,
    V1Error,
    solution,
)
from mendarr.api.worker_client import WorkerUnavailable
from mendarr.core.router import V1Params
from mendarr.rpc import ProxySpec, SessionCreate

log = logging.getLogger(__name__)
router = APIRouter()

_REMOVED_PARAMS = ("headers", "userAgent", "returnRawHtml", "download")


class _Failure(V1Error):
    def __init__(self, message: str, failure: str | None) -> None:
        super().__init__(message)
        self.failure = failure


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _proxy(data: dict) -> ProxySpec | None:
    p = data.get("proxy")
    if isinstance(p, dict) and p.get("url"):
        return ProxySpec(url=str(p["url"]), username=p.get("username"), password=p.get("password"))
    return None


def _params(cmd: str, data: dict) -> V1Params:
    url = data.get("url")
    post_data = data.get("postData")
    if cmd == "request.get":
        if not url:
            raise V1Error(ERR_URL_MISSING.format(cmd=cmd))
        if post_data is not None:
            raise V1Error(ERR_GET_WITH_POSTDATA)
    else:
        if post_data is None:
            raise V1Error(ERR_POSTDATA_MISSING)
        if not url:
            raise V1Error(ERR_URL_MISSING.format(cmd=cmd))
        post_data = str(post_data)
        post_data = post_data[1:] if post_data.startswith("?") else post_data

    max_timeout = _int(data.get("maxTimeout"))
    if max_timeout is None or max_timeout < 1:
        max_timeout = DEFAULT_MAX_TIMEOUT_MS
    try:
        wait_s = max(float(data.get("waitInSeconds") or 0), 0.0)
    except (TypeError, ValueError):
        wait_s = 0.0
    cookies = data.get("cookies")
    session = data.get("session")
    return V1Params(
        url=str(url),
        method="GET" if cmd == "request.get" else "POST",
        post_data=post_data,
        max_timeout_ms=max_timeout,
        cookies=cookies if isinstance(cookies, list) else [],
        proxy=_proxy(data),
        session=session if isinstance(session, str) and session else None,
        session_ttl_minutes=_int(data.get("session_ttl_minutes")),
        return_only_cookies=bool(data.get("returnOnlyCookies")),
        return_screenshot=bool(data.get("returnScreenshot")),
        disable_media=bool(data.get("disableMedia")),
        wait_s=wait_s,
    )


async def _dispatch(data: dict, state: Any) -> dict:
    cmd = data.get("cmd")
    if cmd is None:
        raise V1Error(ERR_CMD_MISSING)
    for k in _REMOVED_PARAMS:
        if k in data:
            log.warning("Request parameter '%s' was removed in FlareSolverr v2.", k)
    worker = state.worker
    try:
        if cmd == "sessions.create":
            created = await worker.create_session(
                SessionCreate(
                    session=data.get("session") or str(uuid.uuid1()),
                    ttl_minutes=_int(data.get("session_ttl_minutes")),
                    proxy=_proxy(data),
                )
            )
            msg = MSG_SESSION_CREATED if created.fresh else MSG_SESSION_EXISTS
            return {"status": "ok", "message": msg, "session": created.session}
        if cmd == "sessions.list":
            return {"status": "ok", "message": "", "sessions": await worker.list_sessions()}
        if cmd == "sessions.destroy":
            if not await worker.destroy_session(str(data.get("session"))):
                raise V1Error(ERR_SESSION_MISSING)
            return {"status": "ok", "message": MSG_SESSION_REMOVED}
    except (WorkerUnavailable, TimeoutError) as e:
        raise V1Error(f"Worker unavailable: {e}") from e

    if cmd not in ("request.get", "request.post"):
        raise V1Error(ERR_CMD_INVALID.format(cmd=cmd))
    outcome = await state.router.request(_params(cmd, data))
    if not outcome.ok:
        raise _Failure(outcome.message, outcome.failure)
    p_only_cookies = bool(data.get("returnOnlyCookies"))
    return {
        "status": "ok",
        "message": outcome.message,
        "solution": solution(
            url=outcome.url,
            status=outcome.status,
            user_agent=outcome.user_agent,
            turnstile_token=outcome.turnstile_token,
            headers=outcome.headers,
            html=outcome.html,
            cookies=outcome.cookies,
            screenshot_b64=outcome.screenshot_b64,
            return_only_cookies=p_only_cookies,
            return_screenshot=bool(data.get("returnScreenshot")),
            return_headers=state.settings.return_headers,
        ),
    }


@router.post("/v1")
async def v1(request: Request) -> JSONResponse:
    start = int(time.time() * 1000)
    try:
        data = json.loads(await request.body() or b"{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    code = 200
    try:
        payload = await _dispatch(data, request.app.state)
    except _Failure as e:
        code, payload = 500, {"status": "error", "message": f"Error: {e}"}
        if e.failure:
            payload["mendarrFailure"] = e.failure
    except V1Error as e:
        code, payload = 500, {"status": "error", "message": f"Error: {e}"}
    except Exception as e:
        log.exception("unexpected error in /v1")
        code, payload = 500, {"status": "error", "message": f"Error: {e}"}
    payload.update(
        startTimestamp=start,
        endTimestamp=int(time.time() * 1000),
        version=FLARESOLVERR_VERSION,
        mendarrVersion=__version__,
    )
    return JSONResponse(payload, status_code=code)


@router.get("/v1/egress")
async def egress(request: Request) -> JSONResponse:
    try:
        info = await request.app.state.worker.egress()
    except (WorkerUnavailable, TimeoutError) as e:
        return JSONResponse({"status": "error", "message": f"Error: Worker unavailable: {e}"}, status_code=502)
    return JSONResponse(
        {"status": "ok", "ip": info.ip, "checkedAt": int(info.checked_at * 1000), "proxy": info.proxy}
    )
