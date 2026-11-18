# Mendarr

Self-healing, FlareSolverr-compatible Cloudflare solver with multi-engine stealth browser automation.

Mendarr is a drop-in replacement for FlareSolverr designed to reliably bypass Cloudflare challenges. It isolates headless and real headed browsers in a dedicated worker container, automatically caches cleared cookies, and uses local LLMs to dynamically repair and heal solver plugins when challenge patterns change.

---

## Features

- **FlareSolverr Drop-in Replacement**: Fully compatible `/v1` endpoint (`request.get`, `request.post`, sessions) for Prowlarr, Jackett, and custom scrapers.
- **Smart Cookie Cache**: Fast path reuse of validated clearance cookies without spawning heavy browser instances.
- **Update Checker**: Shows a "New version available" card in the dashboard when a newer release is published, and can ping Discord or a webhook once per new version (opt-in event). Off with `MENDARR_UPDATE_CHECK=false`. It asks GitHub for the latest published release, which sends your public IP to GitHub once per interval.
- **Background Cookie Refresher**: Optionally re-checks cached cookies on a schedule and re-solves challenged domains in the background. Background checks and solves always leave one browser slot free for real requests; the last check result is kept across restarts.
- **Multi-Engine Stealth Backends**:
  - `camoufox`: Firefox with fingerprint injection and GeoIP matching.
  - `patchright`: Hardened Chromium driver bypassing CDP leaks.
  - `obscura`: High-performance headless stealth engine.
- **Browser Backend Updates**: Settings → Browser backends shows the installed and latest obscura and Camoufox browser builds and updates them in place, without rebuilding the image (optional auto update). Updated builds are hash-checked before use and fall back to the image's build if anything changed.
- **Backend Switch Order & Browser Language**: Choose, by dragging pills, the order the healer tries backends in, and set one browser language for every backend (default: the exit country's main language).
- **Automated LLM Healing**: Failed challenge requests automatically open incidents. A local OpenAI-compatible LLM (e.g., LM Studio, vLLM, Ollama) inspects traces (screenshots, DOM, network calls) and writes a repaired solver script verified against replay gates before promotion.
- **Per-Domain Plugin Scoping**: New or repaired solver scripts are scoped to that domain only; the active default solver is never broken or overwritten.
- **Interactive Manual Solve Studio**: Opens the site in the chosen browser backend and streams it to the web dashboard over an authenticated noVNC live view. You solve it by hand while Mendarr records your actions as a demonstration for the healer; nothing is captured until you click **Finish & Capture**. **Attempt auto solve** runs the domain's solver plugin on the open page while you watch.
- **Strict Upstream Proxy Support**: Route all worker browser and egress traffic through HTTP/SOCKS5 proxies or VPNs with strict kill-switch behavior (no direct leak fallback).
- **Rich Notifications**: Discord alerts with mention support (`@here`, `@everyone`, role IDs) and structured generic JSON webhooks.
- **External Agent Support (MCP)**: Native Model Context Protocol endpoint (`/mcp`) for external agent inspection and repair.

---

## Quick Start (Docker Compose)

Mendarr publishes pre-built multi-architecture container images to GitHub Packages:
- API Server: `ghcr.io/krakerz/mendarr:latest`
- Browser Worker: `ghcr.io/krakerz/mendarr-browser:latest`

### `docker-compose.yml`

```yaml
services:
  mendarr-browser:
    image: ghcr.io/krakerz/mendarr-browser:latest
    shm_size: 1gb
    init: true
    ulimits:
      nofile: 65536
    environment:
      - MENDARR_SLOTS=2
      - MENDARR_WORKER_TOKEN=${MENDARR_WORKER_TOKEN:-generate_a_random_token}
      # Optional: route all worker browser traffic through an upstream proxy or VPN
      # - MENDARR_PROXY_URL=socks5://192.168.1.10:1080
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    sysctls:
      - net.ipv6.conf.all.disable_ipv6=1
      - net.ipv6.conf.default.disable_ipv6=1
      - net.ipv6.conf.lo.disable_ipv6=1
    volumes:
      # updated obscura / Camoufox builds (Settings -> Browser backends); see "Browser backend updates" below
      - mendarr-backends:/data/backends
    restart: unless-stopped

  mendarr:
    image: ghcr.io/krakerz/mendarr:latest
    ports:
      - "8191:8191"
    environment:
      - MENDARR_WORKER_URL=http://mendarr-browser:8192
      - MENDARR_WORKER_TOKEN=${MENDARR_WORKER_TOKEN:-generate_a_random_token}
      - MENDARR_UI_USER=admin
      - MENDARR_UI_PASSWORD=admin
    volumes:
      - mendarr-data:/data
    depends_on:
      mendarr-browser:
        condition: service_healthy
    restart: unless-stopped

volumes:
  mendarr-data:
  mendarr-backends:
```

Start the containers:
```sh
docker compose up -d
```

- **FlareSolverr API**: `http://localhost:8191/v1`
- **Web Dashboard**: `http://localhost:8191/ui` (Default: `admin` / `admin`)

> **Note on open-file limits**: some Docker hosts (`LimitNOFILE=infinity` in the Docker service) give containers an open-file limit of about a billion, which makes x11vnc and other tools that walk every file descriptor crawl. The worker caps x11vnc itself; `ulimits: nofile: 65536` above covers the rest of the browser container. Check with `docker exec <browser container> sh -c 'ulimit -n'`.

> **Note on Reverse Proxies**: If serving Mendarr behind Nginx, Caddy, Traefik, or NPM, enable WebSocket upgrades (`Upgrade` and `Connection` headers) on `/ui/vnc/` for the interactive Manual Solve studio.

### Browser backend updates

When a fix only needs a newer browser build (for example a newer user agent), update it from **Settings → Browser backends** instead of rebuilding the image:

- **obscura**: downloads the latest release binary from GitHub (sha256-checked when the release publishes a digest), test-launches it, then idle browsers switch to it on their next use.
- **Camoufox**: downloads the newest browser build that matches the installed Camoufox package, test-launches it, then restarts the worker once no solve, session or manual solve is open. **Restart now** applies it at once and closes open sessions.
- **patchright**: its Chromium is pinned to the patchright package, so it only updates with a new worker image.
- **Revert** goes back to the previous build, or to the one in the image.
- Every updated build gets a hash list; a build that changed on disk is ignored (with a warning in the log and on the card) and the image's build is used.
- Auto update is off by default: `MENDARR_BACKEND_AUTO_UPDATE=true` on the worker, interval `MENDARR_BACKEND_UPDATE_INTERVAL` (default `24h`, minimum `1h`).

Updated builds live in the worker's `/data/backends` (the `mendarr-backends` volume above); the current and the previous build of each are kept. A Camoufox build is several hundred MB. Without this volume the update buttons have nowhere to write and the worker keeps the image's builds.

**On an external drive** (or any host folder), bind-mount it instead of the named volume:

```yaml
  mendarr-browser:
    volumes:
      - /mnt/external/mendarr-backends:/data/backends
```

```sh
sudo mkdir -p /mnt/external/mendarr-backends
sudo chown 1000:1000 /mnt/external/mendarr-backends   # the worker runs as uid 1000
```

The drive must use a Linux filesystem (ext4, btrfs, xfs): the updater needs symlinks and executable files, which FAT32, exFAT and NTFS mounts don't provide. Don't mount it with `noexec`. Keep it mounted before the containers start (e.g. `nofail` plus a systemd mount dependency), or Docker creates an empty folder on the system disk in its place.

---

## Configuration

Settings can be passed as environment variables or managed in `mendarr.yml` stored in `/data`:

### Primary Settings

| Environment Variable | Default | Description |
|---|---|---|
| `MENDARR_HOST` | `0.0.0.0` | API bind address |
| `MENDARR_PORT` | `8191` | API listening port |
| `MENDARR_DATA_DIR` | `/data` | Directory for database, plugins, and trace storage |
| `MENDARR_LOG_LEVEL` | `info` | Logging verbosity (`debug`, `info`, `warning`, `error`). `debug` is verbose for Mendarr itself only; libraries stay at `info` |
| `MENDARR_WORKER_URL` | `http://127.0.0.1:8192` | URL of the browser worker service |
| `MENDARR_WORKER_TOKEN` | empty | Shared bearer token between API and worker container |
| `MENDARR_SLOTS` | `2` | Number of concurrent browser slots on the worker |
| `MENDARR_UI_USER` | `admin` | Dashboard username (login disabled if empty) |
| `MENDARR_UI_PASSWORD` | empty | Dashboard password |
| `MENDARR_PUBLIC_URL` | empty | Public base URL (e.g. `http://mendarr.lan:8191`) for links in alerts |
| `MENDARR_COOKIE_CACHE` | `true` | Enable fast-path clearance cookie caching |
| `MENDARR_COOKIE_MAX_TTL_HOURS` | `6` | Hard cap on a cached entry's lifetime in hours. `0` or `auto` = no cap: the entry lives as long as its `cf_clearance` cookie (24 h when that cookie has no expiry) |
| `MENDARR_COOKIE_REFRESH_INTERVAL` | empty | How often cached cookies are re-checked in the background (`6h`, `90m`). `0` or empty = off. Also on the Settings page. The first round runs 60 s after start |
| `MENDARR_COOKIE_REFRESH_IDLE_DAYS` | `7` | Stop re-checking a domain that has not been requested for this many days |
| `MENDARR_UPDATE_CHECK` | `true` | Check GitHub for a newer published release and show a "New version available" card in the dashboard. `false` = no requests at all. Also on the Settings page |
| `MENDARR_UPDATE_CHECK_INTERVAL` | `24h` | How often to check (`12h`, `1d`); minimum `1h`. The first check runs 60 s after start |
| `MENDARR_UPDATE_RELEASES_URL` | GitHub's latest-release API URL of Mendarr | For testing only: read the latest release from this URL instead (same JSON shape: `tag_name`, `html_url`, `published_at`) |
| `MENDARR_DEFAULT_BACKEND` | `camoufox` | Browser engine for automated solves whose plugin sets no `backend` (`camoufox`, `patchright` or `obscura`); `solver.default_backend` in `mendarr.yml` overrides it |
| `MENDARR_PROXY_URL` | empty | Upstream proxy URL (`http://`, `https://`, `socks5://`) |
| `MENDARR_PROXY_USERNAME` | empty | Username for upstream proxy (alternative to URL embedding) |
| `MENDARR_PROXY_PASSWORD` | empty | Password for upstream proxy |
| `MENDARR_EGRESS_COUNTRY_URL` | `https://api.country.is/{ip}` | Country lookup for the exit IP (`{ip}` is replaced; the IP is sent to that service). Empty disables it |
| `MENDARR_GEOIP` | `true` | Match Camoufox timezone & locale to egress IP |
| `MENDARR_LOCALE` | empty | Browser language for every backend (e.g. `en-US`); `solver.locale` in `mendarr.yml` (Settings page) wins. Empty = the exit country's main language when `MENDARR_GEOIP` is on (an Australian exit gets `en-AU`); unknown exit IP = the backend's default |

### Advanced & Integrations

| Environment Variable | Default | Description |
|---|---|---|
| `MENDARR_MCP_TOKEN` | empty | Bearer token to enable `/mcp` Model Context Protocol endpoint |
| `MENDARR_MCP_REPLAYS_PER_HOUR` | `30` | Rate limit for MCP plugin validation replays (`0` = unlimited) |
| `MENDARR_HEALER_ENABLED` | `true` | Enable background LLM solver healing loop |
| `MENDARR_ROLLBACK_ENABLED` | `true` | Automatically rollback domain plugins if success rate drops |
| `MENDARR_ROLLBACK_THRESHOLD` | `0.5` | Success rate threshold (0.0–1.0) triggering auto-rollback |
| `MENDARR_COOKIE_CHECK` | `curl` | Clearance pre-check method (`curl` impersonate or `browser`) |
| `MENDARR_UI_SECRET` | auto | Secret key for session cookie signing (stored in `<data_dir>/ui_secret`) |
| `MENDARR_UI_SESSION_HOURS` | `168.0` | Dashboard session duration in hours (default 7 days) |
| `MENDARR_BACKEND_AUTO_UPDATE` | `false` | Worker: install new obscura and Camoufox browser builds automatically |
| `MENDARR_BACKEND_UPDATE_INTERVAL` | `24h` | Worker: how often the auto update looks (minimum 1h) |
| `MENDARR_BACKENDS_DIR` | `/data/backends` | Worker: volume holding updated builds; empty = image versions only |

### Notifications & LLM (`mendarr.yml`)

The configuration file supports environment variable interpolation (`${VAR}`):

```yaml
llm:
  order: [local_llm]
  profiles:
    local_llm:
      base_url: "http://192.168.1.50:1234/v1"
      model: "gemma-4-26b-a4b"
      api_key: "${MENDARR_LLM_KEY}"
      vision: true

healer:
  replay_timeout_s: 120s   # budget of each healer/agent/plugin-test replay (tier 0, 1 and 2; default 120s); also on the Settings page

cookies:
  refresh_interval: 6h     # 0 = off; overrides MENDARR_COOKIE_REFRESH_INTERVAL
  refresh_idle_days: 7
  refresh_timeout: 90s     # budget of one background re-solve (default 60s, minimum 10s)

solver:
  default_backend: patchright   # camoufox | patchright | obscura; for plugins without their own backend; overrides MENDARR_DEFAULT_BACKEND
  backend_order: [camoufox, patchright, obscura]   # order the healer switches a failing plugin's backend in (subset allowed); manual solve lists backends in this order
  locale: en-US                 # browser language for every backend; overrides MENDARR_LOCALE (empty = MENDARR_LOCALE / exit country's main language)

updates:
  check: true              # overrides MENDARR_UPDATE_CHECK
  interval: 24h            # minimum 1h; overrides MENDARR_UPDATE_CHECK_INTERVAL

notify:
  discord_webhook: "${DISCORD_WEBHOOK_URL}"
  discord_ping: "@here"   # @here, @everyone, or <@&role_id>
  webhook_url: "http://n8n.lan:5678/webhook/mendarr"
  events:
    healed: true
    manual: true
    rollback: true
    default_proposed: true
    agent_wanted: true
```

---

## Development

```sh
# Install dependencies
uv sync --extra browser

# Run tests
uv run pytest tests/unit/

# Run API server
uv run mendarr serve

# Run Browser Worker
uv run mendarr-worker
```
