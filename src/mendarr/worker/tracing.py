"""Failure evidence for the healer: console, network, trimmed DOM of every frame, a screenshot."""

import base64
import logging
from typing import Any

from mendarr.rpc import TraceData

log = logging.getLogger(__name__)

TRACE_GRACE = 2.5  # seconds the capture may take; taken off the plugin's budget when tracing
MAX_CONSOLE = 50
MAX_NETWORK = 150
MAX_FRAMES = 6
PAGE_DOM_CHARS = 60_000
FRAME_DOM_CHARS = 15_000

# Serialises the DOM without scripts/styles, inlining open shadow roots (Turnstile hosts its iframe in one).
TRIM_JS = r"""(limit) => {
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "LINK", "META"]);
  const KEEP = /^(id|class|name|type|role|src|href|action|method|value|title|alt|for|style|hidden|disabled|checked|aria-.*|data-.*)$/;
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  let out = "";
  const walk = (node) => {
    if (out.length > limit) return;
    if (node.nodeType === 3) {
      const t = node.textContent.replace(/\s+/g, " ").trim();
      if (t) out += esc(t.slice(0, 200));
      return;
    }
    if (node.nodeType !== 1 || SKIP.has(node.tagName)) return;
    const tag = node.tagName.toLowerCase();
    if (tag === "svg") { out += "<svg/>"; return; }
    let attrs = "";
    for (const a of node.attributes) if (KEEP.test(a.name)) attrs += ` ${a.name}="${esc(a.value.slice(0, 150))}"`;
    out += `<${tag}${attrs}>`;
    if (node.shadowRoot) {
      out += "<shadow-root>";
      for (const c of node.shadowRoot.childNodes) walk(c);
      out += "</shadow-root>";
    }
    for (const c of node.childNodes) walk(c);
    out += `</${tag}>`;
  };
  if (document.documentElement) walk(document.documentElement);
  return out.length > limit ? out.slice(0, limit) + "<!-- truncated -->" : out;
}"""


class TraceRecorder:
    """Attach right after the page is created; `collect` once the run is over."""

    def __init__(self, page: Any) -> None:
        self.console: list[str] = []
        self.network: list[dict] = []
        page.on("console", self._on_console)
        page.on("pageerror", self._on_pageerror)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_requestfailed)

    def _on_console(self, msg: Any) -> None:
        if msg.type in ("error", "warning") and len(self.console) < MAX_CONSOLE:
            self.console.append(f"{msg.type}: {msg.text[:300]}")

    def _on_pageerror(self, err: Any) -> None:
        if len(self.console) < MAX_CONSOLE:
            self.console.append(f"pageerror: {str(err)[:300]}")

    def _on_response(self, resp: Any) -> None:
        if len(self.network) < MAX_NETWORK:
            self.network.append(
                {
                    "url": resp.url[:200],
                    "status": resp.status,
                    "type": resp.request.resource_type,
                    "cf": resp.headers.get("cf-mitigated", ""),
                }
            )

    def _on_requestfailed(self, req: Any) -> None:
        if len(self.network) < MAX_NETWORK:
            failure = req.failure or ""
            self.network.append(
                {"url": req.url[:200], "status": 0, "type": req.resource_type, "cf": "", "failure": str(failure)[:100]}
            )

    async def collect(self, page: Any, steps: list[str]) -> TraceData:
        data = TraceData(console=list(self.console), network=list(self.network), steps=list(steps)[-200:])
        try:
            data.screenshot_b64 = base64.b64encode(await page.screenshot(type="png")).decode()
        except Exception as e:
            log.debug("trace screenshot failed: %s", e)
        parts: list[str] = []
        try:
            parts.append(await page.evaluate(TRIM_JS, PAGE_DOM_CHARS))
        except Exception as e:
            log.debug("trace dom failed: %s", e)
        for frame in [f for f in page.frames if f != page.main_frame][:MAX_FRAMES]:
            parent = frame.parent_frame
            data.frames.append({"url": frame.url[:300], "name": frame.name, "parent": parent.url[:300] if parent else ""})
            try:
                parts.append(f"\n<!-- frame {frame.url[:300]} -->\n" + await frame.evaluate(TRIM_JS, FRAME_DOM_CHARS))
            except Exception as e:
                parts.append(f"\n<!-- frame {frame.url[:300]}: not readable ({type(e).__name__}) -->")
        data.dom = "".join(parts)
        return data
