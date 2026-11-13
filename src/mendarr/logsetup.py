"""Logging setup shared by the API and the worker."""

import logging

log = logging.getLogger("mendarr.logsetup")

NOISY = (
    "httpx", "httpcore", "websockets", "asyncio", "playwright", "patchright",
    "curl_cffi", "urllib3", "uvicorn", "uvicorn.error", "uvicorn.access",
)  # fmt: skip
_QUIET_HTTP = ("httpx", "httpcore")  # one INFO line per API->worker call drowns the request flow


def configure(level: str) -> str:
    """Set up logging; returns the level name for uvicorn. DEBUG applies to Mendarr only."""
    name = level.strip().upper()
    if name not in logging.getLevelNamesMapping() or name in ("NOTSET", "WARN"):
        log.warning("unknown log level %r, using INFO", level)
        name = "INFO"
    debug = name == "DEBUG"
    logging.basicConfig(level="INFO" if debug else name)
    logging.getLogger("mendarr").setLevel(name)
    if debug:
        for noisy in NOISY:
            logging.getLogger(noisy).setLevel(logging.INFO)
    for quiet in _QUIET_HTTP:
        logging.getLogger(quiet).setLevel(max(logging.getLevelName(name), logging.WARNING))
    return "info" if debug else name.lower()
