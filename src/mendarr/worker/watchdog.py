"""Hard stop for plugin code the asyncio deadline can't interrupt (busy loops, swallowed cancellation)."""

import asyncio
import contextlib
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator

log = logging.getLogger("mendarr.worker.watchdog")

EXIT_CODE = 70  # compose restarts the worker


class PluginStalled(Exception):
    pass


class Watchdog:
    def __init__(
        self,
        stall_s: float,
        kill_s: float,
        *,
        exit_fn: Callable[[int], object] = os._exit,
        interval: float = 0.1,
    ) -> None:
        self.stall_s = stall_s
        self.kill_s = kill_s
        self._exit = exit_fn
        self._interval = interval
        self._loop: asyncio.AbstractEventLoop | None = None
        self._beat = time.monotonic()
        self._running: dict[asyncio.Task, float] = {}  # plugin task -> request deadline (monotonic)
        self._signalled = False
        self._can_signal = False
        self._stop = threading.Event()
        self._heartbeat: asyncio.Task | None = None

    def start(self) -> None:
        """Call from inside the running loop."""
        self._loop = asyncio.get_running_loop()
        self._beat = time.monotonic()
        self._heartbeat = self._loop.create_task(self._beat_forever())
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGUSR1, self._on_signal)
            self._can_signal = True
        else:
            log.info("watchdog off the main thread: a stalled plugin can only be stopped by exiting")
        self._stop.clear()
        threading.Thread(target=self._watch, name="plugin-watchdog", daemon=True).start()

    async def stop(self) -> None:
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat
        if self._can_signal:
            signal.signal(signal.SIGUSR1, signal.SIG_DFL)
            self._can_signal = False

    @contextlib.contextmanager
    def plugin_running(self, deadline: float) -> Iterator[None]:
        task = asyncio.current_task()
        if task is None:
            yield
            return
        self._running[task] = deadline
        try:
            yield
        finally:
            self._running.pop(task, None)

    async def _beat_forever(self) -> None:
        while True:
            self._beat = time.monotonic()
            await asyncio.sleep(self._interval)

    def _watch(self) -> None:
        while not self._stop.wait(self._interval):
            running = dict(self._running)
            if not running:
                self._signalled = False
                continue
            now = time.monotonic()
            stalled = now - self._beat
            if stalled > self.kill_s or any(now > d + self.kill_s for d in running.values()):
                log.critical("plugin did not stop (loop stalled %.1fs); exiting so the worker restarts", stalled)
                self._exit(EXIT_CODE)
                return
            if stalled > self.stall_s and self._can_signal and not self._signalled:
                self._signalled = True
                signal.pthread_kill(threading.main_thread().ident, signal.SIGUSR1)
            elif stalled <= self.stall_s:
                self._signalled = False

    def _on_signal(self, signum: int, frame: object) -> None:
        # runs on the main thread between bytecodes, i.e. inside whatever is blocking the loop
        task = asyncio.current_task(self._loop) if self._loop is not None else None
        if task is not None and task in self._running:
            raise PluginStalled(f"plugin blocked the event loop for over {self.stall_s:g}s")
