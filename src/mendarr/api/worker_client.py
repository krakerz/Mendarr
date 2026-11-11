"""API -> worker HTTP client. Endpoints mirror worker/server.py."""

from typing import Protocol

import httpx

from mendarr.rpc import (
    CheckRequest,
    CheckResult,
    EgressInfo,
    ManualResult,
    ManualAutoSolve,
    ManualAutoSolved,
    ManualStart,
    ManualStarted,
    ManualState,
    SessionCreate,
    SessionCreated,
    SessionList,
    SolveRequest,
    SolveResult,
    WorkerInfo,
)


class WorkerUnavailable(Exception):
    pass


class WorkerRejected(WorkerUnavailable):
    """The worker answered with an error status (404 = unknown manual session, 409 = no free slot)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class WorkerClient(Protocol):
    async def user_agent(self) -> str: ...  # "" when the worker can't be reached (never raises)
    async def solve(self, req: SolveRequest) -> SolveResult: ...
    async def check(self, req: CheckRequest) -> CheckResult: ...
    async def create_session(self, req: SessionCreate) -> SessionCreated: ...
    async def list_sessions(self) -> list[str]: ...
    async def destroy_session(self, session_id: str) -> bool: ...
    async def egress(self) -> EgressInfo: ...
    async def manual_start(self, req: ManualStart) -> ManualStarted: ...
    async def manual_state(self, sid: str) -> ManualState: ...
    async def manual_finish(self, sid: str) -> ManualResult: ...
    async def manual_cancel(self, sid: str) -> bool: ...
    async def manual_autosolve(self, sid: str, req: ManualAutoSolve) -> ManualAutoSolved: ...
    async def aclose(self) -> None: ...


class HttpWorkerClient:
    def __init__(self, base_url: str, token: str = "") -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.AsyncClient(base_url=base_url, timeout=10.0, headers=headers)

    async def _call(self, method: str, path: str, *, timeout: float | None = None, json: dict | None = None) -> dict:
        kwargs: dict = {"json": json}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            r = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as e:
            raise TimeoutError(str(e)) from e
        except httpx.TransportError as e:
            raise WorkerUnavailable(str(e) or type(e).__name__) from e
        if not r.is_success:
            raise WorkerRejected(r.status_code, f"worker answered {r.status_code} on {method} {path}")
        try:
            return r.json()
        except ValueError as e:
            raise WorkerUnavailable(f"worker sent invalid JSON on {method} {path}") from e

    async def user_agent(self) -> str:
        try:
            return WorkerInfo.model_validate(await self._call("GET", "/info", timeout=3.0)).user_agent
        except (WorkerUnavailable, TimeoutError, ValueError):
            return ""

    async def solve(self, req: SolveRequest) -> SolveResult:
        data = await self._call("POST", "/solve", timeout=req.timeout_s + 10, json=req.model_dump())
        return SolveResult.model_validate(data)

    async def check(self, req: CheckRequest) -> CheckResult:
        data = await self._call("POST", "/check", timeout=req.timeout_s + 10, json=req.model_dump())
        return CheckResult.model_validate(data)

    async def create_session(self, req: SessionCreate) -> SessionCreated:
        return SessionCreated.model_validate(await self._call("POST", "/sessions", json=req.model_dump()))

    async def list_sessions(self) -> list[str]:
        return SessionList.model_validate(await self._call("GET", "/sessions")).sessions

    async def destroy_session(self, session_id: str) -> bool:
        data = await self._call("DELETE", f"/sessions/{session_id}")
        return bool(data.get("existed"))

    async def egress(self) -> EgressInfo:
        return EgressInfo.model_validate(await self._call("GET", "/egress"))

    async def manual_start(self, req: ManualStart) -> ManualStarted:
        data = await self._call("POST", "/manual", timeout=req.slot_wait_s + 30, json=req.model_dump())
        return ManualStarted.model_validate(data)

    async def manual_state(self, sid: str) -> ManualState:
        return ManualState.model_validate(await self._call("GET", f"/manual/{sid}", timeout=15))

    async def manual_finish(self, sid: str) -> ManualResult:
        return ManualResult.model_validate(await self._call("POST", f"/manual/{sid}/finish", timeout=30))

    async def manual_autosolve(self, sid: str, req: ManualAutoSolve) -> ManualAutoSolved:
        data = await self._call("POST", f"/manual/{sid}/autosolve", timeout=req.timeout_s + 15, json=req.model_dump())
        return ManualAutoSolved.model_validate(data)

    async def manual_cancel(self, sid: str) -> bool:
        data = await self._call("DELETE", f"/manual/{sid}")
        return bool(data.get("existed"))

    async def aclose(self) -> None:
        await self._http.aclose()
