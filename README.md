# Mendarr

Self-healing, FlareSolverr-compatible Cloudflare solver with multi-engine stealth browser automation.

Mendarr is a drop-in replacement for FlareSolverr designed to reliably bypass Cloudflare challenges. It isolates headless and real headed browsers in a dedicated worker container, automatically caches cleared cookies, and uses local LLMs to dynamically repair and heal solver plugins when challenge patterns change.

---

## Features

- **FlareSolverr Drop-in Replacement**: Fully compatible `/v1` endpoint (`request.get`, `request.post`, sessions) for Prowlarr, Jackett, and custom scrapers.
- **Smart Cookie Cache**: Fast path reuse of validated clearance cookies without spawning heavy browser instances.
- **Multi-Engine Stealth Backends**:
  - `camoufox`: Firefox with fingerprint injection and GeoIP matching.
  - `patchright`: Hardened Chromium driver bypassing CDP leaks.
  - `obscura`: High-performance headless stealth engine.
- **Automated LLM Healing**: Failed challenge requests automatically open incidents. A local OpenAI-compatible LLM (e.g., LM Studio, vLLM, Ollama) inspects traces (screenshots, DOM, network calls) and writes a repaired solver script verified against replay gates before promotion.
- **Per-Domain Plugin Scoping**: New or repaired solver scripts are scoped to that domain only; the active default solver is never broken or overwritten.
- **Interactive Manual Solve Studio**: View and solve challenges in real-time inside the web dashboard via an authenticated noVNC live browser stream.
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
    environment:
      - MENDARR_SLOTS=2
      - MENDARR_WORKER_TOKEN=${MENDARR_WORKER_TOKEN:-generate_a_random_token}
      # Optional: route all worker browser traffic through an upstream proxy or VPN
      # - MENDARR_PROXY_URL=socks5://192.168.1.10:1080
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
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
```

Start the containers:
```sh
docker compose up -d
```

- **FlareSolverr API**: `http://localhost:8191/v1`
- **Web Dashboard**: `http://localhost:8191/ui` (Default: `admin` / `admin`)

---

## Configuration

Settings can be passed as environment variables or managed in `mendarr.yml` stored in `/data`:

| Environment Variable | Default | Description |
|---|---|---|
| `MENDARR_HOST` | `0.0.0.0` | API bind address |
| `MENDARR_PORT` | `8191` | API listening port |
| `MENDARR_WORKER_URL` | `http://127.0.0.1:8192` | URL of the browser worker service |
| `MENDARR_WORKER_TOKEN` | empty | Shared secret between API and worker |
| `MENDARR_UI_USER` | `admin` | Dashboard username (login disabled if empty) |
| `MENDARR_UI_PASSWORD` | empty | Dashboard password |
| `MENDARR_COOKIE_CACHE` | `true` | Enable in-memory clearance cookie cache |
| `MENDARR_COOKIE_MAX_TTL_HOURS` | `6` | Maximum age of cached cookies in hours |
| `MENDARR_PROXY_URL` | empty | Upstream proxy URL (`http://`, `https://`, `socks5://`) |
| `MENDARR_GEOIP` | `true` | Match Camoufox timezone & locale to egress IP |

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
