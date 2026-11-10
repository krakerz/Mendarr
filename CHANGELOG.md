# Changelog

## [Unreleased]

### Added
- FlareSolverr-compatible `/v1` API: `request.get`, `request.post` and sessions
- Builtin solver that waits out Cloudflare challenges and clicks the Turnstile checkbox
- Cookie cache: a repeat request reuses cleared cookies after one check, without a browser
- `GET /v1/egress` reports the worker's public IP
- Settings for cookie TTL, cookie check mode, click method and egress check URL
- Per-domain solver plugins with versions, stored in `data/store.db` and `data/plugins/`
- Plugins are checked against an import and API allowlist before they are stored and before they run
- A domain plugin that keeps failing is rolled back to its previous version and an incident is opened
- Plugins can choose the Chromium-based patchright browser instead of Camoufox
- `mendarr plugins` and `mendarr incidents` commands
- A plugin that blocks the worker is stopped, and the worker restarts if it doesn't stop
- The browser worker requires a bearer token when `MENDARR_WORKER_TOKEN` is set
- Healer: failed solves open an incident, and a local LLM writes a replacement plugin that is promoted only after it passes a replay
- Incident lifecycle with retry windows, backoff, a daily round cap and escalation to manual solving
- Failure traces (screenshot, trimmed DOM, console and network summary) kept per domain
- LLM endpoint profiles for OpenAI-compatible servers such as LM Studio, with fallback order
- Discord and JSON webhook notifications for healed, manual, rollback and proposed default plugin events
- `mendarr.yml` config file with per-domain overrides
- CLI: `mendarr incidents show/retry/close`, `mendarr healer check`, `mendarr notify test`
- Web dashboard at /ui with login, overview, incidents, plugins, cookies and settings pages
- Incident pages show the failure trace (screenshot, DOM, network) and the healer's prompts and replies
- Plugins can be compared, promoted, rolled back, rejected, edited and test-run from the dashboard
- LLM endpoints, healer budgets and notifications can be edited and tested from the dashboard
- MENDARR_UI_USER and MENDARR_UI_PASSWORD set the dashboard login
- Manual solve from the dashboard with a live noVNC view of the worker's browser
- Manual solves are recorded as a demonstration that the healer uses when writing a plugin
- `hold_requests`: requests for a domain awaiting a manual solve wait for it up to maxTimeout
- MCP endpoint `/mcp` for external agents (Claude Code, Codex and others): read incidents, traces and plugins, test a plugin, and submit one that is promoted only after it passes a replay
- `agent_wanted` notification when an incident waits for an agent
- Upstream proxy for all worker traffic: `MENDARR_PROXY_URL` and `MENDARR_PROXY_TYPE` (`http`, `https`, `socks5`), also read as `PROXY_URL` and `PROXY_TYPE`
- `GET /v1/egress` and the dashboard overview show the upstream proxy

### Changed
- The VNC view uses the worker's RPC port and token; websockify and ports 6081/6082 are gone
- The example compose file no longer includes gluetun; point `MENDARR_PROXY_URL` at your own proxy or VPN instead
- The cookie check resolves DNS through SOCKS5 proxies
- Proxy credentials no longer appear in cookie cache entries

## [0.1.0] — 2026-10-30

### Added
- Project scaffold with API and browser worker images
- `GET /` and `GET /health` endpoints
- Worker per-slot virtual display with VNC and websockify
- `mendarr-worker smoke` browser self-check
- Example compose file with gluetun VPN
