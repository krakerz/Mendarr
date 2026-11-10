"""FlareSolverr /v1 wire format. Strings and shapes follow FlareSolverr 3.x (MIT,
github.com/FlareSolverr/FlareSolverr: src/flaresolverr_service.py, src/dtos.py, src/tests.py)."""

from mendarr.core.classify import FailureKind
from mendarr.rpc import Cookie

# Clients (Prowlarr, Jackett) sometimes gate on the FlareSolverr version, so we report one they accept.
FLARESOLVERR_VERSION = "3.3.21"
DEFAULT_MAX_TIMEOUT_MS = 60000

MSG_SOLVED = "Challenge solved!"
MSG_NOT_DETECTED = "Challenge not detected!"
MSG_SESSION_CREATED = "Session created successfully."
MSG_SESSION_EXISTS = "Session already exists."
MSG_SESSION_REMOVED = "The session has been removed."

ERR_CMD_MISSING = "Request parameter 'cmd' is mandatory."
ERR_CMD_INVALID = "Request parameter 'cmd' = '{cmd}' is invalid."
ERR_URL_MISSING = "Request parameter 'url' is mandatory in '{cmd}' command."
ERR_GET_WITH_POSTDATA = "Cannot use 'postBody' when sending a GET request."
ERR_POSTDATA_MISSING = "Request parameter 'postData' is mandatory in 'request.post' command."
ERR_SESSION_MISSING = "The session doesn't exist."
ERR_TIMEOUT = "Error solving the challenge. Timeout after {seconds} seconds."
ERR_BLOCKED = (
    "Error solving the challenge. Cloudflare has blocked this request. "
    "Probably your IP is banned for this site, check in your web browser."
)
ERR_GENERIC = "Error solving the challenge. {detail}"

_DROP_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"}


class V1Error(Exception):
    """Rendered as HTTP 500 with message 'Error: <text>'."""


def failure_message(kind: FailureKind, detail: str, max_timeout_ms: int) -> str:
    if kind in ("timeout", "challenge_loop"):
        return ERR_TIMEOUT.format(seconds=max_timeout_ms / 1000)
    if kind == "blocked":
        return ERR_BLOCKED
    return ERR_GENERIC.format(detail=(detail or "Unknown error").replace("\n", "\\n"))


def fs_cookie(c: Cookie) -> dict:
    d = {
        "name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
        "expires": c.expires, "size": len(c.name) + len(c.value), "httpOnly": c.httpOnly,
        "secure": c.secure, "session": c.expires < 0, "sameSite": c.sameSite,
    }
    if c.expires >= 0:
        d["expiry"] = int(c.expires)  # Selenium-era FlareSolverr name
    return d


def solution(
    *, url: str, status: int, user_agent: str, turnstile_token: str | None, headers: dict[str, str],
    html: str, cookies: list[Cookie], screenshot_b64: str | None,
    return_only_cookies: bool, return_screenshot: bool, return_headers: bool,
) -> dict:
    # key order = FlareSolverr's attribute assignment order in _evil_logic
    sol: dict = {"url": url, "status": status, "userAgent": user_agent, "turnstile_token": turnstile_token}
    if not return_only_cookies:
        sol["headers"] = {k: v for k, v in headers.items() if k not in _DROP_HEADERS} if return_headers else {}
        sol["response"] = html
    if return_screenshot:
        sol["screenshot"] = screenshot_b64
    sol["cookies"] = [fs_cookie(c) for c in cookies]
    return sol
