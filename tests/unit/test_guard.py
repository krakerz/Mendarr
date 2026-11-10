from importlib import resources

import pytest

import mendarr.sdk
from mendarr.plugins import guard
from mendarr.plugins.guard import GuardError, check, import_allowed


def wrap(top: str = "", body: str = "pass") -> str:
    """A valid plugin with `top` at module level (before solve) and `body` inside solve."""
    return (
        "from mendarr.sdk import Ctx, Page\n\n"
        'META = {"backend": "camoufox"}\n\n'
        + (top + "\n\n" if top else "")
        + "async def solve(page: Page, ctx: Ctx) -> None:\n"
        + f"    {body}\n"
    )


def test_accepts_valid():
    report = check(wrap())
    assert report.meta["backend"] == "camoufox"


def test_builtin_default_passes():
    src = resources.files("mendarr.plugins.builtin").joinpath("_default.py").read_text()
    assert check(src).meta["backend"] == "camoufox"


def test_design_example_passes():
    src = (
        "from mendarr.sdk import Ctx, Page\n\n"
        'META = {"backend": "camoufox", "parent": 1, "notes": "example"}\n\n\n'
        "async def solve(page: Page, ctx: Ctx) -> None:\n"
        "    await ctx.wait_challenge(timeout=15)\n"
        "    frame = await ctx.find_turnstile_frame()\n"
        "    if frame:\n"
        '        await ctx.human_click(frame, "input[type=checkbox]")\n'
        "    await ctx.wait_clearance(timeout=20)\n"
    )
    assert check(src).meta["backend"] == "camoufox"


def test_accepts_allowed_modules():
    src = (
        "from __future__ import annotations\n\n"
        "import asyncio\nimport math\nimport random\nimport re\nimport time\n\n"
        "from mendarr.sdk import Ctx, Page\n\n"
        'META = {"backend": "camoufox"}\n\n\n'
        "class Counter:\n"
        "    def __init__(self):\n"
        "        self.n = 0\n\n"
        "    def bump(self):\n"
        "        self.n += 1\n"
        "        return self.n\n\n\n"
        "async def solve(page: Page, ctx: Ctx) -> None:\n"
        "    await asyncio.sleep(random.uniform(0.1, 0.2))\n"
        '    re.search("a", "b")\n'
        "    math.floor(1.5)\n"
        "    t = time.monotonic()\n"
        '    clicks = getattr(ctx, "clicks")\n'
        "    try:\n"
        "        Counter().bump()\n"
        "    except TimeoutError:\n"
        "        pass\n"
        "    except Exception:\n"
        "        pass\n"
        '    print(f"{t} {clicks}")\n'
    )
    assert check(src).meta["backend"] == "camoufox"


IMPORT_ASYNCIO = "import asyncio"

# (top-level snippet, snippet inside solve, expected substring of the error)
REJECTS = {
    "import-os": ("import os", "pass", "import os"),
    "import-subprocess": ("import subprocess", "pass", "import subprocess"),
    "import-asyncio-subprocess": ("import asyncio.subprocess", "pass", "import asyncio.subprocess"),
    "from-os": ("from os import system", "pass", "not allowed"),
    "from-importlib": ("from importlib import import_module", "pass", "importlib"),
    "from-asyncio-subprocess": ("from asyncio import create_subprocess_shell", "pass", "not allowed"),
    "asyncio-subprocess-attr": (
        IMPORT_ASYNCIO,
        'await asyncio.create_subprocess_exec("sh")',
        "asyncio.create_subprocess_exec",
    ),
    "eval": ("", 'eval("1")', "eval"),
    "exec": ("", 'exec("x=1")', "exec"),
    "compile": ("", 'compile("1", "x", "eval")', "compile"),
    "open": ("", 'open("/etc/passwd")', "open"),
    "dunder-import": ("", '__import__("os")', "__import__"),
    "globals": ("", "globals()", "globals"),
    "vars": ("", "vars()", "vars"),
    "locals": ("", "locals()", "locals"),
    "breakpoint": ("", "breakpoint()", "breakpoint"),
    "type": ("", "type(page)", "type"),
    "setattr": ("", 'setattr(ctx, "a", 1)', "setattr"),
    "subclasses": ("", "().__class__.__bases__[0].__subclasses__()", "private attribute"),
    "getattr-dynamic": ("", "name = 'x'\n    getattr(ctx, name)", "literal"),
    "getattr-dunder": ("", 'getattr(ctx, "__class__")', "private attribute"),
    "getattr-private": ("", 'getattr(ctx, "_logger")', "private attribute"),
    "getattr-alias": ("", "f = getattr", "getattr"),
    "builtins-string": ("", 'x = {}["__builtins__"]', "dunder"),
    "format-string": ("", '"{0.__class__}".format(1)', "dunder"),
    "impl-obj": ("", "ctx.page._impl_obj", "private attribute"),
    "solve-globals": ("", "solve.__globals__", "private attribute"),
    "cr-frame": (
        IMPORT_ASYNCIO,
        "asyncio.current_task().get_coro().cr_frame",
        "current_task",
    ),
    "browser-type": (
        "",
        'page.context.browser.browser_type.launch(executable_path="/bin/sh")',
        "browser_type",
    ),
    "screenshot-path": ("", 'await page.screenshot(path="/app/x.png")', "path="),
    "kwargs-splat": ("", 'kw = {"path": "/x"}\n    await page.pdf(**kw)', "**kwargs"),
    "time-sleep": ("import time", "time.sleep(100)", "time.sleep"),
    "from-time-sleep": ("from time import sleep", "pass", "not allowed"),
    "bare-except": ("", "try:\n        pass\n    except:\n        pass", "bare except"),
    "base-exception": (
        "",
        "try:\n        pass\n    except BaseException:\n        pass",
        "BaseException",
    ),
    "cancelled-error": (
        IMPORT_ASYNCIO,
        "try:\n        pass\n    except asyncio.CancelledError:\n        pass",
        "CancelledError",
    ),
    "finally-loop": (
        IMPORT_ASYNCIO,
        "try:\n        pass\n    finally:\n        while True:\n            await asyncio.sleep(1)",
        "finally",
    ),
    "top-while": ("while True:\n    pass", "pass", "While statement"),
    "top-call": ("X = print(1)", "pass", "must not call"),
    "top-default-arg": ("def helper(x=print(1)):\n    return x", "pass", "default argument"),
    "top-decorator": ("@staticmethod\ndef f():\n    pass", "pass", "decorators"),
    "relative-import": ("from . import x", "pass", "relative"),
    "star-import": ("from mendarr.sdk import *", "pass", "star"),
    "sdk-other-name": ("from mendarr.sdk import logging", "pass", "not allowed"),
    "import-sdk-module": ("import mendarr.sdk", "pass", "import mendarr.sdk"),
    "module-as-value": ("import asyncio\na = asyncio", "pass", "module asyncio"),
    "asyncio-unix-events": (IMPORT_ASYNCIO, 'asyncio.unix_events.os.system("id")', "asyncio.unix_events"),
    "private-module-attr": ("import random as r", "r._os", "private"),
    "global": ("", "global X", "global"),
    "dunder-alias": ("import asyncio as __x__", "pass", "alias"),
}


@pytest.mark.parametrize(("top", "body", "expected"), REJECTS.values(), ids=REJECTS.keys())
def test_rejects(top, body, expected):
    with pytest.raises(GuardError) as err:
        check(wrap(top, body))
    assert expected in str(err.value)


STRUCTURE = {
    "no-meta": ("async def solve(page, ctx):\n    pass\n", "META dict is missing"),
    "meta-call": (
        'META = dict(backend="camoufox")\nasync def solve(page, ctx):\n    pass\n',
        "literal",
    ),
    "meta-backend": (
        'META = {"backend": "chrome"}\nasync def solve(page, ctx):\n    pass\n',
        "backend",
    ),
    "no-solve": ('META = {"backend": "camoufox"}\n', "missing"),
    "sync-solve": ('META = {"backend": "camoufox"}\ndef solve(page, ctx):\n    pass\n', "async"),
    "solve-arity": (
        'META = {"backend": "camoufox"}\nasync def solve(page):\n    pass\n',
        "exactly",
    ),
    "too-large": ("# " + "x" * 70000, "larger"),
    "syntax": ("async def solve(", "syntax error"),
}


@pytest.mark.parametrize(("src", "expected"), STRUCTURE.values(), ids=STRUCTURE.keys())
def test_structure_rejects(src, expected):
    with pytest.raises(GuardError) as err:
        check(src)
    assert expected in str(err.value)


def test_sdk_names_cover_sdk_all():
    assert set(mendarr.sdk.__all__) <= guard.SDK_NAMES


def test_import_allowed():
    assert import_allowed("asyncio", None)
    assert import_allowed("mendarr.sdk", ["Ctx"])
    assert not import_allowed("os", None)
    assert not import_allowed("mendarr.sdk", None)
    assert not import_allowed("time", ["sleep"])
    assert not import_allowed("mendarr.sdk", ["logging"])
