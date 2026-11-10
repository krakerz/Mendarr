"""AST guard for plugin source, run when a plugin is stored (API) and before it is loaded (worker).

A tripwire, not a sandbox: in-process Python can't be contained; the worker container (non-root,
no store, no secrets) is the wall. Rules are allowlists where they can be.
"""

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass

BACKENDS = ("camoufox", "patchright")
DEFAULT_BACKEND = "camoufox"
MAX_SOURCE_BYTES = 64 * 1024

# importable module -> names a plugin may use from it (None = any public name; math has no submodules)
MODULE_API: dict[str, frozenset[str] | None] = {
    "asyncio": frozenset({
        "sleep", "wait_for", "wait", "gather", "timeout", "timeout_at", "TimeoutError", "Event", "Lock",
        "Semaphore", "Condition", "Queue", "create_task", "as_completed", "FIRST_COMPLETED", "ALL_COMPLETED",
    }),
    "re": frozenset({
        "compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split", "escape",
        "error", "Pattern", "Match", "I", "IGNORECASE", "M", "MULTILINE", "S", "DOTALL", "X", "VERBOSE",
    }),
    "random": frozenset({
        "random", "uniform", "randint", "randrange", "choice", "choices", "shuffle", "sample", "gauss",
        "triangular",
    }),
    "math": None,
    "time": frozenset({"monotonic", "time", "perf_counter"}),  # no sleep: it blocks the event loop
}
SDK_MODULE = "mendarr.sdk"
SDK_NAMES = frozenset({"Ctx", "Page", "Frame", "Locator"})
FUTURE_NAMES = frozenset({"annotations"})

SAFE_BUILTINS = frozenset({
    "abs", "aiter", "all", "anext", "any", "bool", "bytes", "callable", "chr", "dict", "divmod",
    "enumerate", "filter", "float", "format", "frozenset", "getattr", "hasattr", "hash", "int",
    "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min", "next", "ord", "pow", "print",
    "property", "range", "repr", "reversed", "round", "set", "slice", "sorted", "staticmethod", "str",
    "sum", "super", "tuple", "zip", "NotImplemented",
    "Exception", "ArithmeticError", "AssertionError", "AttributeError", "IndexError", "KeyError",
    "LookupError", "NotImplementedError", "RuntimeError", "StopAsyncIteration", "StopIteration",
    "TimeoutError", "TypeError", "ValueError", "ZeroDivisionError",
})
BANNED_NAMES = frozenset({
    "eval", "exec", "compile", "open", "globals", "vars", "locals", "setattr", "delattr", "breakpoint",
    "input", "help", "exit", "quit", "memoryview", "type", "object", "dir",
    # catching these swallows cancellation, so the request deadline can't stop the plugin
    "BaseException", "CancelledError", "GeneratorExit", "KeyboardInterrupt", "SystemExit",
})
BANNED_ATTRS = frozenset({
    # frames and code objects lead back to globals and builtins
    "f_globals", "f_locals", "f_builtins", "f_back", "f_code", "gi_frame", "gi_code", "cr_frame",
    "cr_code", "ag_frame", "ag_code", "tb_frame", "tb_next", "co_code", "get_stack", "print_stack",
    # processes, threads, raw fds
    "subprocess_exec", "subprocess_shell", "create_subprocess_exec", "create_subprocess_shell",
    "run_in_executor", "set_default_executor", "to_thread", "add_reader", "add_writer",
    "add_signal_handler",
    # Playwright: launching another binary, writing or reading worker files
    "browser_type", "launch", "launch_persistent_context", "launch_server", "connect", "connect_over_cdp",
    "save_as", "set_input_files",
})
BANNED_KWARGS = frozenset({
    "path", "executable_path", "user_data_dir", "downloads_path", "traces_dir", "record_har_path",
    "record_video_dir",
})
_DUNDER = re.compile(r"__\w+__")
_RUNS_CODE = (
    ast.Call, ast.Await, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
    ast.NamedExpr, ast.Yield, ast.YieldFrom,
)


class GuardError(ValueError):
    def __init__(self, violations: list[str]) -> None:
        super().__init__("; ".join(violations))
        self.violations = violations


@dataclass(frozen=True)
class GuardReport:
    meta: dict  # the literal META, "backend" filled in


def _api_allows(api: frozenset[str] | None, name: str) -> bool:
    return not name.startswith("_") and (api is None or name in api)


def import_allowed(name: str, fromlist: Iterable[str] | None = ()) -> bool:
    """The import rules; the worker's restricted __import__ applies them again at run time."""
    names = list(fromlist or ())
    if name in MODULE_API:
        return all(_api_allows(MODULE_API[name], n) for n in names)
    if name == SDK_MODULE:
        return bool(names) and set(names) <= SDK_NAMES
    if name == "__future__":
        return set(names) <= FUTURE_NAMES
    return False


def check(source: str) -> GuardReport:
    if len(source.encode()) > MAX_SOURCE_BYTES:
        raise GuardError([f"source is larger than {MAX_SOURCE_BYTES} bytes"])
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise GuardError([f"syntax error at line {e.lineno}: {e.msg}"]) from e
    v = _Visitor(tree)
    v.check_body(tree.body, "module")
    v.visit(tree)
    meta = _meta(tree, v.violations)
    _check_solve(tree, v.violations)
    if v.violations:
        raise GuardError(v.violations)
    return GuardReport(meta)


def _meta(tree: ast.Module, violations: list[str]) -> dict:
    node = next(
        (s for s in tree.body if isinstance(s, ast.Assign)
         and any(isinstance(t, ast.Name) and t.id == "META" for t in s.targets)),
        None,
    )
    if node is None:
        violations.append("META dict is missing")
        return {}
    try:
        meta = ast.literal_eval(node.value)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        violations.append("META must be a literal dict")
        return {}
    if not isinstance(meta, dict) or not all(isinstance(k, str) for k in meta):
        violations.append("META must be a dict with string keys")
        return {}
    backend = meta.setdefault("backend", DEFAULT_BACKEND)
    if backend not in BACKENDS:
        violations.append(f"META backend {backend!r} is not one of {', '.join(BACKENDS)}")
    return meta


def _check_solve(tree: ast.Module, violations: list[str]) -> None:
    fn = next(
        (s for s in tree.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == "solve"),
        None,
    )
    if fn is None:
        violations.append("async def solve(page, ctx) is missing")
    elif not isinstance(fn, ast.AsyncFunctionDef):
        violations.append("solve must be async")
    elif len(fn.args.posonlyargs) + len(fn.args.args) != 2 or fn.args.vararg or fn.args.kwarg:
        violations.append("solve must take exactly (page, ctx)")


class _Visitor(ast.NodeVisitor):
    def __init__(self, tree: ast.Module) -> None:
        self.violations: list[str] = []
        self._judged: set[int] = set()  # Name nodes already judged by their parent (module bases, getattr)
        self.modules: dict[str, str] = {}  # local name -> imported module
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.modules[a.asname or a.name.split(".")[0]] = a.name

    def bad(self, node: ast.AST, msg: str) -> None:
        self.violations.append(f"line {getattr(node, 'lineno', '?')}: {msg}")

    def check_body(self, body: list[ast.stmt], where: str) -> None:
        """Module and class bodies run at load time: only definitions, imports and call-free assignments."""
        for stmt in body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.Pass, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            if isinstance(stmt, ast.Assign):
                self.static(stmt.value, f"{where}-level assignment")
                continue
            if isinstance(stmt, ast.AnnAssign):
                self.static(stmt.annotation, "annotation")
                if stmt.value is not None:
                    self.static(stmt.value, f"{where}-level assignment")
                continue
            self.bad(stmt, f"{type(stmt).__name__} statement is not allowed at {where} level")

    def static(self, node: ast.AST, what: str) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, _RUNS_CODE):
                self.bad(sub, f"{what} must not call or await anything")
                return

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.decorator_list:
            self.bad(node, "decorators are not allowed")
        a = node.args
        for d in [*a.defaults, *(d for d in a.kw_defaults if d is not None)]:
            self.static(d, "default argument")
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, *(x for x in (a.vararg, a.kwarg) if x)]:
            if arg.annotation is not None:
                self.static(arg.annotation, "annotation")
        if node.returns is not None:
            self.static(node.returns, "annotation")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.decorator_list or node.keywords:
            self.bad(node, "class decorators and keywords are not allowed")
        for b in node.bases:
            self.static(b, "class base")
        self.check_body(node.body, "class")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.name not in MODULE_API:
                self.bad(node, f"import {a.name} is not allowed")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        names = [a.name for a in node.names]
        if node.level:
            self.bad(node, "relative imports are not allowed")
        elif "*" in names:
            self.bad(node, "star imports are not allowed")
        elif not import_allowed(node.module or "", names):
            self.bad(node, f"from {node.module} import {', '.join(names)} is not allowed")
        self.generic_visit(node)

    def visit_alias(self, node: ast.alias) -> None:
        if node.asname and (_DUNDER.fullmatch(node.asname) or node.asname in BANNED_NAMES):
            self.bad(node, f"alias {node.asname} is not allowed")

    def visit_arg(self, node: ast.arg) -> None:
        if _DUNDER.fullmatch(node.arg):
            self.bad(node, f"argument name {node.arg} is not allowed")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if id(node) in self._judged:
            return
        n = node.id
        if n in BANNED_NAMES or _DUNDER.fullmatch(n):
            self.bad(node, f"name {n} is not allowed")
        elif n in ("getattr", "hasattr"):
            self.bad(node, f"{n} is only allowed as a call with a literal attribute name")
        elif n in self.modules:
            self.bad(node, f"module {n} may only be used as {n}.<name>")

    def _attr(self, node: ast.AST, name: str) -> None:
        if name.startswith("_"):
            self.bad(node, f"private attribute .{name} is not allowed")
        elif name in BANNED_ATTRS:
            self.bad(node, f"attribute .{name} is not allowed")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._attr(node, node.attr)
        base = node.value
        if isinstance(base, ast.Name) and base.id in self.modules:
            mod = self.modules[base.id]
            if not _api_allows(MODULE_API.get(mod, frozenset()), node.attr):
                self.bad(node, f"{mod}.{node.attr} is not allowed")
            self._judged.add(id(base))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Name) and f.id in ("getattr", "hasattr"):
            self._judged.add(id(f))
            name = node.args[1] if len(node.args) >= 2 else None
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                self.bad(node, f"{f.id} needs a literal attribute name")
            else:
                self._attr(node, name.value)
        for kw in node.keywords:
            if kw.arg is None:
                self.bad(node, "**kwargs in calls are not allowed")
            elif kw.arg in BANNED_KWARGS:
                self.bad(node, f"keyword argument {kw.arg}= is not allowed")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        v = node.value
        if isinstance(v, bytes):
            v = v.decode("latin-1")
        if isinstance(v, str) and _DUNDER.search(v):
            self.bad(node, "strings naming dunder attributes are not allowed")

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.bad(node, "bare except is not allowed (it swallows cancellation)")
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        for stmt in node.finalbody:
            if any(isinstance(s, (ast.While, ast.For, ast.AsyncFor)) for s in ast.walk(stmt)):
                self.bad(stmt, "loops in finally blocks are not allowed")
        self.generic_visit(node)

    visit_TryStar = visit_Try

    def visit_Global(self, node: ast.Global) -> None:
        self.bad(node, "global is not allowed")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.bad(node, "nonlocal is not allowed")
