"""Worker-side updates of the backend builds that carry a browser's user agent: the obscura binary and the
Camoufox browser build. Updated builds live under `settings.backends_dir`; an empty or broken overlay means the
versions baked into the image. Patchright's Chromium is pinned to the patchright package and not updatable here."""

import asyncio
import contextlib
import hashlib
import importlib.metadata
import logging
import os
import shutil
import socket
import tarfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from mendarr.config import Settings
from mendarr.worker import camoufox_mirror, integrity
from mendarr.healer.config import MIN_UPDATE_S, parse_duration
from mendarr.rpc import BackendsState, BackendStatus
from mendarr.worker.slots import SlotPool

log = logging.getLogger("mendarr.worker.backend_updates")

OBSCURA_ASSET = "obscura-x86_64-linux.tar.gz"
OBSCURA_DOWNLOAD_PREFIX = "https://github.com/h4ckf0r0day/obscura/releases/download/"
OBSCURA_VERSION_FILE = Path("/usr/local/share/mendarr/obscura.version")  # written by the image build
MAX_DOWNLOAD = 200 * 1024 * 1024
AUTO_FIRST_DELAY_S = 600.0
UPDATABLE = ("obscura", "camoufox")

Runner = Callable[..., Awaitable[tuple[int, str]]]


async def _run_subprocess(cmd: list[str], *, env: dict[str, str], timeout: float) -> tuple[int, str]:
    """Run `cmd`, returning (exit code, tail of the combined output); killed after `timeout` seconds."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"timed out after {timeout:.0f}s"
    return proc.returncode or 0, out.decode(errors="replace")[-2000:]


def _link_target(link: Path) -> Path | None:
    """The resolved directory a `current` link points at, None when missing or broken."""
    try:
        target = link.resolve(strict=True)
    except OSError:
        return None
    return target if target.is_dir() else None


def camoufox_overlay(settings: Settings) -> Path | None:
    """The updated Camoufox cache root (`<dir>/camoufox/current`, an XDG cache root), None = built-in.
    The build is verified against its manifest once per process (and per build)."""
    if not settings.backends_dir:
        return None
    root = _link_target(Path(settings.backends_dir) / "camoufox" / "current")
    if root is None:
        return None
    # camoufox's multiversion layout: config.json next to browsers/ in INSTALL_DIR (= <root>/camoufox)
    install = root / "camoufox"
    if not ((install / "config.json").is_file() and (install / "browsers").is_dir()):
        return None
    key = str(root)
    if key not in integrity._CHECKED:
        bad = integrity.verify_manifest(root, no_extra_exec=True)
        integrity._CHECKED[key] = bad is None
        if bad is None:
            integrity.FAILED.pop("camoufox", None)
        else:
            integrity.fail("camoufox", bad)
    return root if integrity._CHECKED[key] else None


def _switch(link: Path, target_name: str) -> None:
    """Point `link` at versions/<name> atomically."""
    tmp = link.with_name(link.name + ".tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    tmp.symlink_to(Path("versions") / target_name)
    os.replace(tmp, link)


def _prune(base: Path, keep: set[str]) -> None:
    versions = base / "versions"
    for d in versions.iterdir() if versions.is_dir() else ():
        if d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)


def _previous(base: Path) -> str | None:
    """The newest installed version that isn't the active one."""
    current = _link_target(base / "current")
    versions = base / "versions"
    others = [d for d in versions.iterdir() if d.is_dir() and d != current] if versions.is_dir() else []
    return max(others, key=lambda d: d.stat().st_mtime).name if others else None


def _short(e: BaseException) -> str:
    text = str(e).strip() or type(e).__name__
    return text.splitlines()[0][:200]


class BackendUpdater:
    def __init__(
        self,
        settings: Settings,
        pool: SlotPool,
        *,
        is_idle: Callable[[], bool],
        request_restart: Callable[[], None],
        transport: httpx.AsyncBaseTransport | None = None,
        run: Runner = _run_subprocess,
    ) -> None:
        self._settings = settings
        self._pool = pool
        self._is_idle = is_idle
        self._request_restart = request_restart
        self._transport = transport
        self._run = run
        self._base = Path(settings.backends_dir) if settings.backends_dir else None
        self._locks = {n: asyncio.Lock() for n in UPDATABLE}
        self._info: dict[str, dict] = {
            n: {"state": "idle", "error": "", "latest": "", "via": "", "checked_at": 0.0} for n in UPDATABLE
        }
        self._camoufox_installed = ""
        self._asset: dict[str, str] = {}  # obscura: url / digest of the last check
        self._task: asyncio.Task | None = None

    # -- status ------------------------------------------------------------------------------------------

    def _obscura_installed(self) -> tuple[str, bool]:
        """(version in use, True when it's the updated one)."""
        if self._base is not None and "obscura" not in integrity.FAILED:
            cur = _link_target(self._base / "obscura" / "current")
            if cur is not None and (cur / "obscura").is_file():
                with contextlib.suppress(OSError):
                    return (cur / "VERSION").read_text().strip(), True
        with contextlib.suppress(OSError):
            return OBSCURA_VERSION_FILE.read_text().strip(), False
        return "", False

    def _status(self, name: str) -> BackendStatus:
        if name == "patchright":
            ver = ""
            with contextlib.suppress(importlib.metadata.PackageNotFoundError):
                ver = importlib.metadata.version("patchright")
            return BackendStatus(
                name=name,
                component="",
                installed=ver,
                note="Chromium is pinned to the patchright package; update by rebuilding the worker image",
            )
        info = self._info[name]
        if name == "obscura":
            installed, updated = self._obscura_installed()
            component = "binary"
        else:
            installed, updated = self._camoufox_installed, camoufox_overlay(self._settings) is not None
            component = "browser build"
        return BackendStatus(
            name=name,
            component=component,
            installed=installed,
            source="updated" if updated else "built-in",
            latest=info["latest"],
            latest_via=info["via"],
            checked_at=info["checked_at"],
            state=info["state"],
            error=info["error"] or integrity.FAILED.get(name, ""),
            updatable=self._base is not None,
            can_revert=updated,
        )

    def state(self) -> BackendsState:
        s = self._settings
        interval = max(parse_duration(s.backend_update_interval), MIN_UPDATE_S) if s.backend_auto_update else 0.0
        return BackendsState(
            auto_update=s.backend_auto_update,
            interval_s=interval,
            backends=[self._status(n) for n in ("obscura", "camoufox", "patchright")],
        )

    async def refresh_installed(self) -> None:
        """Read the active Camoufox build (camoufox internals, in a thread); never raises."""
        def read() -> str:
            from camoufox.pkgman import installed_verstr

            return installed_verstr()

        try:
            self._camoufox_installed = await asyncio.to_thread(read)
        except Exception as e:
            log.info("camoufox: installed version unknown: %s", _short(e))
            self._camoufox_installed = ""

    def _newer(self, name: str) -> bool:
        st = self._status(name)
        return bool(st.latest) and st.latest != st.installed

    # -- check -------------------------------------------------------------------------------------------

    async def check(self) -> BackendsState:
        await self.refresh_installed()
        for name in UPDATABLE:
            info = self._info[name]
            if info["state"] in ("updating", "pending_restart") or self._locks[name].locked():
                continue
            info["state"] = "checking"
            try:
                info["latest"] = await (self._latest_obscura() if name == "obscura" else self._latest_camoufox())
                info["error"], info["state"] = "", "idle"
            except Exception as e:
                info["error"], info["state"] = f"check failed: {_short(e)}", "failed"
            info["checked_at"] = time.time()
        return self.state()

    async def _latest_obscura(self) -> str:
        async with httpx.AsyncClient(transport=self._transport, timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                self._settings.obscura_releases_url, headers={"Accept": "application/vnd.github+json"}
            )
            resp.raise_for_status()
            data = resp.json()
        tag = str(data.get("tag_name") or "")
        asset = next((a for a in data.get("assets") or [] if a.get("name") == OBSCURA_ASSET), None)
        if not tag or asset is None:
            raise RuntimeError(f"no {OBSCURA_ASSET} in the latest release")
        self._asset = {
            "tag": tag,
            "url": str(asset.get("browser_download_url") or ""),
            "digest": str(asset.get("digest") or ""),
        }
        return tag

    async def _latest_camoufox(self) -> str:
        def read() -> str:
            info = self._info["camoufox"]
            try:
                from camoufox.__main__ import CamoufoxUpdate

                version = CamoufoxUpdate().verstr
                info["via"] = ""
                return version
            except Exception as e:
                official = e
            mirror = self._settings.camoufox_mirror_url
            if mirror:
                try:
                    version = camoufox_mirror.latest(mirror, self._transport).version  # type: ignore[arg-type]
                except Exception as e:
                    log.info("camoufox mirror check failed: %s", _short(e))
                else:
                    info["via"] = "mirror"
                    return version
            # 2026-11-19: GitHub suspended the maintainers (daijro, JWriter20), every release repo returns 404
            if "404" in str(official):
                raise RuntimeError(
                    "Camoufox's GitHub releases are unavailable (404, maintainer accounts suspended 2026-11-19); the built-in build keeps working"
                ) from official
            raise official

        return await asyncio.to_thread(read)

    # -- update / revert ---------------------------------------------------------------------------------

    async def update(self, name: str) -> BackendStatus:
        if name not in UPDATABLE or self._base is None:
            raise KeyError(name)
        lock = self._locks[name]
        if lock.locked():
            return self._status(name)
        async with lock:
            info = self._info[name]
            if info["state"] == "pending_restart":
                return self._status(name)
            info["state"], info["error"] = "updating", ""
            try:
                if name == "obscura":
                    await self._update_obscura()
                    info["state"] = "idle"
                else:
                    await self._update_camoufox()
                    info["state"] = "pending_restart"
            except Exception as e:
                info["state"], info["error"] = "failed", _short(e)
                log.warning("%s update failed: %s", name, info["error"])
            else:
                if name == "obscura":
                    log.info("obscura updated to %s", self._obscura_installed()[0])
                else:
                    log.info("camoufox build downloaded; the worker restarts to load it")
                    self._request_restart()
        return self._status(name)

    async def _update_obscura(self) -> None:
        assert self._base is not None
        if not self._asset:
            await self._latest_obscura()
        tag, url, digest = self._asset["tag"], self._asset["url"], self._asset["digest"]
        if not url.startswith(OBSCURA_DOWNLOAD_PREFIX):
            raise RuntimeError("refusing a download URL outside the obscura GitHub releases")
        if "/" in tag or tag.startswith("."):
            raise RuntimeError("unexpected release tag")
        base = self._base / "obscura"
        staging = base / f"staging-{os.getpid()}-{int(time.time())}"
        staging.mkdir(parents=True)
        try:
            archive = staging / "a.tar.gz"
            await self._download(url, archive, digest)
            self._extract(archive, staging)
            archive.unlink()
            await self._smoke_obscura(staging / "obscura")
            (staging / "VERSION").write_text(tag + "\n")
            integrity.write_manifest(staging, tag, only=["obscura"])
            versions = base / "versions"
            versions.mkdir(exist_ok=True)
            old = _link_target(base / "current")
            dest = versions / tag
            if dest.exists():
                shutil.rmtree(dest)
            os.rename(staging, dest)
            _switch(base / "current", tag)
            _prune(base, {tag} | ({old.name} if old is not None else set()))
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self._pool.retire("obscura")

    async def _download(self, url: str, dest: Path, digest: str) -> None:
        h = hashlib.sha256()
        size = 0
        async with httpx.AsyncClient(transport=self._transport, timeout=60.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                if resp.url.scheme != "https":
                    raise RuntimeError("download left https")
                with dest.open("wb") as f:
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_DOWNLOAD:
                            raise RuntimeError("download is larger than 200 MB")
                        h.update(chunk)
                        f.write(chunk)
        if digest.startswith("sha256:") and h.hexdigest() != digest[7:].lower():
            raise RuntimeError("sha256 of the download does not match the release's digest")

    @staticmethod
    def _extract(archive: Path, staging: Path) -> None:
        with tarfile.open(archive) as tf:
            member = next((m for m in tf.getmembers() if m.isfile() and Path(m.name).name == "obscura"), None)
            if member is None:
                raise RuntimeError("no obscura binary in the archive")
            src = tf.extractfile(member)
            assert src is not None
            with src, (staging / "obscura").open("wb") as out:
                shutil.copyfileobj(src, out)
        (staging / "obscura").chmod(0o755)

    async def _smoke_obscura(self, binary: Path) -> None:
        """Start the new binary on a free port and wait for its CDP endpoint."""
        from mendarr.worker.backends import obscura

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "serve",
            "--port",
            str(port),
            "--stealth",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await obscura._wait_for_cdp(f"ws://127.0.0.1:{port}/devtools/browser", timeout=15.0)
        except Exception as e:
            raise RuntimeError(f"the new obscura did not start: {_short(e)}") from None
        finally:
            await obscura._terminate_process(proc)

    async def _update_camoufox(self) -> None:
        assert self._base is not None
        base = self._base / "camoufox"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        staging = base / f"staging-{stamp}"
        staging.mkdir(parents=True)
        try:
            env = {**os.environ, "XDG_CACHE_HOME": str(staging)}
            code, out = await self._run(["camoufox", "fetch"], env=env, timeout=900)
            # `camoufox fetch` exits 0 even when the download failed: judge by what landed in staging
            if code != 0 or not any((staging / "camoufox" / "browsers").glob("*/*")):
                mirror = self._settings.camoufox_mirror_url
                if not mirror:
                    raise RuntimeError(f"camoufox fetch failed: {_tail(out)}")
                log.info("camoufox fetch failed (%s); trying the mirror", _tail(out)[-200:])
                try:
                    build = await asyncio.to_thread(camoufox_mirror.latest, mirror, self._transport)
                    await asyncio.to_thread(camoufox_mirror.install, build, staging, self._transport)
                except Exception as e:
                    raise RuntimeError(f"camoufox fetch failed: {_tail(out)}; mirror: {_short(e)}") from e
                self._info["camoufox"]["via"] = "mirror"
            else:
                self._info["camoufox"]["via"] = ""
            code, out = await self._run(["mendarr-worker", "smoke"], env=env, timeout=180)
            if code != 0 or "Firefox" not in _tail(out):
                raise RuntimeError(f"the new Camoufox build failed its test launch: {_tail(out)}")
            await asyncio.to_thread(integrity.write_manifest, staging, stamp)
            versions = base / "versions"
            versions.mkdir(exist_ok=True)
            old = _link_target(base / "current")
            dest = versions / stamp
            if dest.exists():
                shutil.rmtree(dest)
            os.rename(staging, dest)
            _switch(base / "current", stamp)
            _prune(base, {stamp} | ({old.name} if old is not None else set()))
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    async def revert(self, name: str) -> BackendStatus:
        if name not in UPDATABLE or self._base is None:
            raise KeyError(name)
        async with self._locks[name]:
            base = self._base / name
            prev = _previous(base)
            link = base / "current"
            try:
                if prev is not None:
                    _switch(link, prev)
                else:
                    link.unlink()
            except OSError as e:
                self._info[name]["state"], self._info[name]["error"] = "failed", _short(e)
                return self._status(name)
            self._info[name]["error"] = ""
            if name == "obscura":
                self._pool.retire("obscura")
            else:
                self._info[name]["state"] = "pending_restart"
                self._request_restart()
        return self._status(name)

    # -- auto update -------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._settings.backend_auto_update and self._base is not None and self._task is None:
            self._task = asyncio.create_task(self._auto_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _auto_loop(self) -> None:
        interval = max(parse_duration(self._settings.backend_update_interval), MIN_UPDATE_S)
        delay = AUTO_FIRST_DELAY_S
        while True:
            await asyncio.sleep(delay)
            delay = interval
            try:
                await self.check()
                for name in UPDATABLE:
                    if self._newer(name) and self._info[name]["state"] in ("idle", "failed"):
                        st = await self.update(name)
                        log.info("auto update %s: %s %s", name, st.state, st.error)
            except Exception:
                log.exception("backend auto update round failed")


def _tail(out: str) -> str:
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""
