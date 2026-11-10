"""Browser backends a plugin picks with META["backend"]."""

from mendarr.plugins.guard import BACKENDS, DEFAULT_BACKEND

__all__ = ["BACKENDS", "DEFAULT_BACKEND", "BackendUnavailable", "UpstreamUnreachable", "error_types"]


class BackendUnavailable(Exception):
    pass


class UpstreamUnreachable(Exception):
    """The upstream proxy can't be used (exit IP lookup through it failed); never a reason to go direct."""


def error_types(backend: str) -> tuple[type[Exception], type[Exception]]:
    """(Error, TimeoutError) of the driver behind a backend; patchright raises its own classes."""
    try:
        if backend == "patchright":
            from patchright.async_api import Error, TimeoutError
        else:
            from playwright.async_api import Error, TimeoutError
    except ImportError as e:
        raise BackendUnavailable(f"backend {backend!r} is not installed in this worker") from e
    return Error, TimeoutError
