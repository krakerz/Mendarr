# Mendarr

A self-healing, FlareSolverr-compatible Cloudflare solver.

## Description

Mendarr is a drop-in replacement for FlareSolverr that runs real headed browsers in a separate worker container, so clients like Prowlarr and Jackett can point at it unchanged.

Status: M3 healer (builtin solver, `/v1` API, per-domain plugins, self-healing with a local LLM).

Features:

- FlareSolverr-compatible `/v1` API with a cookie cache
- Per-domain solver plugins with versions and automatic rollback
- Self-healing: a failed solve opens an incident and a local LLM writes a replacement plugin, promoted only after it passes a replay
- Discord and JSON webhook notifications
- Optional upstream HTTP or SOCKS5 proxy for all worker traffic, with no direct fallback

## Installation

Copy `docker/compose.example.yml`, set `MENDARR_PROXY_URL` if the worker should leave through a proxy or VPN (see [Upstream proxy](#upstream-proxy)), and run:

```sh
docker compose up -d --build
curl localhost:8191/
```

## Building from source

```sh
uv sync --extra browser
uv run pytest
```

## Usage

- `mendarr` (or `mendarr serve`) starts the API on port 8191.
- `mendarr-worker` starts the browser worker on port 8192.
- `mendarr-worker smoke` launches a headed browser on display `:1` as a self-check.

Settings are environment variables prefixed with `MENDARR_` (`HOST`, `PORT`, `WORKER_URL`, `SLOTS`, `DATA_DIR`, `LOG_LEVEL`, `UI_USER`, `UI_PASSWORD`).

### `/v1` API

FlareSolverr-compatible JSON commands are sent as POST to `/v1`:

- `request.get`, `request.post`
- `sessions.create`, `sessions.list`, `sessions.destroy`

`GET /v1/egress` returns the public IP the worker exits through, and the upstream proxy (`proxy`, `null` when direct).

Clients must leave through the same egress as the worker. Cached cookies only work from the worker's IP.

| Variable | Default | Description |
|---|---|---|
| `MENDARR_COOKIE_CACHE` | `true` | Reuse cookies cleared by an earlier request, without a browser |
| `MENDARR_COOKIE_MAX_TTL_HOURS` | `6` | Maximum age of a cached cookie, in hours |
| `MENDARR_COOKIE_CHECK` | `curl` | How a cached cookie is checked: `curl` or `browser` |
| `MENDARR_CURL_IMPERSONATE` | `firefox` | Browser fingerprint used by the curl cookie check |
| `MENDARR_EGRESS_CHECK_URL` | `https://api.ipify.org?format=json` | URL used to look up the worker's public IP |
| `MENDARR_GEOIP` | `true` | Camoufox matches timezone, locale and geolocation to the egress IP (the proxy's exit IP when one is set; needs internet at launch) |
| `MENDARR_PROXY_URL` | empty | Upstream proxy for all worker traffic: `host:port` or `scheme://[user:pass@]host:port`. Also read as `PROXY_URL` |
| `MENDARR_PROXY_TYPE` | `http` | `http`, `https` or `socks5`, used when the URL has no scheme. Also read as `PROXY_TYPE` |
| `MENDARR_PROXY_USERNAME`, `MENDARR_PROXY_PASSWORD` | empty | Proxy credentials (HTTP(S) proxies only) |
| `MENDARR_CLICK_BACKEND` | `auto` | How the Turnstile checkbox is clicked: `auto`, `xdotool` or `mouse` |
| `MENDARR_RETURN_HEADERS` | `false` | Return response headers in `/v1` replies |
| `MENDARR_WORKER_TOKEN` | empty | Shared secret the worker requires from the API. Set the same value on both containers |
| `MENDARR_ROLLBACK_ENABLED` | `true` | Roll back a domain plugin that keeps failing |
| `MENDARR_ROLLBACK_MIN_SAMPLES` | `5` | Runs needed before a plugin version can be rolled back |
| `MENDARR_ROLLBACK_THRESHOLD` | `0.5` | Success rate below which the active version is rolled back |
| `MENDARR_ROLLBACK_WINDOW` | `20` | Recent runs per version that count |
| `MENDARR_PLUGIN_STALL_S` | `5` | Worker: a plugin blocking this long is stopped |
| `MENDARR_PLUGIN_KILL_S` | `20` | Worker: a plugin still stuck this long restarts the worker |

### Upstream proxy

The worker can send all its traffic (browsers, cookie check, egress lookup) through one proxy you run: a LAN proxy, a VPN container's built-in proxy (for example gluetun with `HTTPPROXY=on`, then `MENDARR_PROXY_URL=gluetun:8888`), or anything else that speaks HTTP or SOCKS5.

```yaml
- MENDARR_PROXY_URL=192.168.1.10:1080   # or socks5://192.168.1.10:1080
- MENDARR_PROXY_TYPE=socks5             # http (default), https or socks5
```

- A per-request `proxy` in a `/v1` call overrides it for that request.
- If the proxy is down, requests fail with an `Upstream proxy … failed` error; nothing falls back to a direct connection, and no incident is opened.
- DNS is resolved by the proxy, and WebRTC can't go around it.
- SOCKS5 with a username and password is refused at startup: the browsers can't authenticate to SOCKS5. Use an HTTP proxy for credentials, or a SOCKS5 proxy without them.
- For a hard kill switch, put the worker on a Docker network that only reaches the API and the proxy.

### Plugins

A plugin is a Python file with a `META` dict and `async def solve(page, ctx)`. Each domain can have its
own versions; requests use the domain's active version, else the active `_default`. Plugins are checked
before they are stored and again before they run: only `asyncio`, `re`, `random`, `math`, `time` and
`mendarr.sdk` can be imported. `META["backend"]` picks `camoufox` (default) or `patchright` (Chromium).

Run these inside the API container (`docker compose exec mendarr mendarr plugins …`):

- `mendarr plugins list [--scope DOMAIN] [--json]`
- `mendarr plugins show SCOPE [VERSION]`
- `mendarr plugins import FILE|-|DIR [--scope DOMAIN] [--activate]`
- `mendarr plugins export DIR [--scope DOMAIN]`
- `mendarr plugins promote SCOPE VERSION`
- `mendarr plugins rollback SCOPE`
- `mendarr plugins retire SCOPE`
- `mendarr incidents list [--json]`

Plugin files are also written to `data/plugins/<scope>/v<N>.py`.

### Self-healing

When a solve fails, Mendarr saves a trace (screenshot, trimmed DOM, console and network summary) and opens
an incident for the domain. The healer retries the active plugin, then asks a local OpenAI-compatible LLM
(such as LM Studio) for a replacement plugin. Each candidate passes the plugin checks and is replayed on the
failing URL; it is promoted only when the replay passes. If every window fails, the incident goes to manual
and you are notified. A candidate is never promoted without passing a replay, and a plugin proposed as
`_default` is never promoted automatically.

Settings live in `mendarr.yml` in the data directory (path override: `MENDARR_CONFIG_FILE`). It is reloaded when
it changes, and `${VAR}` in a value is read from the environment. See `docker/mendarr.example.yml`. A minimal setup:

```yaml
llm:
  order: [desktop]
  profiles:
    desktop: {base_url: "http://desktop.lan:1234/v1", model: gemma-4-26b-a4b, api_key: "${MENDARR_LLM_KEY}"}
notify:
  discord_webhook: "${MENDARR_DISCORD_WEBHOOK}"
```

Set `MENDARR_PUBLIC_URL` so notifications link to the dashboard. Under `domains:` a domain can turn healing off
(`enabled: false`) or override `llm_order`, `daily_rounds` and `escalation`.

Commands (inside the API container):

- `mendarr incidents list|show|retry|close`
- `mendarr healer check` (is an LLM endpoint reachable?)
- `mendarr notify test`

### Dashboard

The web dashboard is at `http://<host>:8191/ui`. Log in with `MENDARR_UI_USER` and `MENDARR_UI_PASSWORD`; the login is disabled while either is unset. `MENDARR_UI_SECRET` sets the session signing key (default: a random one kept in the data directory) and `MENDARR_UI_SESSION_HOURS` the session length.

- **Overview**: per-domain success rate, the active plugin, open incidents and the healer state.
- **Incidents**: failure traces (screenshot, DOM, network), the healer's prompts and replies, retry and close.
- **Plugins**: versions and diffs, promote, roll back, reject, edit and test-run, submit.
- **Cookies**: cached cookie names per domain (never the values) and purge.
- **Settings**: LLM endpoints with a reachability check, healer budgets, notifications with a test send, and the raw YAML.

Saving a settings form rewrites `mendarr.yml` without its comments; the Raw YAML form keeps your text as typed. Put the dashboard behind HTTPS when it is reachable beyond your LAN.

#### Manual solve

When the healer gives up, open the incident and press the manual solve button. The worker opens the URL in a real browser in a reserved slot, and the page shows a live noVNC view of it. That browser runs on the worker, through the upstream proxy if one is set, so the site sees the worker's IP. Solve the challenge there; Mendarr detects success, caches the cookies, closes the incident, frees the slot and saves what you did as a demonstration the healer uses when it writes the next plugin.

The VNC view goes through the API port (8191) only, behind the dashboard login; the worker's RPC port and token carry it from there, so no extra ports (such as 6081/6082) are published.

To make `/v1` requests for a domain wait while its manual solve is pending (up to their `maxTimeout`) instead of failing, set `hold_requests` in `mendarr.yml`:

```yaml
domains:
  example.com:
    escalation:
      manual:
        hold_requests: true
```

---

### Agents (MCP)

Any MCP client can repair plugins through the same replay gate as the healer: nothing is promoted without a passing replay.

- Enable with `MENDARR_MCP_TOKEN` (comma-separated for several agents). `MENDARR_MCP_REPLAYS_PER_HOUR` limits replays per token (default 30, 0 = unlimited).
- Endpoint: `http://<host>:8191/mcp` (streamable HTTP, `Authorization: Bearer <token>`).
- Tools:
  - `list_incidents`, `get_incident`: open incidents and their attempt history.
  - `get_trace`: failure trace with DOM excerpt and screenshot.
  - `get_plugin`: the effective plugin for a domain and its versions.
  - `get_sdk_reference`: the plugin SDK and the healer's rules.
  - `test_plugin`: try a plugin on a URL without storing it.
  - `submit_plugin`: submit a plugin for a domain with an open incident; it is promoted if the replay passes.
- With `escalation.agent_tier: on`, an escalated incident waits `agent_wait` for an agent before going manual; the `agent_wanted` event notifies you.

```
claude mcp add --transport http mendarr http://<host>:8191/mcp --header "Authorization: Bearer <token>"
```

### Notes

Built and maintained with the help of AI.
