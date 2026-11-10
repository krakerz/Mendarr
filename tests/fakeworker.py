import time

from mendarr.rpc import (
    CheckRequest,
    CheckResult,
    Cookie,
    EgressInfo,
    SessionCreate,
    SessionCreated,
    Snapshot,
    SolveRequest,
    SolveResult,
)

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0"


def snapshot(
    url="http://example.com/", status=200, title="Hello", html="<title>Hello</title>", headers=None, cookies=None
) -> Snapshot:
    return Snapshot(url=url, status=status, headers=headers or {}, title=title, html=html, cookies=cookies or [])


def plain_result(url, html="<title>Hello</title>") -> SolveResult:
    return SolveResult(
        user_agent=UA,
        egress="203.0.113.7",
        snapshot=snapshot(url, html=html, cookies=[Cookie(name="sid", value="1", domain="example.com")]),
    )


def clearance_cookie(domain="example.com") -> Cookie:
    return Cookie(name="cf_clearance", value="tok", domain=domain, expires=time.time() + 3600)


def challenge_result(url) -> SolveResult:
    return SolveResult(
        challenge_seen=True,
        clicks=1,
        user_agent=UA,
        egress="203.0.113.7",
        snapshot=snapshot(url, cookies=[clearance_cookie()]),
    )


class FakeWorker:
    def __init__(self) -> None:
        self.ua = UA
        self.egress_ip = "203.0.113.7"
        self.solve_results: list[SolveResult] = []
        self.check_results: list[CheckResult] = []
        self.sessions: dict[str, SessionCreate] = {}
        self.calls: list[tuple[str, object]] = []
        self.raise_on_solve: Exception | None = None

    async def user_agent(self) -> str:
        return self.ua

    async def solve(self, req: SolveRequest) -> SolveResult:
        self.calls.append(("solve", req))
        if self.raise_on_solve is not None:
            raise self.raise_on_solve
        return self.solve_results.pop(0) if self.solve_results else plain_result(req.url)

    async def check(self, req: CheckRequest) -> CheckResult:
        self.calls.append(("check", req))
        if self.check_results:
            return self.check_results.pop(0)
        return CheckResult(snapshot=snapshot(req.url))

    async def create_session(self, req: SessionCreate) -> SessionCreated:
        self.calls.append(("create_session", req))
        sid = req.session or "generated"
        fresh = sid not in self.sessions
        self.sessions.setdefault(sid, req)
        return SessionCreated(session=sid, fresh=fresh)

    async def list_sessions(self) -> list[str]:
        return list(self.sessions)

    async def destroy_session(self, session_id: str) -> bool:
        return self.sessions.pop(session_id, None) is not None

    async def egress(self) -> EgressInfo:
        return EgressInfo(ip=self.egress_ip, checked_at=1700000000.0)

    async def aclose(self) -> None:
        pass

    def calls_of(self, name: str) -> list:
        return [r for n, r in self.calls if n == name]
