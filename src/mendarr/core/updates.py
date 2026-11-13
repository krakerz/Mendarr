"""Update checker: asks GitHub for the latest published release now and then and remembers what it found.

State lives in <data_dir>/updates.json (a handful of fields, no schema to migrate; the cookie/plugin
store is a different lifecycle) so a restart keeps "last checked" and does not notify twice.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import httpx

from mendarr import __version__
from mendarr.api.worker_client import WorkerClient
from mendarr.config import Settings
from mendarr.healer.config import MIN_UPDATE_S, ConfigFile, update_policy
from mendarr.healer.notify import Notice, Notifier

log = logging.getLogger(__name__)

_VERSION = re.compile(r"^\s*v?(\d+(?:\.\d+)*)(.*)$", re.IGNORECASE)


def version_key(v: str) -> tuple[tuple[int, ...], int, str] | None:
    """Sortable key: numeric parts (trailing zeros ignored), release > any suffix (rc, beta, dev), suffix text.

    None = not a version at all.
    """
    m = _VERSION.match(v or "")
    if not m:
        return None
    nums = [int(x) for x in m.group(1).split(".")]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    suffix = m.group(2).strip().lower()
    return tuple(nums), 0 if suffix else 1, suffix


def is_newer(latest: str, current: str) -> bool:
    a, b = version_key(latest), version_key(current)
    return a is not None and b is not None and a > b


@dataclass
class UpdateState:
    last_check_at: float = 0.0
    latest: str = ""
    url: str = ""
    published_at: str = ""
    error: str = ""  # of the last check; "" = it worked
    etag: str = ""
    notified: str = ""  # last version a notification went out for


@dataclass(frozen=True)
class UpdateView:
    enabled: bool
    status: str  # never | current | update | error
    current: str
    latest: str
    url: str
    published_at: str
    last_check_at: float
    error: str
    worker_version: str  # "" = unknown (worker unreachable or not asked yet)
    worker_mismatch: bool

    @property
    def update_available(self) -> bool:
        return self.latest != "" and is_newer(self.latest, self.current)


class UpdateChecker:
    POLL_S = 15.0
    FIRST_CHECK_S = 60.0  # after startup or enabling: not at once, the worker may still be warming up
    WORKER_TTL_S = 300.0
    TIMEOUT_S = 10.0

    def __init__(
        self,
        settings: Settings,
        config: ConfigFile,
        worker: WorkerClient,
        notifier: Notifier | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._config = config
        self._worker = worker
        self._notifier = notifier
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._path = Path(settings.data_dir) / "updates.json"
        self._state = self._load()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._last_logged_error = ""
        self._logged_clamp: float | None = None
        self._worker_version = ""
        self._worker_at = 0.0
        self._next_at: float | None = None
        self._interval = 0.0

    # --- state file ---

    def _load(self) -> UpdateState:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(UpdateState)}
            return UpdateState(**{k: v for k, v in data.items() if k in known})
        except FileNotFoundError:
            return UpdateState()
        except (OSError, ValueError, TypeError) as e:
            log.warning("ignoring unreadable %s: %s", self._path, e)
            return UpdateState()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f".{self._path.name}.tmp")
            tmp.write_text(json.dumps(asdict(self._state)), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            log.warning("could not store the update state: %s", e)

    # --- lifecycle ---

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="update-checker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self._sleep(await self.tick())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("update check failed")
                await self._sleep(self.POLL_S)

    async def tick(self) -> float:
        """One loop pass: re-reads the policy, checks when due; returns how long to sleep."""
        enabled, interval_s, raw = update_policy(self._settings, self._config.get())
        if enabled and raw < MIN_UPDATE_S and self._logged_clamp != raw:
            log.warning("update check interval %ss is below the minimum; using %ss", int(raw), int(MIN_UPDATE_S))
        self._logged_clamp = raw if raw < MIN_UPDATE_S else None
        now = self._clock()
        if not enabled:
            self._next_at = None
            self._interval = 0.0
            return self.POLL_S
        if self._next_at is None or self._interval != interval_s:
            self._next_at = now + min(self.FIRST_CHECK_S, interval_s)
            self._interval = interval_s
        if now >= self._next_at:
            await self.check_now()
            self._next_at = self._clock() + interval_s
        return max(0.5, min(self.POLL_S, self._next_at - self._clock()))

    # --- the check ---

    def _enabled(self) -> bool:
        return update_policy(self._settings, self._config.get())[0]

    async def check_now(self) -> UpdateView:
        """Ask GitHub now (also for the Settings button). Never raises; off = no request."""
        if not self._enabled():
            return self.snapshot()
        async with self._lock:
            await self._check()
            await self.refresh_worker(force=True)
        return self.snapshot()

    async def _check(self) -> None:
        s = self._state
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"mendarr/{__version__}",
        }
        if s.etag and s.latest:
            headers["If-None-Match"] = s.etag
        error = ""
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self.TIMEOUT_S) as http:
                r = await http.get(self._settings.update_releases_url, headers=headers)
            if r.status_code == 304:
                pass
            elif r.status_code == 403 or r.status_code == 429:
                error = f"GitHub refused the request (HTTP {r.status_code}, rate limit?)"
            elif r.status_code == 404:
                error = "no published release found (HTTP 404)"
            elif not r.is_success:
                error = f"GitHub answered HTTP {r.status_code}"
            else:
                error = self._parse(r)
        except httpx.HTTPError as e:
            error = f"{type(e).__name__}: {e}"[:200]
        s.last_check_at = self._clock()
        s.error = error
        if error:
            if error != self._last_logged_error:
                log.warning("update check failed: %s", error)
            self._last_logged_error = error
        else:
            self._last_logged_error = ""
            log.info("update check: latest release %s (this is %s)", s.latest, __version__)
        self._save()
        if not error and s.latest and is_newer(s.latest, __version__):
            await self._notify()

    def _parse(self, r: httpx.Response) -> str:
        try:
            data = r.json()
            tag = str(data["tag_name"]).strip()
        except (ValueError, KeyError, TypeError):
            return "GitHub sent an unexpected answer"
        latest = tag[1:] if tag[:1] in "vV" else tag
        if version_key(latest) is None:
            return f"unrecognised release tag {tag!r}"
        s = self._state
        s.latest = latest
        s.url = str(data.get("html_url") or "")
        s.published_at = str(data.get("published_at") or "")
        s.etag = r.headers.get("etag", "")
        return ""

    async def _notify(self) -> None:
        s = self._state
        if self._notifier is None or s.notified == s.latest:
            return
        notice = Notice(
            "update_available",
            "Mendarr",
            f"Mendarr {s.latest} is available (this is {__version__}).\n{s.url}".strip(),
            url=s.url,
        )
        res = await self._notifier.send(self._config.get().notify, notice)
        if any(res.values()):  # event off or no channel: try again at the next check
            s.notified = s.latest
            self._save()

    # --- worker version ---

    async def refresh_worker(self, *, force: bool = False) -> None:
        now = self._clock()
        if not force and self._worker_at and now - self._worker_at < self.WORKER_TTL_S:
            return
        self._worker_at = now
        try:
            self._worker_version = (await self._worker.info()).version
        except Exception as e:  # unreachable, old worker without /info, ...
            log.debug("worker version unknown: %s: %s", type(e).__name__, e)
            self._worker_version = ""

    def snapshot(self) -> UpdateView:
        s = self._state
        if s.error:
            status = "error"
        elif not s.latest:
            status = "never" if not s.last_check_at else "error"
        elif is_newer(s.latest, __version__):
            status = "update"
        else:
            status = "current"
        wv = self._worker_version
        return UpdateView(
            enabled=self._enabled(),
            status=status,
            current=__version__,
            latest=s.latest,
            url=s.url,
            published_at=s.published_at,
            last_check_at=s.last_check_at,
            error=s.error,
            worker_version=wv,
            worker_mismatch=bool(wv) and wv != __version__,
        )

    async def view(self) -> UpdateView:
        """Snapshot for the dashboard: reads stored state; the worker is asked at most every 5 minutes."""
        await self.refresh_worker()
        return self.snapshot()
