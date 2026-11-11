"""Vendor the dashboard's static assets (htmx, Lucide icon sprite, licences). Re-run to update; pins at the top."""

import hashlib
import re
import urllib.error
import urllib.request
from pathlib import Path

HTMX = "2.0.4"
LUCIDE = "0.469.0"
OUT = Path(__file__).resolve().parents[1] / "src" / "mendarr" / "web" / "static"
ICONS = [
    "layout-dashboard", "siren", "puzzle", "cookie", "mouse-pointer-click", "settings", "log-out", "log-in",
    "shield-check", "circle-check", "circle-x", "clock", "rotate-ccw", "circle-arrow-up", "undo-2", "ban",
    "git-compare", "play", "send", "save", "trash-2", "refresh-cw", "wifi", "wifi-off", "image", "file-code",
    "network", "chevron-down", "external-link", "triangle-alert", "plus", "x", "bell", "pencil", "eye", "check",
]
ALIASES = {
    "circle-check": "check-circle", "circle-x": "x-circle", "circle-arrow-up": "arrow-up-circle",
    "triangle-alert": "alert-triangle",
}
JSD = "https://cdn.jsdelivr.net/npm"
GH = "https://raw.githubusercontent.com"
versions: list[tuple[str, str, str]] = []


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 - fixed https URLs
        return r.read()


def save(rel: str, url: str, data: bytes) -> None:
    path = OUT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    versions.append((rel, url, hashlib.sha256(data).hexdigest()))


def fetch_first(*urls: str) -> tuple[str, bytes]:
    last: Exception | None = None
    for u in urls:
        try:
            return u, fetch(u)
        except urllib.error.URLError as e:
            last = e
    raise SystemExit(f"download failed: {urls}: {last}")


def main() -> None:
    url = f"{JSD}/htmx.org@{HTMX}/dist/htmx.min.js"
    save("vendor/htmx/htmx.min.js", url, fetch(url))
    url, data = fetch_first(f"{JSD}/htmx.org@{HTMX}/LICENSE", f"{GH}/bigskysoftware/htmx/v{HTMX}/LICENSE")
    save("vendor/htmx/LICENSE", url, data)
    url, data = fetch_first(f"{JSD}/lucide-static@{LUCIDE}/LICENSE", f"{GH}/lucide-icons/lucide/{LUCIDE}/LICENSE")
    save("vendor/lucide/LICENSE", url, data)

    symbols = []
    for name in ICONS:
        candidates = [f"{JSD}/lucide-static@{LUCIDE}/icons/{name}.svg"]
        if name in ALIASES:
            candidates.append(f"{JSD}/lucide-static@{LUCIDE}/icons/{ALIASES[name]}.svg")
        url, data = fetch_first(*candidates)
        svg = data.decode()
        svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S)
        inner = re.search(r"<svg[^>]*>(.*)</svg>", svg, flags=re.S)
        if not inner:
            raise SystemExit(f"unexpected svg for {name}")
        body = " ".join(inner.group(1).split())
        symbols.append(
            f'<symbol id="{name}" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round">{body}</g></symbol>'
        )
        if name == "shield-check":
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / "favicon.svg").write_bytes(data)
    sprite = '<svg xmlns="http://www.w3.org/2000/svg" style="display:none">\n' + "\n".join(symbols) + "\n</svg>\n"
    save("vendor/lucide/sprite.svg", f"lucide-static@{LUCIDE} (built from {len(ICONS)} icons)", sprite.encode())

    lines = [f"{rel}  {url}  {digest}" for rel, url, digest in versions]
    (OUT / "vendor" / "VERSIONS.txt").write_text("\n".join(lines) + "\n")
    print(f"vendored {len(versions)} files into {OUT}")


if __name__ == "__main__":
    main()
