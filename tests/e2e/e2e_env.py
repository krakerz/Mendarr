"""Shared e2e settings and helpers (own module: `import conftest` would collide with tests/conftest.py)."""

import os
import subprocess

import httpx

API = os.environ.get("MENDARR_E2E_URL", "").rstrip("/")
FIXTURE = os.environ.get("MENDARR_E2E_FIXTURE", "http://fixture:8080").rstrip("/")
FIXTURE_HOST = os.environ.get("MENDARR_E2E_FIXTURE_HOST", "http://127.0.0.1:8180").rstrip("/")
ONLINE = os.environ.get("MENDARR_E2E_ONLINE") == "1"


def v1(payload: dict, timeout: float = 150) -> tuple[int, dict]:
    r = httpx.post(f"{API}/v1", json=payload, timeout=timeout)
    return r.status_code, r.json()


COMPOSE = os.environ.get(
    "MENDARR_E2E_COMPOSE", os.path.expanduser("~/Projects/docker-storage/mendarr/compose.yml")
)


def compose_exec(service: str, *args: str, stdin: str | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", COMPOSE, "exec", "-T", service, *args],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )


def cli(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    """`mendarr ...` inside the API container (plugins have no HTTP endpoint before M4's auth)."""
    return compose_exec("mendarr", "mendarr", *args, stdin=stdin)
