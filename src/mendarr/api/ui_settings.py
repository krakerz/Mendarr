"""Settings page: edits the healer YAML (LLM endpoints, budgets, notifications, raw text)."""

import os
import re
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import yaml
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from mendarr.api.auth import require_user
from mendarr.healer.config import MIN_UPDATE_S, HealerConfig, parse, parse_duration, update_policy
from mendarr.healer.llm import LlmClient
from mendarr.healer.notify import TITLES, Notice, Notifier
from mendarr.web import error_page, render, templates

router = APIRouter(dependencies=[Depends(require_user)])

_LOCK = threading.Lock()
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SECRET = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_EVENTS = ("healed", "manual", "rollback", "default_proposed", "gave_up", "agent_wanted", "update_available")
_CHANNELS = ("discord", "webhook")
_BROKEN = "Fix the file in Raw YAML first"


class _Bad(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _path(request: Request) -> Path:
    return request.app.state.config_file.path


def _load(path: Path) -> tuple[dict, str, str]:
    """(raw mapping, file text, error) — missing file = ({}, "", "")."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, "", ""
    except OSError as e:
        return {}, "", str(e)
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        return {}, text, str(e)
    if not isinstance(data, dict):
        return {}, text, "top level must be a mapping"
    return data, text, ""


def _write(path: Path, data: dict | str) -> str | None:
    """Validate, back up, replace atomically. Returns an error message, None on success."""
    text = data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    try:
        parse(text)
    except (ValueError, yaml.YAMLError) as e:
        return str(e)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return None


def _secret_state(v: str | None) -> str:
    if not v:
        return "not set"
    return f"from {v}" if _SECRET.match(v) else "set"


def _fmt_dur(s: float) -> str:
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n and s % n == 0:
            return f"{int(s // n)}{unit}"
    return f"{int(s)}s" if float(s).is_integer() else str(s)


def _section(raw: dict, key: str) -> dict:
    v = raw.get(key)
    if not isinstance(v, dict):
        v = raw[key] = {}
    return v


def _int(form, key: str, label: str) -> int:
    try:
        return int(str(form.get(key, "")).strip())
    except ValueError:
        raise _Bad(f"{label} must be a whole number") from None


def _flag(form, key: str) -> bool:
    return form.get(key) is not None


def _opt(target: dict, key: str, value: str) -> None:
    value = value.strip()
    if value:
        target[key] = value
    else:
        target.pop(key, None)


def _secret_field(target: dict, form, key: str, clear_key: str) -> None:
    value = str(form.get(key, "")).strip()
    if value:
        target[key] = value
    elif _flag(form, clear_key):
        target.pop(key, None)


def _context(request: Request, text: str | None = None, **extra: object) -> dict:
    path = _path(request)
    raw, file_text, file_error = _load(path)
    try:
        cfg = parse(file_text)
    except (ValueError, yaml.YAMLError):
        cfg = HealerConfig()
    st = request.app.state.settings
    llm_raw = raw.get("llm") if isinstance(raw.get("llm"), dict) else {}
    raw_profiles = llm_raw.get("profiles") if isinstance(llm_raw.get("profiles"), dict) else {}
    names = list(raw_profiles) or list(cfg.llm.profiles)
    profiles = []
    for name in names:
        p = raw_profiles.get(name) if isinstance(raw_profiles.get(name), dict) else {}
        profiles.append(
            {
                "name": name,
                "base_url": p.get("base_url", ""),
                "model": p.get("model", ""),
                "vision": bool(p.get("vision", False)),
                "reasoning_effort": p.get("reasoning_effort") or "",
                "max_tokens": p.get("max_tokens", ""),
                "timeout_s": p.get("timeout_s", ""),
                "secret": _secret_state(p.get("api_key")),
            }
        )
    esc_raw = raw.get("escalation") if isinstance(raw.get("escalation"), dict) else {}
    heal_raw = raw.get("healer") if isinstance(raw.get("healer"), dict) else {}
    notify_raw = raw.get("notify") if isinstance(raw.get("notify"), dict) else {}
    cookies_raw = raw.get("cookies") if isinstance(raw.get("cookies"), dict) else {}
    updates_raw = raw.get("updates") if isinstance(raw.get("updates"), dict) else {}
    upd_enabled, upd_interval, _ = update_policy(st, cfg)
    windows = esc_raw.get("heal_windows")
    if not isinstance(windows, list):
        windows = [_fmt_dur(w) for w in cfg.escalation.heal_windows]
    dur = {
        "heal_windows": ", ".join(str(w) for w in windows),
        "agent_wait": esc_raw.get("agent_wait", _fmt_dur(cfg.escalation.agent_wait)),
        **{
            k: heal_raw.get(k, _fmt_dur(getattr(cfg.healer, k)))
            for k in ("poll_s", "llm_wait_s", "give_up_after", "replay_timeout_s")
        },
        "notify_timeout": notify_raw.get("timeout_s", _fmt_dur(cfg.notify.timeout_s)),
    }
    return {
        "active": "settings",
        "path": str(path),
        "text": file_text if text is None else text,
        "file_error": file_error,
        "cfg": cfg,
        "env": {
            "healer_enabled": st.healer_enabled,
            "public_url": st.public_url,
            "data_dir": st.data_dir,
            "ui_user": st.ui_user,
        },
        "profiles": profiles,
        "order": ", ".join(cfg.llm.order if not llm_raw.get("order") else map(str, llm_raw["order"])),
        "dur": dur,
        "secrets": {
            "discord": _secret_state(notify_raw.get("discord_webhook")),
            "webhook": _secret_state(notify_raw.get("webhook_url")),
        },
        "cookies": {
            "refresh_interval": cookies_raw.get("refresh_interval", ""),
            "refresh_idle_days": cookies_raw.get("refresh_idle_days", ""),
            "env_interval": st.cookie_refresh_interval,
            "env_idle": st.cookie_refresh_idle_days,
        },
        "updates": {
            "enabled": upd_enabled,
            "interval": updates_raw.get("interval", _fmt_dur(upd_interval)),
            "env_interval": st.update_check_interval,
            "view": request.app.state.updates.snapshot(),
        },
        "events": _EVENTS,
        "titles": TITLES,
        **extra,
    }


def _page(request: Request, status: int = 200, **extra: object) -> Response:
    return render(request, "settings.html", status_code=status, **_context(request, **extra))


def _saved(message: str, anchor: str) -> Response:
    return RedirectResponse(f"/ui/settings?ok={quote(message)}#{anchor}", status_code=303)


async def _edit(request: Request, mutate: Callable[[dict, object], None], message: str, anchor: str) -> Response:
    form = await request.form()
    path = _path(request)
    try:
        with _LOCK:
            raw, _, err = _load(path)
            if err:
                raise _Bad(_BROKEN)
            mutate(raw, form)
            err = _write(path, raw)
        if err:
            raise _Bad(err)
    except _Bad as e:
        if e.status == 404:
            return error_page(request, 404, str(e))
        return _page(request, 400, error=str(e))
    return _saved(message, anchor)


@router.get("/settings")
def settings_page(request: Request) -> Response:
    return _page(request)


@router.post("/settings/llm-order")
async def llm_order(request: Request) -> Response:
    def mutate(raw: dict, form) -> None:
        order = [s.strip() for s in str(form.get("order", "")).split(",") if s.strip()]
        _section(raw, "llm")["order"] = order

    return await _edit(request, mutate, "Saved the endpoint order", "llm")


@router.post("/settings/llm")
async def llm_add(request: Request) -> Response:
    def mutate(raw: dict, form) -> None:
        name = str(form.get("name", "")).strip()
        base_url = str(form.get("base_url", "")).strip()
        if not _NAME.match(name):
            raise _Bad("Name: lowercase letters, digits, - and _ (max 32)")
        if not base_url:
            raise _Bad("Base URL is required")
        llm = _section(raw, "llm")
        profiles = llm.get("profiles")
        if not isinstance(profiles, dict):
            profiles = llm["profiles"] = {}
        if name in profiles:
            raise _Bad(f"Endpoint {name} already exists")
        profiles[name] = {"base_url": base_url}
        order = llm.get("order")
        llm["order"] = [*order, name] if isinstance(order, list) else [name]

    return await _edit(request, mutate, "Added the endpoint", "llm")


def _profile(raw: dict, name: str) -> dict:
    llm = raw.get("llm")
    profiles = llm.get("profiles") if isinstance(llm, dict) else None
    if not isinstance(profiles, dict) or not isinstance(profiles.get(name), dict):
        raise _Bad(f"Unknown endpoint {name}", 404)
    return profiles[name]


@router.post("/settings/llm/{name}")
async def llm_save(request: Request, name: str) -> Response:
    def mutate(raw: dict, form) -> None:
        p = _profile(raw, name)
        base_url = str(form.get("base_url", "")).strip()
        if not base_url:
            raise _Bad("Base URL is required")
        p["base_url"] = base_url
        _opt(p, "model", str(form.get("model", "")))
        _opt(p, "reasoning_effort", str(form.get("reasoning_effort", "")))
        _opt(p, "timeout_s", str(form.get("timeout_s", "")))
        if str(form.get("max_tokens", "")).strip():
            p["max_tokens"] = _int(form, "max_tokens", "Max tokens")
        else:
            p.pop("max_tokens", None)
        p["vision"] = _flag(form, "vision")
        _secret_field(p, form, "api_key", "clear_api_key")

    return await _edit(request, mutate, f"Saved endpoint {name}", "llm")


@router.post("/settings/llm/{name}/delete")
async def llm_delete(request: Request, name: str) -> Response:
    def mutate(raw: dict, form) -> None:
        _profile(raw, name)
        llm = raw["llm"]
        del llm["profiles"][name]
        if isinstance(llm.get("order"), list):
            llm["order"] = [n for n in llm["order"] if n != name]

    return await _edit(request, mutate, f"Deleted endpoint {name}", "llm")


@router.post("/settings/budgets")
async def budgets(request: Request) -> Response:
    def mutate(raw: dict, form) -> None:
        esc = _section(raw, "escalation")
        esc["auto_attempts"] = _int(form, "auto_attempts", "Auto attempts")
        esc["heal_rounds"] = _int(form, "heal_rounds", "Heal rounds")
        esc["heal_windows"] = [s.strip() for s in str(form.get("heal_windows", "")).split(",") if s.strip()]
        esc["agent_tier"] = _flag(form, "agent_tier")
        _opt(esc, "agent_wait", str(form.get("agent_wait", "")))
        manual = _section(esc, "manual")
        manual["notify"] = [c for c in form.getlist("manual_notify") if c in _CHANNELS]
        manual["hold_requests"] = _flag(form, "hold_requests")
        healer = _section(raw, "healer")
        healer["enabled"] = _flag(form, "enabled")
        for key, label in (
            ("daily_rounds", "Daily rounds"),
            ("trace_keep", "Traces kept"),
            ("cross_replay_max", "Cross-replay max"),
        ):
            healer[key] = _int(form, key, label)
        for key in ("poll_s", "llm_wait_s", "give_up_after", "replay_timeout_s"):
            _opt(healer, key, str(form.get(key, "")))

    return await _edit(request, mutate, "Saved the budgets", "budgets")


@router.post("/settings/cookies")
async def cookies(request: Request) -> Response:
    def mutate(raw: dict, form) -> None:
        sec = _section(raw, "cookies")
        _opt(sec, "refresh_interval", str(form.get("refresh_interval", "")))
        days = str(form.get("refresh_idle_days", "")).strip()
        if not days:
            sec.pop("refresh_idle_days", None)
            return
        try:
            value = float(days)
        except ValueError:
            raise _Bad("Idle days must be a number") from None
        if value < 0:
            raise _Bad("Idle days must be 0 or more")
        sec["refresh_idle_days"] = value

    return await _edit(request, mutate, "Saved the cookie settings", "cookies")


@router.post("/settings/updates")
async def updates_save(request: Request) -> Response:
    def mutate(raw: dict, form) -> None:
        sec = _section(raw, "updates")
        sec["check"] = _flag(form, "check")
        value = str(form.get("interval", "")).strip()
        if not value:
            sec.pop("interval", None)
            return
        try:
            seconds = parse_duration(value)
        except ValueError as e:
            raise _Bad(str(e)) from None
        if seconds < MIN_UPDATE_S:
            raise _Bad("Check interval must be at least 1h")
        sec["interval"] = value

    return await _edit(request, mutate, "Saved the update settings", "updates")


@router.post("/settings/updates/check")
async def updates_check(request: Request) -> Response:
    checker = request.app.state.updates
    if not checker.snapshot().enabled:
        return RedirectResponse(f"/ui/settings?err={quote('Update checks are switched off')}#updates", status_code=303)
    view = await checker.check_now()
    if view.status == "error":
        return RedirectResponse(f"/ui/settings?err={quote('Update check failed: ' + view.error)}#updates", status_code=303)
    if view.update_available:
        return _saved(f"Mendarr {view.latest} is available", "updates")
    return _saved("Mendarr is up to date", "updates")


@router.post("/settings/notify")
async def notify_save(request: Request) -> Response:
    def mutate(raw: dict, form) -> None:
        n = _section(raw, "notify")
        _secret_field(n, form, "discord_webhook", "clear_discord")
        _secret_field(n, form, "webhook_url", "clear_webhook")
        _opt(n, "discord_ping", str(form.get("discord_ping", "")))
        chosen = set(form.getlist("events"))
        n["events"] = {e: e in chosen for e in _EVENTS}
        _opt(n, "timeout_s", str(form.get("timeout_s", "")))

    return await _edit(request, mutate, "Saved the notifications", "notify")


@router.post("/settings/raw")
async def raw_save(request: Request, text: str = Form("")) -> Response:
    with _LOCK:
        err = _write(_path(request), text)
    if err:
        return _page(request, 400, error=err, text=text)
    return _saved("Saved mendarr.yml", "raw")


def _fragment(request: Request, name: str, **ctx: object) -> Response:
    return templates.TemplateResponse(request, name, ctx)


@router.post("/settings/llm/{name}/check")
async def llm_check(request: Request, name: str) -> Response:
    _, text, _err = _load(_path(request))
    try:
        prof = parse(text).llm.profiles.get(name)
    except (ValueError, yaml.YAMLError):
        prof = None
    if prof is None:
        return _fragment(request, "_settings_check.html", state="failed", detail="unknown profile — save it first")
    client = LlmClient(transport=getattr(request.app.state, "llm_transport", None))
    try:
        ep = await client.probe(name, prof)
    finally:
        await client.aclose()
    if ep is None:
        return _fragment(
            request, "_settings_check.html", state="failed", detail="unreachable or no matching model (see logs)"
        )
    return _fragment(request, "_settings_check.html", state="ok", detail=ep.model)


@router.post("/settings/notify/test")
async def notify_test(request: Request, event: str = Form("healed")) -> Response:
    if event not in TITLES:
        return _fragment(request, "_notify_result.html", lines=[("failed", f"Unknown event {event}")])
    _, text, _err = _load(_path(request))
    try:
        cfg = parse(text)
    except (ValueError, yaml.YAMLError):
        cfg = HealerConfig()
    if not cfg.notify.discord_webhook and not cfg.notify.webhook_url:
        return _fragment(request, "_notify_result.html", lines=[("failed", "No notification channel configured")])
    state = request.app.state
    notifier = Notifier(public_url=state.settings.public_url, transport=getattr(state, "notify_transport", None))
    try:
        res = await notifier.send(cfg.notify, Notice(event, "example.org", "Test notification from the Mendarr dashboard"))
    finally:
        await notifier.aclose()
    if not res:
        lines = [("failed", f"The {event} event is switched off")]
    else:
        lines = [("ok" if ok else "failed", f"{ch}: {'sent' if ok else 'failed'}") for ch, ok in res.items()]
    return _fragment(request, "_notify_result.html", lines=lines)
