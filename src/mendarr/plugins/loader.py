"""Plugin loading in the worker: the builtin module, or source shipped by the API (guarded, then exec'd)."""

import builtins
import hashlib
import importlib
import inspect
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

from mendarr.plugins import guard
from mendarr.rpc import PluginSpec

if TYPE_CHECKING:
    from mendarr.sdk import Ctx, Page

BUILTIN_REF = "_default@builtin"
_CACHE_SIZE = 64
_loaded: "OrderedDict[str, LoadedPlugin]" = OrderedDict()


class PluginLoadError(Exception):
    pass


@dataclass(frozen=True)
class LoadedPlugin:
    scope: str
    version: str
    meta: dict
    solve: "Callable[[Page, Ctx], Awaitable[None]]"

    @property
    def ref(self) -> str:
        return f"{self.scope}@{self.version}"

    @property
    def backend(self) -> str:
        return str(self.meta.get("backend", guard.DEFAULT_BACKEND))


def _check_solve(scope: str, solve: object) -> None:
    if solve is None or not inspect.iscoroutinefunction(solve):
        raise TypeError(f"plugin {scope!r} must define `async def solve(page, ctx)`")


def load_builtin(scope: str = "_default") -> LoadedPlugin:
    module = importlib.import_module(f"mendarr.plugins.builtin.{scope}")
    solve = getattr(module, "solve", None)
    _check_solve(scope, solve)
    return LoadedPlugin(scope, "builtin", dict(module.META), solve)


@cache
def default() -> LoadedPlugin:
    return load_builtin("_default")


def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level != 0 or not guard.import_allowed(name, fromlist):
        raise ImportError(f"import of {name!r} is not allowed in plugins")
    return importlib.__import__(name, globals, locals, fromlist, level)


def _builtins() -> dict:
    safe = {name: getattr(builtins, name) for name in guard.SAFE_BUILTINS}
    safe["__import__"] = _restricted_import
    safe["__build_class__"] = builtins.__build_class__
    return safe


def load_source(scope: str, version: int, source: str) -> LoadedPlugin:
    """Guard, then exec in a fresh namespace with restricted builtins. Raises GuardError or the load error."""
    guard.check(source)
    namespace = {"__builtins__": _builtins(), "__name__": f"mendarr_plugin.{scope}.v{version}"}
    exec(compile(source, f"<plugin {scope}@v{version}>", "exec"), namespace)
    solve = namespace.get("solve")
    _check_solve(scope, solve)
    return LoadedPlugin(scope, f"v{version}", dict(namespace.get("META", {})), solve)


def load_spec(spec: PluginSpec | None) -> LoadedPlugin:
    if spec is None:
        return default()
    digest = hashlib.sha256(spec.source.encode()).hexdigest()
    if digest != spec.sha256:
        raise PluginLoadError(f"{spec.ref}: sha256 mismatch")
    key = f"{spec.ref}:{digest}"
    if (hit := _loaded.get(key)) is not None:
        _loaded.move_to_end(key)
        return hit
    try:
        plugin = load_source(spec.scope, spec.version, spec.source)
    except guard.GuardError as e:
        raise PluginLoadError(f"{spec.ref} rejected by the guard: {e}") from e
    except Exception as e:
        raise PluginLoadError(f"{spec.ref} failed to load: {type(e).__name__}: {e}") from e
    _loaded[key] = plugin
    while len(_loaded) > _CACHE_SIZE:
        _loaded.popitem(last=False)
    return plugin
