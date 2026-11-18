"""SHA-256 manifests of updated backend builds: the backends volume is writable and persistent, so a build is
only used while its files still match the manifest written at update time."""

import hashlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("mendarr.worker.integrity")

MANIFEST = "MANIFEST.json"
FAILED: dict[str, str] = {}  # backend -> short note, set while its updated build fails the check
_CHECKED: dict[str, bool] = {}  # camoufox: resolved build path -> verified once per process
NOTE = "integrity check failed"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _files(root: Path) -> list[Path]:
    """Regular files and symlinks under `root` (symlinked dirs are not followed), except the manifest itself."""
    out = []
    for dirpath, dirs, names in os.walk(root):
        for n in [*names, *(d for d in dirs if (Path(dirpath) / d).is_symlink())]:
            p = Path(dirpath) / n
            if (p.is_symlink() or p.is_file()) and not (p.parent == root and n == MANIFEST):
                out.append(p)
    return out


def _digest(p: Path) -> str:
    """sha256 of a regular file; a symlink is recorded by its target (planting a link must not pass)."""
    return f"link:{os.readlink(p)}" if p.is_symlink() else sha256_file(p)


def write_manifest(root: Path, version: str, only: list[str] | None = None) -> None:
    """Record the hashes of `only` (relative paths) or of every file and symlink under `root`."""
    rels = only if only is not None else [str(p.relative_to(root)) for p in _files(root)]
    files = {r: _digest(root / r) for r in sorted(rels)}
    (root / MANIFEST).write_text(json.dumps({"version": version, "files": files}, indent=1))


def verify_manifest(root: Path, *, only: list[str] | None = None, no_extra_exec: bool = False) -> str | None:
    """None when `root` matches its manifest, else a short reason. `only` checks just those listed files."""
    try:
        data = json.loads((root / MANIFEST).read_text())
        files = data["files"]
        if not isinstance(files, dict) or not files:
            return "manifest is empty"
    except (OSError, ValueError, KeyError, TypeError):
        return "manifest missing or invalid"
    for rel in only if only is not None else files:
        want = files.get(rel)
        p = root / rel
        if want is None or not (p.is_symlink() or p.is_file()):
            return f"{rel} missing"
        if _digest(p) != want:
            return f"{rel} changed"
    if no_extra_exec:  # a browser also loads unlisted non-executables (prefs, libs): any extra entry fails
        for p in _files(root):
            if str(p.relative_to(root)) not in files:
                return f"unlisted file {p.relative_to(root)}"
    return None


def fail(backend: str, reason: str) -> None:
    FAILED[backend] = f"{NOTE} ({reason})"
    log.warning("updated %s build failed its integrity check, using the built-in build: %s", backend, reason)
