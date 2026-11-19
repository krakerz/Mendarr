# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.2.0] — 2026-11-19

### Added
- Camoufox mirror fallback: when the official Camoufox downloads fail (its GitHub repos are currently unavailable), the browser image and the Settings updater fetch Camoufox from a mirror (jo-inc/camofox-browser backups), checked against its sha256. Set the mirror with `MENDARR_CAMOUFOX_MIRROR_URL` on the worker, or the `CAMOUFOX_MIRROR_URL` build arg for the image; an empty value turns it off. The Backends table shows "via mirror".
- LLM endpoints: a status chip next to "Test connection" (e.g. "ok 200", "failed timeout"), a model dropdown filled after a successful test or save, LM Studio's loaded models marked "(loaded)", and a rank number per endpoint.
- Optional "Extra request JSON" per LLM endpoint, merged into chat requests (e.g. `chat_template_kwargs` for Qwen3 or NIM). `model`, `messages` and `stream` are reserved and can't be overridden.
- Incident page refreshes itself and shows a "Now" row with what the healer is doing, a Backend row, and the backend each heal attempt used (older incidents show it as "inferred from plugin"). Trace pages show the backend too.

### Changed
- The healer re-reads the config and picks the LLM before every attempt, so changes to the endpoint order, endpoint or key apply to the next attempt.
- The heal prompt includes the code and failure reason of the last 3 attempts.
- Settings page layout: one label column, equal field widths, and equal-height controls and buttons.
- Browser image builds download Camoufox and Chromium before copying the source, so code changes no longer re-download the browsers.
- New app icon (a bandaged ring) for the favicon, sidebar and login page, plus an Apple touch icon. The favicon is now the full coloured tile, so it stays visible on dark browser tabs.

### Fixed
- The healer's progress text showed the LLM profile, including its API key. It now shows "via <endpoint name>".
- Browser image builds no longer succeed silently without a Camoufox browser. `camoufox fetch` exits 0 on failure, so the build now checks the installed version and fails if there is none.
- The Settings updater now detects whether a Camoufox build was actually installed.

## [1.1.0] — 2026-11-18

### Added
- Settings: backend switch order for the healer's backend switches; the manual solve picker follows it
- "Browser language" on the Settings page (`solver.locale` in `mendarr.yml`, e.g. `en-US`): one language for every backend, overriding `MENDARR_LOCALE`. Empty keeps the old behaviour (`MENDARR_LOCALE`, else the exit country's main language). A browser picks a changed language up on its next launch; one with an open session or manual solve keeps its language until it is free.

- Settings → Browser backends: shows the installed and latest obscura binary and Camoufox browser build and updates them from their GitHub releases without rebuilding the image (verified with a test launch, kept on the worker's `/data/backends` volume, Revert goes back to the previous or built-in one). A Camoufox update restarts the worker once no solve, session or manual solve is open; "Restart now" does it at once and closes them. `MENDARR_BACKEND_AUTO_UPDATE=true` on the worker does it on its own (`MENDARR_BACKEND_UPDATE_INTERVAL`, default 24h). patchright's Chromium still comes with the image.

### Fixed
- An obscura solve or manual solve with its own proxy left through the worker's default proxy (obscura ignores per-context proxies). It now runs on an obscura process started with that proxy (one per slot next to the default one), and a dead request proxy fails with "Upstream proxy … failed" like on Camoufox.
- The obscura backend behind a SOCKS5 upstream proxy looked host names up itself instead of at the proxy, so sites the worker can't resolve on its own failed with "error resolving for socks proxy". It now uses `socks5h://` like Camoufox and the cookie check.
- Cookies from a healer, agent (MCP) or plugin-test replay that clears a challenge are now cached. They were thrown away, so after a fix was promoted the next client request had to solve the same challenge again with the new plugin version.
- Camoufox sometimes rendered pages in another language than the exit country's main one (e.g. Chinese on an Australian exit): its GeoIP option picks the browser language at random, weighted by the share of speakers. Mendarr now pins the country's main language (`en-AU`) on every backend (Camoufox, patchright, obscura), so every browser launch looks the same. `MENDARR_LOCALE` sets a fixed language for all of them instead.
- Saving the healer budgets on the Settings page no longer writes the current poll interval, LLM wait, give-up time and replay timeout into `mendarr.yml`. Those fields are now empty unless the file sets them, with the default as a grey placeholder, so a changed default (such as the 120 s replay timeout in 1.0.0) applies. A value saved earlier stays: clear the field and save to go back to the default.

## [1.0.0] — 2026-11-18

### Added
- Time limit for background cookie re-solves: `cookies.refresh_timeout` in `mendarr.yml` and "Solve timeout" on the Settings page (default `60s`, minimum `10s`). It was fixed at 60 s.
- Backend picker (camoufox or patchright) next to "Solve manually" on the incident page; before, a manual solve from an incident always used camoufox.
- Default backend for automated solves: `MENDARR_DEFAULT_BACKEND` (`camoufox`, `patchright` or `obscura`; default `camoufox`), `solver.default_backend` in `mendarr.yml` and a "Solver" card on the Settings page. It applies to every plugin that sets no `backend` in its `META`, including the builtin `_default`; a plugin's own backend always wins. The manual-solve pickers preselect it when it is camoufox or patchright. Plugins without a backend show `default` in the Backend column.

### Changed
- Healer, agent (MCP) and plugin-test replays get 120 s by default (`healer.replay_timeout_s`, "Replay timeout" on the Settings page) instead of 60 s, which cut slow challenges off before a client's own 90–150 s limit.

### Fixed
- A client that sends the same proxy Mendarr already uses as `MENDARR_PROXY_URL` (e.g. Jackett with its proxy setting) now shares the cookie cache with it: its cookies are stored under the proxy's exit IP instead of a separate `proxy:` key, so the background cookie refresher keeps them fresh instead of skipping them as "other egress". Entries cached under the old `proxy:` key are no longer used; delete them on the Cookies page. Update the API and the browser worker together: the worker now reports its proxy username so both sides pick the same cache key.

### Docs
- The README now documents `healer.replay_timeout_s`, the time limit for healer, agent (MCP) and plugin-test replays shown in incident details.

## [0.9.0] — 2026-11-13

### Added
- Update checker: a "New version available" card in the dashboard sidebar (dismiss it per version, or open the release notes), a warning when the browser worker runs a different version than the API, and an optional "Update available" Discord/webhook notification (off by default, sent once per version). It asks GitHub for the latest published release once per interval, which sends your public IP to GitHub; turn it off with `MENDARR_UPDATE_CHECK=false`. `MENDARR_UPDATE_CHECK_INTERVAL` (default `24h`, minimum `1h`), `updates:` in `mendarr.yml` and an "Updates" section on the Settings page with a "Check now" button.

## [0.8.1] — 2026-11-13

### Fixed
- Cookie labels on the Cookies page lost the spaces between the cookie name, its expiry and "sets lifetime" (e.g. "cf_clearancein 364d").

### Changed
- Slim scrollbars in the dashboard's accent colour instead of the browser's default grey bars (Firefox shows a thin bar in the same colours).

## [0.8.0] — 2026-11-13

### Added
- Background cookie refresher (`MENDARR_COOKIE_REFRESH_INTERVAL`, `MENDARR_COOKIE_REFRESH_IDLE_DAYS`, `cookies:` in `mendarr.yml`, Settings page): first round 60 s after start, background checks and solves keep one browser slot free, and the last check result is kept across restarts.
- `auto` cache lifetime (`MENDARR_COOKIE_MAX_TTL_HOURS=auto` or `0`): a cached entry lives as long as its `cf_clearance` cookie.
- Per-cookie expiry on the Cookies page.
- One log line per request.
- Domains that were requested but never showed a challenge appear in the Domains table as "no challenge".
- Counter for cookie check/browser mismatches.
- Stale marker on the Egress card.

### Changed
- The exit IP is refreshed in the background; requests no longer wait for the lookup.
- The `debug` log level no longer enables library debug output.

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
