import logging
import subprocess

log = logging.getLogger(__name__)

SCREEN = "1366x768x24"
VNC_BASE_PORT = 5900
VNC_NOFILE = 1024  # x11vnc needs a handful of fds: the X connection, the listener and a few clients


def vnc_port(slot: int) -> int:
    return VNC_BASE_PORT + slot


class SlotDisplay:
    """Xvfb + x11vnc (localhost only) for one slot, supervised as subprocesses; server.py bridges VNC over WS."""

    def __init__(self, slot: int) -> None:
        self.slot = slot
        self.procs: dict[str, subprocess.Popen] = {}

    def start(self) -> None:
        i = self.slot
        self.procs["xvfb"] = subprocess.Popen(["Xvfb", f":{i}", "-screen", "0", SCREEN, "-nolisten", "tcp"])
        # x11vnc exits at once if the X socket isn't up yet; a retry loop is cheaper than polling the socket.
        # localhost only (IPv4, no IPv6 listener, no reverse lookups): the worker's /vnc bridge is the only client.
        # ulimit: x11vnc walks every possible fd, so a huge inherited RLIMIT_NOFILE (Docker's LimitNOFILE=infinity is
        # ~1e9) makes each pass take minutes: idle CPU in the kernel and a 3+ minute wait before the RFB banner.
        vnc = (
            f"ulimit -n {VNC_NOFILE}; for _ in $(seq 20); do x11vnc -display :{i} -rfbport {vnc_port(i)} -rfbportv6 0 "
            "-listen 127.0.0.1 -allow 127.0.0.1 -nopw -forever -shared "
            "-nolookup -no6 -noipv6 -quiet && exit 0; sleep 0.5; done; exit 1"
        )
        self.procs["x11vnc"] = subprocess.Popen(["sh", "-c", vnc])

    def stop(self) -> None:
        for p in self.procs.values():
            p.terminate()
        for name, p in self.procs.items():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("slot %d: %s did not exit, killing", self.slot, name)
                p.kill()
        self.procs.clear()

    def state(self) -> dict[str, bool]:
        return {name: p.poll() is None for name, p in self.procs.items()}


class DisplayManager:
    def __init__(self, slots: int) -> None:
        self.slots = [SlotDisplay(i) for i in range(1, slots + 1)]

    def start(self) -> None:
        for s in self.slots:
            s.start()

    def stop(self) -> None:
        for s in self.slots:
            s.stop()

    def state(self) -> dict[str, dict[str, bool]]:
        return {str(s.slot): s.state() for s in self.slots}
