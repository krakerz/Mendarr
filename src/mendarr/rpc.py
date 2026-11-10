"""API <-> worker wire models. Internal; the FlareSolverr surface lives in api/flaresolverr.py."""

from typing import Literal

from pydantic import BaseModel, Field


class Cookie(BaseModel):
    name: str
    value: str
    domain: str = ""
    path: str = "/"
    expires: float = -1  # -1 = session cookie (Playwright convention)
    httpOnly: bool = False
    secure: bool = False
    sameSite: str = "Lax"


class ProxySpec(BaseModel):
    url: str
    username: str | None = None
    password: str | None = None


class Snapshot(BaseModel):
    """Page state taken by the executor after the plugin returned."""

    url: str
    status: int = 200
    headers: dict[str, str] = Field(default_factory=dict)  # names lower-cased
    title: str = ""
    html: str = ""
    cookies: list[Cookie] = Field(default_factory=list)


class PluginSpec(BaseModel):
    """One plugin version, shipped API -> worker (the worker has no store)."""

    scope: str
    version: int
    source: str
    sha256: str

    @property
    def ref(self) -> str:
        return f"{self.scope}@v{self.version}"


class WorkerError(BaseModel):
    type: Literal["navigation", "deadline", "no_slot", "plugin", "guard", "internal"]
    detail: str = ""


class TraceData(BaseModel):
    """Failure evidence the worker captures for the healer (only when asked and the run looks failed)."""

    screenshot_b64: str | None = None  # PNG of the viewport
    dom: str = ""  # trimmed DOM of the page, open shadow roots inlined, child frames appended
    frames: list[dict] = Field(default_factory=list)  # {url, name, parent}
    console: list[str] = Field(default_factory=list)
    network: list[dict] = Field(default_factory=list)  # {url, status, type, cf, failure?}
    steps: list[str] = Field(default_factory=list)  # ctx.log lines of the plugin


class SolveRequest(BaseModel):
    method: Literal["GET", "POST"] = "GET"
    url: str
    post_data: str | None = None
    cookies: list[dict] = Field(default_factory=list)  # FlareSolverr request cookies, as sent
    proxy: ProxySpec | None = None
    session: str | None = None
    session_ttl_minutes: int | None = None
    timeout_s: float
    disable_media: bool = False
    wait_s: float = 0
    screenshot: bool = False
    plugin: PluginSpec | None = None  # None = the worker's builtin _default
    trace: bool = False  # attach TraceData when the run fails or ends on a challenge/block page


class SolveResult(BaseModel):
    challenge_seen: bool = False
    clicks: int = 0
    plugin: str = "_default@builtin"
    user_agent: str = ""
    egress: str = "default"
    snapshot: Snapshot | None = None
    screenshot_b64: str | None = None
    turnstile_token: str | None = None
    error: WorkerError | None = None
    trace: TraceData | None = None


class ManualStart(BaseModel):
    url: str
    proxy: ProxySpec | None = None
    timeout_s: float = 600  # the worker closes the session on its own after this (+ a grace period)
    slot_wait_s: float = 30


class ManualStarted(BaseModel):
    id: str
    slot: int
    user_agent: str = ""
    egress: str = "default"


class ManualState(BaseModel):
    id: str
    status: Literal["running", "closed"]  # closed = the user closed every tab
    challenge_seen: bool = False
    snapshot: Snapshot | None = None  # None while the page is navigating
    events: int = 0  # demo events recorded so far


class ManualResult(BaseModel):
    challenge_seen: bool = False
    snapshot: Snapshot | None = None
    user_agent: str = ""
    egress: str = "default"
    demo: dict = Field(default_factory=dict)  # see worker/demo.py DemoRecorder.summary
    trace: TraceData | None = None  # steps = the demo as readable lines, plus screenshot/DOM of the end state


class CheckRequest(BaseModel):
    method: Literal["GET", "POST"] = "GET"
    url: str
    post_data: str | None = None
    cookies: list[Cookie]
    user_agent: str
    proxy: ProxySpec | None = None
    timeout_s: float = 15


class CheckResult(BaseModel):
    snapshot: Snapshot | None = None
    error: WorkerError | None = None
    mode: Literal["curl", "browser"] = "curl"


class SessionCreate(BaseModel):
    session: str | None = None
    ttl_minutes: int | None = None
    proxy: ProxySpec | None = None


class SessionCreated(BaseModel):
    session: str
    fresh: bool


class SessionList(BaseModel):
    sessions: list[str]


class EgressInfo(BaseModel):
    ip: str  # "default" when no check URL is set, "unknown" when the lookup failed
    checked_at: float = 0
    proxy: str | None = None  # upstream proxy as scheme://host:port; None = direct


class WorkerInfo(BaseModel):
    user_agent: str = ""
    slots: int = 0
    version: str = ""
    cookie_check: Literal["curl", "browser"] = "curl"
    impersonate: str | None = None
    proxy: str | None = None  # upstream proxy as scheme://host:port; None = direct
