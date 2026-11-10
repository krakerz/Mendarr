"""Prompt pack for the plugin-repair LLM, plus parsing of its reply."""

import ast
import base64
import json
import re
from functools import cache
from importlib import resources

from mendarr.core.traces import Trace
from mendarr.healer.config import LlmProfile


class PromptError(Exception):
    pass


SYSTEM = """\
You repair Mendarr solver plugins. A plugin is a small async Python module. It gets a Playwright page that
is showing a Cloudflare challenge and must get the browser through it, usually by finding the Turnstile
widget, clicking its checkbox like a human and waiting for clearance. Mendarr judges success itself from
the final page and cookies; a plugin cannot fake it.

Rules:
- Reply with exactly ONE fenced ```python block that holds the COMPLETE new plugin file.
- The file defines a literal dict META ("backend": "camoufox" or "patchright", "notes": what you changed)
  and `async def solve(page, ctx)`.
- Use only the SDK and the Playwright calls shown in the reference. Allowed imports: asyncio, re, random,
  math, time (never time.sleep) and mendarr.sdk. No other imports, no file, network or process access, no
  eval/exec, no names that start with "_", no bare except, no module-level code besides imports, constants
  and defs.
- Never wait past ctx.remaining().
- Change what the evidence shows is wrong and keep what worked.
"""

_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\n(.*?)```", re.S)
_PY_LANGS = ("", "python", "py", "python3")
_NET_PRIORITY = ("cdn-cgi", "challenges.cloudflare.com")


@cache
def sdk_reference() -> str:
    return resources.files("mendarr.sdk").joinpath("reference.md").read_text("utf-8")


def extract_plugin(text: str) -> str:
    text = _THINK.sub("", text)
    blocks = [
        code for lang, code in _FENCE.findall(text) if lang.lower() in _PY_LANGS and "def solve" in code
    ]
    if not blocks:
        raise PromptError("no ```python block with async def solve in the reply")
    return blocks[-1].strip() + "\n"


def switch_backend(source: str) -> str | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "META"
            and isinstance(node.value, ast.Dict)
        ):
            try:
                meta = ast.literal_eval(node.value)
            except ValueError:
                return None
            new = "patchright" if meta.get("backend", "camoufox") == "camoufox" else "camoufox"
            meta["backend"] = new
            meta["notes"] = f"healer: backend switch to {new}"
            lines = source.splitlines(keepends=True)
            eol = lines[node.end_lineno - 1][len(lines[node.end_lineno - 1].rstrip("\r\n")) :]
            lines[node.lineno - 1 : node.end_lineno] = [f"META = {meta!r}{eol}"]
            return "".join(lines)
    return None


def _is_notable(n: dict) -> bool:
    status = n.get("status") or 0
    url = str(n.get("url", ""))
    return status >= 400 or status == 0 or bool(n.get("cf")) or any(h in url for h in _NET_PRIORITY)


def _network_lines(network: list[dict]) -> list[str]:
    ordered = [n for n in network if _is_notable(n)] + [n for n in network if not _is_notable(n)]
    out = []
    for n in ordered[:40]:
        line = f"{n.get('status', 0)} {n.get('type', '')} {str(n.get('url', ''))[:200]}"
        if n.get("cf"):
            line += f" cf={n['cf']}"
        out.append(line)
    return out


def _fenced(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text.rstrip()}\n```"


def _failure_section(domain: str, url: str, kind: str, detail: str, trace: Trace | None) -> str:
    lines = [f"- domain: {domain}", f"- url: {url}", f"- class: {kind}", f"- detail: {detail}"]
    if trace is not None:
        m = trace.meta
        for key in ("final_url", "status", "title", "challenge_seen", "clicks", "error"):
            if m.get(key) not in (None, "", [], {}):
                lines.append(f"- {key}: {m[key]}")
        cf = {k: v for k, v in (m.get("headers") or {}).items() if k.startswith("cf-")}
        if cf:
            lines.append(f"- cf headers: {json.dumps(cf)}")
    return "## Failure\n" + "\n".join(lines)


def _history_section(history: list[dict], profile: LlmProfile, load_trace) -> str:
    rows = history[-6:]
    lines = [
        f"- window {r['heal_window'] + 1} round {r['round'] + 1}: {r['result']} — {str(r['detail'])[:400]}"
        for r in rows
    ]
    out = "## Previous attempts\n" + "\n".join(lines)
    newest = next((r for r in reversed(rows) if r.get("version") is not None), None)
    if newest is not None and newest.get("source"):
        out += f"\n\n### Last candidate ({newest['scope']}@v{newest['version']})\n" + _fenced(
            newest["source"], "python"
        )
        t = load_trace(newest["trace_id"]) if newest.get("trace_id") and load_trace else None
        if t is not None:
            replay = ""
            steps = t.meta.get("steps") or []
            if steps:
                replay += "Steps:\n" + "\n".join(f"- {s}" for s in steps[-30:]) + "\n"
            replay += "DOM:\n" + _fenced(t.dom[: profile.dom_chars // 3])
            out += "\n\n### Its replay\n" + replay
    return out


def build_messages(
    *,
    domain: str,
    url: str,
    kind: str,
    detail: str,
    plugin_ref: str,
    plugin_source: str,
    trace: Trace | None,
    history: list[dict],
    demo: Trace | None,
    profile: LlmProfile,
    load_trace=None,
) -> list[dict]:
    """`load_trace(id) -> Trace | None` renders the newest candidate's replay trace in the history."""
    parts = [
        "## SDK reference\n" + sdk_reference(),
        f"## Failing plugin ({plugin_ref})\n" + _fenced(plugin_source, "python"),
        _failure_section(domain, url, kind, detail, trace),
    ]
    if trace is not None:
        m = trace.meta
        if m.get("steps"):
            parts.append("## What the plugin logged\n" + "\n".join(f"- {s}" for s in m["steps"][-60:]))
        if m.get("console"):
            parts.append("## Console\n" + "\n".join(f"- {c}" for c in m["console"][:30]))
        if m.get("network"):
            parts.append("## Network\n" + "\n".join(_network_lines(m["network"])))
        if m.get("frames"):
            parts.append("## Frames\n" + "\n".join(f"- {json.dumps(f)}" for f in m["frames"]))
        if trace.dom:
            parts.append("## Page DOM (trimmed)\n" + _fenced(trace.dom[: profile.dom_chars], "html"))
    if demo is not None:
        body = json.dumps(demo.meta.get("extra", {}), indent=1)[:4000]
        steps = demo.meta.get("steps") or []
        if steps:
            body += "\nSteps:\n" + "\n".join(f"- {s}" for s in steps)
        parts.append("## Demonstration by a human\n" + body)
    if history:
        parts.append(_history_section(history, profile, load_trace))
    parts.append(
        f"## Task\nWrite the complete fixed plugin for {domain}. Reply with one ```python block."
    )
    text = "\n\n".join(parts)
    content: str | list[dict] = text
    if profile.vision and trace is not None and trace.screenshot:
        b64 = base64.b64encode(trace.screenshot).decode()
        content = [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]
