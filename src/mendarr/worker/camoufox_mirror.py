"""Fallback source for the Camoufox browser build.

On 2026-11-19 GitHub suspended the Camoufox maintainers, so `camoufox fetch` (which reads their releases) fails.
camoufox.com links a mirror, the GitHub repo `jo-inc/camofox-browser`, whose releases tagged `camoufox-backup-<n>`
carry the same zips (`camoufox-<version>-lin.<arch>.zip`) with a GitHub sha256 `digest`. This module lists those
releases, downloads the zip for this machine, verifies the digest and unpacks it into the layout camoufox 0.5.8 reads
(`<cache>/camoufox/browsers/official/<version>-<build>-<sha8>/`, `config.json` next to `browsers/`). It never imports
camoufox, so it also runs where the browser extra is not installed.

CLI (image build): `python -m mendarr.worker.camoufox_mirror install [--url URL]`.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

TAG_PREFIX = "camoufox-backup-"
ASSET_RE = re.compile(r"^camoufox-(?P<version>.+)-lin\.(?P<arch>[a-z0-9_]+)\.zip$")
ARCHES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}
REPO_NAME = "official"  # the folder camoufox's own `fetch` uses, so its default-repo lookups stay valid
REQUEST_TIMEOUT = httpx.Timeout(30.0, read=120.0)


class MirrorError(RuntimeError):
    pass


@dataclass(frozen=True)
class MirrorBuild:
    version: str  # "158.0.1-beta.41"
    asset_url: str
    sha256: str
    asset_name: str
    created_at: str = ""
    interface: int = 1


def _version_key(version: str) -> tuple:
    """Sortable key: "158.0.1-beta.41" -> ((158, 0, 1), (beta, 41)); a plain release sorts above its betas."""
    base, _, build = version.partition("-")
    nums = tuple(int(p) if p.isdigit() else 0 for p in base.split("."))
    parts = tuple((1, int(p), "") if p.isdigit() else (0, 0, p) for p in build.split(".")) if build else ((2, 0, ""),)
    return nums, parts


def _split(version: str) -> tuple[str, str]:
    """camoufox's (version, build) split: "158.0.1-beta.41" -> ("158.0.1", "beta.41")."""
    base, _, build = version.partition("-")
    return base, build


def _release_version(release: dict, asset_name: str) -> str:
    match = re.fullmatch(r"Camoufox backup v(.+)", str(release.get("name") or "").strip())
    if match:
        return match.group(1)
    m = ASSET_RE.match(asset_name)
    return m.group("version") if m else ""


def _arch() -> str:
    machine = platform.machine().lower()
    if machine not in ARCHES:
        raise MirrorError(f"no Camoufox mirror build for architecture {machine}")
    return ARCHES[machine]


def latest(url: str, transport: httpx.BaseTransport | None = None, version: str | None = None) -> MirrorBuild:
    """The newest `camoufox-backup-*` release's Linux build for this machine's architecture (or exactly `version`)."""
    arch = _arch()
    with httpx.Client(transport=transport, timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url, params={"per_page": 100}, headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        releases = resp.json()
        best: tuple[tuple, MirrorBuild, dict] | None = None
        for rel in releases if isinstance(releases, list) else []:
            if not str(rel.get("tag_name") or "").startswith(TAG_PREFIX):
                continue
            for asset in rel.get("assets") or []:
                m = ASSET_RE.match(str(asset.get("name") or ""))
                if not m or ARCHES.get(m.group("arch")) != arch:
                    continue
                found = _release_version(rel, asset["name"])
                if not found or (version and found != version):
                    continue
                digest = str(asset.get("digest") or "")
                if not digest.startswith("sha256:"):
                    raise MirrorError(f"mirror asset {asset['name']} has no sha256 digest; refusing it")
                build = MirrorBuild(
                    version=found,
                    asset_url=str(asset.get("browser_download_url") or ""),
                    sha256=digest.split(":", 1)[1].lower(),
                    asset_name=asset["name"],
                    created_at=str(asset.get("created_at") or ""),
                )
                key = _version_key(found)
                if best is None or key > best[0]:
                    best = (key, build, rel)
        if best is None:
            raise MirrorError(f"no camoufox-backup release {version or ''} with a linux {arch} build at {url}")
        _, build, rel = best
        return MirrorBuild(**{**build.__dict__, "interface": _interface(client, rel)})


def _interface(client: httpx.Client, release: dict) -> int:
    """The browser interface the release's manifest.json declares (absent or unreadable: 1)."""
    manifest = next((a for a in release.get("assets") or [] if a.get("name") == "manifest.json"), None)
    if manifest is None:
        return 1
    try:
        resp = client.get(manifest["browser_download_url"])
        resp.raise_for_status()
        return int(resp.json().get("interface", 1))
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return 1


def install(
    build: MirrorBuild, cache_root: Path, transport: httpx.BaseTransport | None = None, *, force_pin: bool = True
) -> Path:
    """Download, verify and unpack `build` so camoufox finds it under XDG_CACHE_HOME=`cache_root`; returns its dir.

    `force_pin` records an explicit choice in config.json; leave it off for the build the camoufox package pairs with."""
    install_dir = Path(cache_root) / "camoufox"  # camoufox.pkgman.INSTALL_DIR
    version, tag = _split(build.version)
    folder = f"{version}-{tag}-{build.sha256[:8]}"  # multiversion.version_folder_name
    dest = install_dir / "browsers" / REPO_NAME / folder
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".mirror-", suffix=".zip", dir=cache_root)
    tmp = Path(tmp_name)
    extract = dest.parent / f".extract-{os.getpid()}"
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as out, httpx.Client(
            transport=transport, timeout=REQUEST_TIMEOUT, follow_redirects=True
        ) as client, client.stream("GET", build.asset_url) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes(1 << 20):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != build.sha256.lower():
            raise MirrorError(f"sha256 mismatch for {build.asset_name}: expected {build.sha256}, got {digest.hexdigest()}")
        shutil.rmtree(extract, ignore_errors=True)
        with zipfile.ZipFile(tmp) as zf:
            zf.extractall(extract)  # camoufox.pkgman.unzip
        for root, dirs, files in os.walk(extract):  # multiversion.install_versioned: chmod -R 755
            for n in (*dirs, *files):
                os.chmod(Path(root) / n, 0o755)
        os.chmod(extract, 0o755)
        meta = {  # AvailableVersion.to_metadata
            "version": version,
            "build": tag,
            "prerelease": "beta" in tag or "alpha" in tag,
            "sha256": build.sha256,
            "created_at": build.created_at or None,
            "interface": build.interface,
        }
        (extract / "version.json").write_text(json.dumps(meta))
        if dest.exists():
            shutil.rmtree(dest)
        extract.rename(dest)
        # camoufox set <version> (multiversion._set_pinned): an explicit pin keeps the library from insisting on its
        # own paired build, which this mirror build usually is not
        cfg_file = install_dir / "config.json"
        try:
            cfg = json.loads(cfg_file.read_text())
        except (OSError, ValueError):
            cfg = {}
        cfg["active_version"] = f"browsers/{REPO_NAME}/{folder}"
        if force_pin:
            cfg.update(
                {
                    "channel": f"{REPO_NAME}/prerelease" if meta["prerelease"] else f"{REPO_NAME}/stable",
                    "pinned": f"{version}-{tag}",
                    "pinned_sha": build.sha256,
                }
            )
        cfg_file.write_text(json.dumps(cfg, indent=2))
        (install_dir / ".0.5_FLAG").touch()  # multiversion.COMPAT_FLAG
        return dest
    finally:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(extract, ignore_errors=True)


def pinned_version() -> str:
    """The build the installed camoufox package pairs with (its browser-pin.json), read without importing camoufox."""
    spec = importlib.util.find_spec("camoufox")
    if spec is None or not spec.submodule_search_locations:
        raise MirrorError("the camoufox package is not installed")
    pin_file = Path(next(iter(spec.submodule_search_locations))) / "browser-pin.json"
    try:
        pin = json.loads(pin_file.read_text())
        return f"{pin['version']}-{pin['build']}"
    except (OSError, ValueError, KeyError) as e:
        raise MirrorError(f"no usable browser pin in {pin_file}") from e


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mendarr.worker.camoufox_mirror")
    sub = parser.add_subparsers(dest="cmd", required=True)
    inst = sub.add_parser("install", help="install the newest mirror build into the camoufox cache")
    inst.add_argument("--url", default=None, help="mirror releases API URL (default: MENDARR_CAMOUFOX_MIRROR_URL)")
    which = inst.add_mutually_exclusive_group()
    which.add_argument("--version", default=None, help="install exactly this build, e.g. 156.0.1-beta.36")
    which.add_argument("--pinned", action="store_true", help="install the build the camoufox package pins")
    args = parser.parse_args(argv)
    if args.url is not None:  # an explicit (even empty) URL must work without the mendarr package (Docker build)
        url = args.url
    else:
        from mendarr.config import Settings

        url = Settings().camoufox_mirror_url
    if not url:
        print("no Camoufox mirror URL configured", file=sys.stderr)
        return 1
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    root.mkdir(parents=True, exist_ok=True)
    try:
        build = latest(url, version=pinned_version() if args.pinned else args.version)
        print(f"installing Camoufox {build.version} from the mirror ({build.asset_name})", flush=True)
        print(f"installed: {install(build, root, force_pin=not args.pinned)}")
    except (MirrorError, httpx.HTTPError, OSError, zipfile.BadZipFile) as e:
        print(f"camoufox mirror install failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
