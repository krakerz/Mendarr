# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
