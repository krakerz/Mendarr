# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.7.2] — 2026-11-12

### Fixed
- The overview's "In progress" table no longer disappears on every 3 s refresh while a solve is running: its poll inherited the overview's `hx-select`. It now shows while something is in progress and hides when nothing is.

## [0.7.1] — 2026-11-11

### Fixed
- Dashboard Egress card stayed blank ("… looking up"): htmx 2.0 does not swap `hx-trigger="load"` responses for elements present on first paint, so the `/ui/egress` response was never rendered. The card now loads on first paint and re-refreshes on the same 15 s cadence as the overview.

## [0.7.0] — 2026-11-11

### Added
- Dashboard overview shows live in-progress automated solves and healer rounds with domain, details, elapsed time, and 3-second htmx polling.

## [0.6.0] — 2026-11-11

## [0.5.7] — 2026-11-11

### Fixed
- Fixed the live view taking minutes to connect (and x11vnc burning CPU while idle) on Docker hosts with a huge open-file limit (`LimitNOFILE=infinity`): x11vnc now runs with `ulimit -n 1024`, and `docker/compose.example.yml` sets `ulimits: nofile: 65536` for the browser container.

### Changed
- x11vnc runs with its defaults again (`-noxdamage -noxfixes -noxrecord` removed): those flags were added for the slow connect, whose real cause was the open-file limit.

## [0.5.6] — 2026-11-11

### Added
- Manual solve sessions get an "Attempt auto solve" button that runs the domain's solver plugin on the live page while you watch; the session stays open and you capture with "Finish & Capture". Its clicks are not recorded as your demonstration.
- The worker logs live-view timings (first x11vnc bytes, first client message, first frame sent, close) and how long the manual-solve page took to load; a failed initial navigation is now a warning. The live-view badge shows how long the connection took ("connected in 0.3s", then the first frame), and the browser console logs each step.
- `MENDARR_EGRESS_COUNTRY_URL` sets the service used to look up the exit IP's country (empty disables it).

### Changed
- A manual solve session never finishes on its own any more: cookies and the demonstration are captured only when you click "Finish & Capture" (or after an auto solve attempt, when you do).
- The manual-solve page no longer says that success is detected automatically.
- Manual solves refuse the headless Obscura backend and unknown backends with a clear error instead of "worker unavailable".
- Incidents can only be deleted once they are solved or given up.

### Fixed
- "Finish & Capture" on a page that is still a challenge or blocked now fails the session instead of caching its cookies and marking the incident solved.
- "Finish & Capture" no longer races the session watcher, and a failed manual session now releases its browser slot on the worker.
- The "Finish & Capture" button's check icon was missing from the icon sprite.
- Obscura no longer stalls when its output pipes fill up, and its process is stopped if it never becomes ready.
- A failed exit-IP lookup is remembered briefly so manual starts and solves don't each wait on a dead endpoint, and a failed country lookup is retried instead of being cached for the whole interval.
- Live-view WebSocket origin check uses the first value of a multi-proxy `X-Forwarded-Host`.

## [0.5.5] — 2026-11-11

### Fixed
- Fixed manual solve race condition where the backend watcher auto-closed the browser context and killed the VNC stream after 3 seconds when no challenge was detected.
- Fixed premature navigation by waiting for the VNC WebSocket client to connect before navigating to the target URL, ensuring the user sees the page load live.

### Added
- Added an explicit "Finish & Capture" button in the Manual Solve dashboard so users can capture clearance cookies and demonstration traces on demand.

## [0.5.4] — 2026-11-11

### Fixed
- Fixed `FpgenModelError` in `mendarr-browser` by preserving ownership of `fpgen/data` in site-packages for the runtime user.
- Disabled IPv6 via `sysctls` on `mendarr-browser` in `docker/compose.example.yml` to prevent kernel-level IPv6 PTR lookups stalling VNC.

## [0.5.3] — 2026-11-11

### Fixed
- Fixed 2–3 minute VNC connection stall on remote Docker hosts by disabling dual-stack IPv6 sockets and reverse DNS PTR lookups in `x11vnc`.

## [0.5.2] — 2026-11-11

### Changed
- Redesigned the dashboard Egress card into a structured 3-row layout: top-right country tag with flag emoji, full-width monospace IP address, and active routing status indicator dot.
- Added full country name tooltips on country badges (e.g. `Australia (AU)`).
- Cleaned up redundant protocol schemes from upstream proxy addresses in the UI.

## [0.5.1] — 2026-11-10

### Fixed
- Fixed extreme 2–3 minute delay when connecting to VNC on remote servers by disabling client reverse DNS lookups in `x11vnc` (`-nolookup -noipv6`).
- Fixed blank or frozen display in noVNC on headless `Xvfb` by disabling unsupported damage extensions (`-noxdamage -noxfixes -noxrecord`).
- Fixed reverse proxy WebSocket upgrades and origin validation when running behind an external proxy (`X-Forwarded-Host`).
- Fixed GitHub Actions build failures on `main` caused by un-lowercased repository name in GHCR tags.
- Scoped Docker Buildx cache by component (`api`, `browser`) in GitHub Actions so image builds do not invalidate each other's cache.

### Added
- Added IP country flag and ISO code display to the dashboard Egress card.
- Added live noVNC connection status diagnostics in Manual Solve.

## [0.5.0] — 2026-11-10

### Added
- FlareSolverr-compatible `/v1` API: `request.get`, `request.post`, and sessions
- Multi-engine browser worker supporting Camoufox (Firefox), Patchright (Chromium), and Obscura (Headless stealth)
- Automatic cookie cache: repeat requests reuse validated cookies without spawning a browser
- `GET /v1/egress` endpoint reporting the worker's external IP and upstream proxy status
- Per-domain solver plugins with AST-based security sandbox validation and automatic rollback
- Self-healing loop: failed challenges open incidents; local LLMs write scoped replacement plugins promoted only after passing replay validation
- Interactive Studio / direct manual solve from the web UI with live noVNC browser view
- Modernized web dashboard (`/ui`) with dark theme, persistent sidebar, incident traces (screenshots, DOM, network), and plugin editor
- "Clear finished sessions" button in Manual Solve to keep active session lists clean
- Delete plugins: delete an entire custom domain scope (falls back to `_default`) or delete specific inactive versions
- "Clear closed incidents" bulk action button and single closed incident deletion
- Redesigned Incidents and Cookies table layouts with streamlined activity timeline, browser badges, and tag formatting
- Discord notifications with mention/ping support (`@here`, `@everyone`, or custom role mention)
- Generic JSON webhook notifications with structured incident/healing payloads
- Upstream HTTP and SOCKS5 proxy support for worker traffic with no direct fallback
- Model Context Protocol (MCP) server endpoint at `/mcp` for agentic solver development and monitoring
- Credentials and dashboard user status card under Settings

### Changed
- Live manual solve stream routed through the unified API port (8191) via authenticated token bridge (retired standalone websockify ports 6081/6082)
- Reorganized YAML configuration and UI settings with environment variable interpolation (`${VAR}`)

## [0.1.0] — 2026-10-30

### Added
- Project scaffold with API and browser worker images
- `GET /` and `GET /health` endpoints
- Worker per-slot virtual display with VNC and websockify
- `mendarr-worker smoke` browser self-check
- Example compose file
