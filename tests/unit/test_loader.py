import hashlib
import importlib.resources
import inspect
import sys
import types

import pytest

from mendarr.plugins import loader
from mendarr.rpc import PluginSpec


def spec_for(source: str, scope: str = "example.com", version: int = 2) -> PluginSpec:
    return PluginSpec(
        scope=scope, version=version, source=source, sha256=hashlib.sha256(source.encode()).hexdigest()
    )


def builtin_source() -> str:
    return (importlib.resources.files("mendarr.plugins.builtin") / "_default.py").read_text()


def test_load_builtin_default():
    p = loader.load_builtin("_default")
    assert p.ref == "_default@builtin"
    assert p.meta["backend"] == "camoufox"


def test_load_spec_none_is_builtin_default():
    p = loader.load_spec(None)
    assert p is loader.default()
    assert p.ref == loader.BUILTIN_REF == "_default@builtin"


def test_load_spec_builtin_source():
    p = loader.load_spec(spec_for(builtin_source(), "_default", 1))
    assert p.ref == "_default@v1"
    assert p.backend == "camoufox"
    assert inspect.iscoroutinefunction(p.solve)


def test_load_spec_is_cached():
    s = spec_for(builtin_source(), "_default", 1)
    assert loader.load_spec(s) is loader.load_spec(s)


def test_sha_mismatch():
    s = spec_for(builtin_source()).model_copy(update={"sha256": "0" * 64})
    with pytest.raises(loader.PluginLoadError, match="sha256"):
        loader.load_spec(s)


def test_guard_violation():
    with pytest.raises(loader.PluginLoadError, match="rejected by the guard"):
        loader.load_spec(spec_for("import os\n" + builtin_source()))


def test_load_time_error():
    with pytest.raises(loader.PluginLoadError, match="ZeroDivisionError"):
        loader.load_spec(spec_for("X = 1 / 0\n" + builtin_source()))


def test_backend_from_meta():
    src = builtin_source().replace('"camoufox"', '"patchright"', 1)
    assert '"patchright"' in src
    assert loader.load_spec(spec_for(src)).backend == "patchright"


def test_restricted_import():
    with pytest.raises(ImportError):
        loader._restricted_import("os")
    mod = loader._restricted_import("mendarr.sdk", None, None, ("Ctx",), 0)
    assert hasattr(mod, "Ctx")


def test_plugin_has_no_open():
    p = loader.load_spec(spec_for(builtin_source(), "_default", 1))
    assert "open" not in p.solve.__globals__["__builtins__"]


def test_missing_solve_raises():
    sys.modules["mendarr.plugins.builtin._bad"] = types.SimpleNamespace(META={})
    try:
        with pytest.raises(TypeError):
            loader.load_builtin("_bad")
    finally:
        del sys.modules["mendarr.plugins.builtin._bad"]


def test_sync_solve_raises():
    sys.modules["mendarr.plugins.builtin._sync"] = types.SimpleNamespace(META={}, solve=lambda p, c: None)
    try:
        with pytest.raises(TypeError):
            loader.load_builtin("_sync")
    finally:
        del sys.modules["mendarr.plugins.builtin._sync"]
