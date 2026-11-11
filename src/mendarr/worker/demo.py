"""Teach by demonstration: records what a human does during a manual solve (no typed text, no cookie values)."""

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

MAX_EVENTS = 300
URL_CHARS = 200

# Passive capture-phase listeners only, no prototype patching. "__BINDING__" is the random binding name.
DEMO_JS = r"""
(() => {
  const B = "__BINDING__";
  const call = (ev) => { try { const f = window[B]; if (typeof f === "function") f(ev).catch(() => {}); } catch (e) {} };
  const cssPath = (el) => {
    const parts = [];
    let n = el;
    for (let depth = 0; n && n.nodeType === 1 && depth < 10; depth++) {
      let p = n.localName;
      if (n.id) { parts.unshift(p + "#" + n.id); break; }
      const cls = typeof n.className === "string" ? n.className.trim().split(/\s+/).filter(Boolean).slice(0, 2) : [];
      if (cls.length) p += "." + cls.join(".");
      const parent = n.parentNode;
      if (parent && parent.children) {
        const same = Array.from(parent.children).filter((c) => c.localName === n.localName);
        if (same.length > 1) p += ":nth-of-type(" + (same.indexOf(n) + 1) + ")";
      }
      parts.unshift(p);
      if (!n.parentElement && parent && parent.host) { parts.unshift(">>>"); n = parent.host; }
      else n = n.parentElement;
    }
    return parts.join(" > ").split(" > >>> > ").join(" >>> ");
  };
  const describe = (el) => {
    const r = el.getBoundingClientRect();
    const attrs = {};
    for (const a of ["type", "name", "role", "aria-label", "href", "title"]) {
      const v = el.getAttribute && el.getAttribute(a);
      if (v) attrs[a] = String(v).slice(0, 80);
    }
    const formy = /^(input|textarea|select)$/.test(el.localName);
    return {
      selector: cssPath(el), tag: el.localName, attrs,
      text: formy ? "" : String(el.innerText || el.textContent || "").trim().slice(0, 60),
      rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
    };
  };
  addEventListener("pointerdown", (e) => {
    const el = (e.composedPath && e.composedPath()[0]) || e.target;
    if (!(el instanceof Element)) return;
    call(Object.assign({ type: "click", x: Math.round(e.clientX), y: Math.round(e.clientY), button: e.button, trusted: e.isTrusted }, describe(el)));
  }, true);
  addEventListener("keydown", (e) => call({ type: "key", key: e.key && e.key.length === 1 ? "char" : String(e.key) }), true);
  addEventListener("submit", (e) => call({ type: "submit", selector: e.target instanceof Element ? cssPath(e.target) : "" }), true);
  if (window === window.top) {
    addEventListener("blur", () => setTimeout(() => {
      const a = document.activeElement;
      if (a && a !== document.body) call({ type: "iframe-focus", selector: cssPath(a), tag: a.localName, src: String(a.src || "").slice(0, 200) });
    }, 0));
  }
})();
"""

# The same cssPath as above, as a standalone function to run on a frame element from Python.
CSS_PATH_JS = r"""(el) => {
  const parts = [];
  let n = el;
  for (let depth = 0; n && n.nodeType === 1 && depth < 10; depth++) {
    let p = n.localName;
    if (n.id) { parts.unshift(p + "#" + n.id); break; }
    const cls = typeof n.className === "string" ? n.className.trim().split(/\s+/).filter(Boolean).slice(0, 2) : [];
    if (cls.length) p += "." + cls.join(".");
    const parent = n.parentNode;
    if (parent && parent.children) {
      const same = Array.from(parent.children).filter((c) => c.localName === n.localName);
      if (same.length > 1) p += ":nth-of-type(" + (same.indexOf(n) + 1) + ")";
    }
    parts.unshift(p);
    if (!n.parentElement && parent && parent.host) { parts.unshift(">>>"); n = parent.host; }
    else n = n.parentElement;
  }
  return parts.join(" > ").split(" > >>> > ").join(" >>> ");
}"""


def _cut(u: str) -> str:
    return u[:URL_CHARS]


class DemoRecorder:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._t0 = clock()
        self.name = "__m" + secrets.token_hex(5)
        self.events: list[dict] = []
        self.start_url = ""
        self._pending: set[asyncio.Task] = set()
        self.paused = False  # while the auto-solver drives the page its clicks are not the human's demo

    async def install(self, context: Any) -> None:
        await context.expose_binding(self.name, self._on_event)
        await context.add_init_script(DEMO_JS.replace("__BINDING__", self.name))

    def watch(self, page: Any) -> None:
        page.on("framenavigated", self._on_nav)

    def _t(self) -> float:
        return round(self._clock() - self._t0, 3)

    def _on_event(self, source: Any, ev: Any) -> None:
        if self.paused or len(self.events) >= MAX_EVENTS or not isinstance(ev, dict):
            return
        frame = source.get("frame") if isinstance(source, dict) else None
        main = frame is None or frame.parent_frame is None
        frame_url = _cut(frame.url if frame else "")
        if ev.get("type") == "key":
            if ev.get("key") == "char":
                last = self.events[-1] if self.events else None
                if last and last["type"] == "typing" and last["frame_url"] == frame_url:
                    last["count"] += 1
                else:
                    self.events.append({"t": self._t(), "type": "typing", "count": 1, "main": main, "frame_url": frame_url})
            else:
                self.events.append(
                    {"t": self._t(), "type": "key", "key": str(ev.get("key", ""))[:20], "main": main, "frame_url": frame_url}
                )
            return
        e = {"t": self._t(), **ev, "main": main, "frame_url": frame_url}
        self.events.append(e)
        if e.get("type") == "click" and not main:
            task = asyncio.get_running_loop().create_task(self._frame_info(frame, e))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def _frame_info(self, frame: Any, e: dict) -> None:
        try:
            el = await frame.frame_element()
            e["frame_box"] = await el.bounding_box()
            e["frame_selector"] = await el.evaluate(CSS_PATH_JS)
        except Exception as exc:
            log.debug("demo frame info failed: %s", exc)
            e["frame_selector"] = ""

    def _on_nav(self, frame: Any) -> None:
        if self.paused or frame.url == "about:blank" or len(self.events) >= MAX_EVENTS:
            return
        main = frame.parent_frame is None
        self.events.append({"t": self._t(), "type": "nav", "main": main, "url": _cut(frame.url)})
        if main and not self.start_url:
            self.start_url = _cut(frame.url)

    def steps(self) -> list[str]:
        out = []
        for e in self.events:
            t = e["t"]
            where = "page"
            if not e.get("main", True):
                box = e.get("frame_box") or {}
                where = (
                    f"frame {e.get('frame_url', '')} (element {e.get('frame_selector', '?')}, "
                    f"box {box.get('x', '?')},{box.get('y', '?')} {box.get('width', '?')}x{box.get('height', '?')})"
                )
            kind = e["type"]
            if kind == "click":
                untrusted = "" if e.get("trusted", True) else " [untrusted]"
                out.append(f"{t:7.2f}s click {where}: {e.get('selector', '')} at ({e.get('x')},{e.get('y')}){untrusted}")
            elif kind == "nav":
                out.append(f"{t:7.2f}s navigate {'page' if e['main'] else 'frame'} {e['url']}")
            elif kind == "typing":
                out.append(f"{t:7.2f}s typed {e['count']} characters in {where}")
            elif kind == "key":
                out.append(f"{t:7.2f}s key {e['key']} in {where}")
            elif kind == "submit":
                out.append(f"{t:7.2f}s submit {e.get('selector', '')} in {where}")
            elif kind == "iframe-focus":
                out.append(f"{t:7.2f}s focus moved into iframe {e.get('selector', '')} {e.get('src', '')}")
            else:
                out.append(f"{t:7.2f}s {kind}")
        return out

    async def summary(self, final_url: str, cookies: list[dict]) -> dict:
        if self._pending:
            await asyncio.wait(set(self._pending), timeout=2)
        return {
            "start_url": self.start_url,
            "final_url": _cut(final_url),
            "duration_s": round(self._clock() - self._t0, 1),
            "events": self.events,
            "frames": sorted({e["url"] for e in self.events if e["type"] == "nav" and not e["main"]}),
            # names/attributes only: cf_clearance is a credential and must never reach an LLM prompt
            "cookies": [
                {k: c.get(k) for k in ("name", "domain", "path", "expires", "httpOnly", "secure")} for c in cookies
            ],
        }
