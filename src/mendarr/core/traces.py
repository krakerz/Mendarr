"""Failure traces on disk: data/traces/<domain>--<ms>-<hex6>/ {screenshot.png, dom.html, meta.json}."""

import base64
import binascii
import json
import logging
import re
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mendarr.rpc import SolveResult

log = logging.getLogger(__name__)

TraceKind = Literal["failure", "replay", "demo"]

_ID = re.compile(r"^[a-z0-9._-]+--\d+-[0-9a-f]{6}$")
_KEEP_HEADERS = ("server", "content-type", "location")
_DROP_BLOCKS = re.compile(
    r"<script\b.*?</script\s*>|<style\b.*?</style\s*>|<noscript\b.*?</noscript\s*>|<!--.*?-->", re.S | re.I
)
_SVG = re.compile(r"<svg\b.*?</svg\s*>", re.S | re.I)
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class FailureReport:
    """What the router hands the healer after a failed (non-replay) solve."""

    url: str
    domain: str
    kind: str  # FailureKind
    detail: str
    plugin: str  # SolveResult.plugin ref
    result: SolveResult | None
    session: bool = False
    background: bool = False  # cookie-refresher solve: opens an incident, never counts in stats


@dataclass(frozen=True)
class Trace:
    id: str
    meta: dict
    dom: str
    screenshot: bytes | None


def trim_html(html: str, limit: int = 60_000) -> str:
    out = _DROP_BLOCKS.sub("", html)
    out = _SVG.sub("<svg/>", out)
    out = _WS.sub(" ", out)
    if len(out) > limit:
        out = out[:limit] + "\n<!-- truncated -->"
    return out


def _ms(name: str) -> int:
    return int(name.rsplit("--", 1)[1].split("-", 1)[0])


class TraceStore:
    def __init__(self, root: Path | None, clock: Callable[[], float] = time.time) -> None:
        self._root = root
        self._clock = clock
        self._last: dict[str, float] = {}

    @classmethod
    def open(cls, root: Path) -> "TraceStore":
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("trace dir %s unusable, traces are not saved: %s", root, e)
            return cls(None)
        return cls(root)

    def should_save(self, domain: str, min_interval_s: float) -> bool:
        now = self._clock()
        last = self._last.get(domain)
        if last is not None and now - last < min_interval_s:
            return False
        self._last[domain] = now
        return True

    def save(
        self,
        *,
        domain: str,
        url: str,
        kind: TraceKind,
        result: SolveResult | None,
        failure: str = "",
        detail: str = "",
        plugin: str = "",
        incident_id: int | None = None,
        extra: dict | None = None,
    ) -> str | None:
        if self._root is None:
            return None
        now = self._clock()
        tid = f"{domain}--{int(now * 1000)}-{secrets.token_hex(3)}"
        d = self._root / tid
        snap = result.snapshot if result else None
        data = result.trace if result else None
        shot = None
        if data is not None and data.screenshot_b64:
            try:
                shot = base64.b64decode(data.screenshot_b64)
            except (binascii.Error, ValueError):
                shot = None
        if data is not None and data.dom:
            dom = data.dom
        elif snap is not None:
            dom = trim_html(snap.html)
        else:
            dom = ""
        meta = {
            "id": tid,
            "domain": domain,
            "url": url,
            "at": now,
            "kind": kind,
            "failure": failure,
            "detail": detail,
            "plugin": plugin,
            "incident_id": incident_id,
            "final_url": snap.url if snap else "",
            "status": snap.status if snap else None,
            "title": snap.title if snap else "",
            "headers": {
                k: v
                for k, v in (snap.headers if snap else {}).items()
                if k.startswith("cf-") or k in _KEEP_HEADERS
            },
            "cookies": [c.name for c in snap.cookies] if snap else [],
            "challenge_seen": result.challenge_seen if result else False,
            "clicks": result.clicks if result else 0,
            "error": {"type": result.error.type, "detail": result.error.detail}
            if result and result.error
            else None,
            "frames": data.frames if data else [],
            "console": data.console if data else [],
            "network": data.network if data else [],
            "steps": data.steps if data else [],
            "has_screenshot": shot is not None,
            "extra": extra or {},
        }
        try:
            d.mkdir(parents=True)
            if shot is not None:
                (d / "screenshot.png").write_bytes(shot)
            (d / "dom.html").write_text(dom, encoding="utf-8")
            (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("saving trace %s failed: %s", tid, e)
            shutil.rmtree(d, ignore_errors=True)
            return None
        return tid

    def load(self, trace_id: str) -> Trace | None:
        if self._root is None or not _ID.match(trace_id):
            return None
        d = self._root / trace_id
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            dom = (d / "dom.html").read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        try:
            shot = (d / "screenshot.png").read_bytes()
        except OSError:
            shot = None
        return Trace(trace_id, meta, dom, shot)

    def _dirs(self, domain: str) -> list[Path]:
        if self._root is None:
            return []
        dirs = [p for p in self._root.glob(f"{domain}--*") if p.is_dir() and _ID.match(p.name)]
        return sorted(dirs, key=lambda p: _ms(p.name), reverse=True)

    @staticmethod
    def _kind(d: Path) -> str | None:
        try:
            return json.loads((d / "meta.json").read_text(encoding="utf-8")).get("kind")
        except (OSError, ValueError):
            return None

    def latest(self, domain: str, kind: TraceKind | None = None) -> Trace | None:
        for d in self._dirs(domain):
            if kind is None or self._kind(d) == kind:
                return self.load(d.name)
        return None

    def prune(self, domain: str, keep: int, keep_demos: int = 3) -> int:
        kept = demos = removed = 0
        for d in self._dirs(domain):
            if self._kind(d) == "demo":
                demos += 1
                if demos <= keep_demos:
                    continue
            else:
                kept += 1
                if kept <= keep:
                    continue
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
        return removed
