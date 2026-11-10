"""`mendarr` command line: run the API (default) or manage the plugin store."""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from mendarr.config import Settings
from mendarr.healer.config import ConfigFile, LlmProfile, config_path
from mendarr.healer.llm import LlmClient
from mendarr.healer.notify import Notice, Notifier
from mendarr.plugins.guard import GuardError
from mendarr.plugins.store import PluginRecord, PluginStore, RollbackPolicy, StoreError

_COLUMNS = ("scope", "version", "status", "author", "backend", "ok", "fail", "parent", "notes")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mendarr")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the API (default)")
    plugins = sub.add_parser("plugins", help="manage solver plugins").add_subparsers(dest="action", required=True)
    ls = plugins.add_parser("list")
    ls.add_argument("--scope")
    ls.add_argument("--json", action="store_true")
    show = plugins.add_parser("show", help="print a plugin's source (default: the active version)")
    show.add_argument("scope")
    show.add_argument("version", nargs="?", type=int)
    imp = plugins.add_parser("import", help="a .py file, - for stdin, or a directory written by export")
    imp.add_argument("path")
    imp.add_argument("--scope", help="required for a file or stdin")
    imp.add_argument("--author", choices=("human", "agent", "llm"), default="human")
    imp.add_argument("--notes")
    imp.add_argument("--activate", action="store_true")
    exp = plugins.add_parser("export")
    exp.add_argument("dir")
    exp.add_argument("--scope")
    promote = plugins.add_parser("promote")
    promote.add_argument("scope")
    promote.add_argument("version", type=int)
    for name in ("rollback", "retire"):
        plugins.add_parser(name).add_argument("scope")
    incidents = sub.add_parser("incidents").add_subparsers(dest="action", required=True)
    incidents.add_parser("list").add_argument("--json", action="store_true")
    incidents.add_parser("show", help="an incident with its attempts, as JSON").add_argument("id", type=int)
    incidents.add_parser("retry", help="heal again now, from the first window").add_argument("id", type=int)
    close = incidents.add_parser("close")
    close.add_argument("id", type=int)
    close.add_argument("--gave-up", action="store_true", help="close as gave_up instead of solved")
    healer = sub.add_parser("healer").add_subparsers(dest="action", required=True)
    healer.add_parser("check", help="probe the LLM endpoints in the configured order")
    notify = sub.add_parser("notify").add_subparsers(dest="action", required=True)
    notify.add_parser("test", help="send a test notification").add_argument(
        "--event", default="healed", choices=["healed", "manual", "rollback", "default_proposed", "gave_up", "agent_wanted"]
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd in (None, "serve"):
        from mendarr.api import app

        app.main()
        return 0
    settings = Settings()
    try:
        store = PluginStore.open(Path(settings.data_dir), policy=RollbackPolicy.from_settings(settings), strict=True)
    except StoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        return _run(store, args)
    except GuardError as e:
        print(f"guard: {e}", file=sys.stderr)
        return 1
    except StoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()


def _table(rows: list[list[str]]) -> None:
    for row in rows:
        print("\t".join(row))


def _run(store: PluginStore, args: argparse.Namespace) -> int:
    if args.cmd == "healer":
        return _healer_check()
    if args.cmd == "notify":
        return _notify_test(args.event)
    if args.cmd == "incidents" and args.action != "list":
        return _incident_action(store, args)
    if args.cmd == "incidents":
        items = store.incidents()
        if args.json:
            print(json.dumps(items, indent=2))
        else:
            _table(
                [["id", "domain", "state", "opened", "reason"]]
                + [[str(i["id"]), i["domain"], i["state"], str(i["opened"]), i["reason"]] for i in items]
            )
        return 0
    action = args.action
    if action == "list":
        recs = store.list(args.scope)
        if args.json:
            print(json.dumps([r.summary() for r in recs], indent=2))
        else:
            _table(
                [list(_COLUMNS)]
                + [[str("-" if (v := r.summary()[c]) is None else v) for c in _COLUMNS] for r in recs]
            )
    elif action == "show":
        rec = store.get(args.scope, args.version) if args.version is not None else store.active(args.scope)
        if rec is None:
            raise StoreError("no active plugin for scope")
        print(rec.source, end="")
    elif action == "import":
        _import(store, args)
    elif action == "export":
        _export(store, args)
    elif action == "promote":
        print(f"active: {store.promote(args.scope, args.version).ref}")
    elif action == "rollback":
        rec = store.rollback(args.scope)
        print(f"active: {rec.ref}" if rec else f"{args.scope} has no active plugin now (requests fall back to _default)")
    elif action == "retire":
        rec = store.retire(args.scope)
        print(f"retired: {rec.ref}" if rec else f"nothing active for {args.scope}")
    return 0


def _import(store: PluginStore, args: argparse.Namespace) -> None:
    if args.path != "-" and Path(args.path).is_dir():
        _import_dir(store, Path(args.path), args)
        return
    if not args.scope:
        raise StoreError("--scope is required")
    source = sys.stdin.read() if args.path == "-" else Path(args.path).read_text(encoding="utf-8")
    rec = store.submit(args.scope, source, author=args.author, notes=args.notes)
    if args.activate:
        rec = store.promote(rec.scope, rec.version)
    print(f"imported {rec.ref} ({rec.status})")


def _import_dir(store: PluginStore, d: Path, args: argparse.Namespace) -> None:
    entries = json.loads((d / "index.json").read_text(encoding="utf-8"))
    entries.sort(key=lambda e: (e["scope"], e["version"]))
    mapping: dict[tuple[str, int], int] = {}
    activate: list[tuple[str, int]] = []
    for e in entries:
        if e["author"] == "builtin":
            continue
        scope = e["scope"]
        source = (d / scope / f"v{e['version']}.py").read_text(encoding="utf-8")
        parent = mapping.get((scope, e["parent"])) if e["parent"] is not None else None
        rec = store.submit(scope, source, author=e["author"], parent=parent, notes=e["notes"])
        mapping[(scope, e["version"])] = rec.version
        if e["status"] == "active":
            activate.append((scope, rec.version))
        print(f"imported {rec.ref} ({rec.status})")
    if args.activate:
        for scope, version in activate:
            print(f"active: {store.promote(scope, version).ref}")


def _export(store: PluginStore, args: argparse.Namespace) -> None:
    recs: list[PluginRecord] = [r for r in store.list(args.scope) if r.author != "builtin" and r.status != "rejected"]
    out = Path(args.dir)
    for r in recs:
        (out / r.scope).mkdir(parents=True, exist_ok=True)
        (out / r.scope / f"v{r.version}.py").write_text(r.source, encoding="utf-8")
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(json.dumps([r.summary() for r in recs], indent=2), encoding="utf-8")
    print(f"exported {len(recs)} plugins to {out}")


def _incident_action(store: PluginStore, args: argparse.Namespace) -> int:
    inc = store.incident(args.id)
    if inc is None:
        print(f"error: no incident #{args.id}", file=sys.stderr)
        return 1
    if args.action == "show":
        print(json.dumps({**inc, "attempts": store.attempts(args.id)}, indent=2))
    elif args.action == "retry":
        store.update_incident(args.id, state="open", heal_window=0, next_attempt_at=time.time())
        print(f"incident #{args.id} queued")
    elif args.action == "close":
        state = "gave_up" if args.gave_up else "solved"
        store.update_incident(args.id, state=state, next_attempt_at=None, note="closed from the CLI")
        print(f"incident #{args.id} {state}")
    return 0


async def _probe(name: str, profile: LlmProfile):
    client = LlmClient()
    try:
        return await client.probe(name, profile)
    finally:
        await client.aclose()


def _healer_check() -> int:
    path = config_path(Settings())
    cfg = ConfigFile(path).get()
    names = list(cfg.llm.order) + [n for n in cfg.llm.profiles if n not in cfg.llm.order]
    if not names:
        print(f"no LLM profiles configured in {path}")
        return 2
    any_ok = False
    for name in names:
        ep = asyncio.run(_probe(name, cfg.llm.profiles[name]))
        if ep is None:
            print(f"{name}\tunreachable")
        else:
            any_ok = True
            print(f"{name}\tok\t{ep.model}")
    return 0 if any_ok else 2


async def _send_test(settings: Settings, cfg, event: str) -> dict[str, bool]:
    notifier = Notifier(settings.public_url)
    try:
        notice = Notice(event, "example.org", "Test notification from mendarr notify test", incident_id=None)
        return await notifier.send(cfg.notify, notice)
    finally:
        await notifier.aclose()


def _notify_test(event: str) -> int:
    settings = Settings()
    cfg = ConfigFile(config_path(settings)).get()
    results = asyncio.run(_send_test(settings, cfg, event))
    if not results:
        print("no notification channel configured")
        return 2
    for channel, ok in results.items():
        print(f"{channel}\t{'ok' if ok else 'failed'}")
    return 0 if all(results.values()) else 1
