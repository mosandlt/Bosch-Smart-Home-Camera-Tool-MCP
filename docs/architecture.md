# Architecture — Bosch Smart Home Camera MCP Server

## Why MCP?

The MCP (Model Context Protocol) is the standard interface for connecting LLM clients (Claude Code, Claude Desktop, Cursor, Cline, etc.) to external tools and data sources. By wrapping the Bosch Camera API as an MCP server, the LLM gains direct access to camera operations without shell-out hacks, manual prompting, or copy-pasting JSON.

## Design constraints

1. **Reuse, don't reimplement.** The sister Python CLI tool already solves the hard problems: OAuth2 PKCE flow, token rotation, FCM push, TLS-proxy, RCP protocol, Digest auth. The MCP server imports those modules; it does not duplicate them.
2. **Read-safe by default.** A misbehaving LLM should not be able to spam the API, exhaust quotas, or trigger physical actions (light, siren) without user intent. All write-tools require explicit user consent in Claude's tool-approval flow.
3. **Stateless server, stateful config.** The server process holds no session state; all persistence (token, camera registry) lives in `bosch_config.json` which is owned by the user, not the MCP process.
4. **Single-process, single-user.** Each MCP server instance is bound to one Bosch account. Multi-tenant is explicitly out of scope.
5. **Local-first.** Default transport is stdio; HTTP-based remote access is opt-in and requires explicit binding to a non-localhost interface.

## Component diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                         MCP Client (Claude)                          │
│  - sends `tools/list`, `tools/call`, `resources/read`, `prompts/get` │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │  JSON-RPC over stdio (default)
┌────────────────────────────────▼─────────────────────────────────────┐
│                     bosch_camera_mcp.server                          │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  FastMCP app                                                   │  │
│  │  - lifespan: load_config() → cache cameras + token             │  │
│  │  - tools/                                                      │  │
│  │  - resources/                                                  │  │
│  │  - prompts/                                                    │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  Adapter layer (bosch_camera_mcp.config)                       │  │
│  │  - normalises Pydantic models to API client expectations       │  │
│  │  - maps MCP errors to API errors                               │  │
│  └────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │  Python imports
┌────────────────────────────────▼─────────────────────────────────────┐
│              bosch_camera.py (sister Python CLI)                     │
│  - load_config, save_config                                          │
│  - make_session, _request_with_retry                                 │
│  - discover_cameras, resolve_cam                                     │
│  - snap_from_proxy, snap_from_local, snap_from_events                │
│  - api_ping, api_get_events, api_get_camera                          │
│  - cmd_privacy, cmd_light, cmd_pan, cmd_notifications (refactored    │
│    to return values instead of print → in v10.4.0 of the CLI)        │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │  HTTPS + OAuth2 bearer
┌────────────────────────────────▼─────────────────────────────────────┐
│              residential.cbs.boschsecurity.com                       │
│              (cloud) + Keycloak (auth)                               │
└──────────────────────────────────────────────────────────────────────┘
```

## Dependency on the sister CLI

The MCP server pins a specific minor version of the Python CLI tool. Two ways to ship this dependency:

### Option A: Git submodule
```
Bosch-Smart-Home-Camera-Tool-MCP/
└── vendor/
    └── bosch_camera_cli/   ← git submodule pinned to a CLI tag
```
- Pro: explicit version pin, full source visible
- Con: submodule UX is awkward; users need `git clone --recurse-submodules`

### Option B: Refactor CLI into a library (preferred)
Refactor the CLI's pure logic (config, token, API calls) into a `bosch_camera_lib` Python package that both the CLI and the MCP server import:

```
Bosch-Smart-Home-Camera-Tool-Python/
├── bosch_camera_lib/         ← extracted package, importable
│   ├── __init__.py
│   ├── config.py
│   ├── auth.py
│   ├── session.py
│   ├── api.py
│   ├── snapshots.py
│   └── i18n.py
└── bosch_camera.py           ← CLI wrapper using bosch_camera_lib
```

The MCP server then declares `bosch-smart-home-camera-cli >= 10.4.0` as a regular pip dependency.

This refactor is **out of scope for the initial MCP repo creation** but is the long-term direction.

### Option C (interim): import path injection
The MCP server's `__init__.py` adds a configurable path to `sys.path` pointing at the sister-CLI checkout. This is a pragmatic workaround until Option B is ready.

```python
# bosch_camera_mcp/__init__.py
import os
import sys

_cli_path = os.environ.get(
    "BOSCH_CAMERA_CLI_PATH",
    os.path.expanduser("~/code/Bosch-Smart-Home-Camera-Tool-Python"),
)
if os.path.isdir(_cli_path) and _cli_path not in sys.path:
    sys.path.insert(0, _cli_path)
```

Drawback: implicit, fragile, but acceptable for v0.1.0-alpha.

**Status (v0.4.0-alpha):** Option C is the active implementation. The adapter
`bosch_camera_mcp/adapters/cli_bridge.py` handles sys.path injection via
`ensure_cli_importable()` and exposes `get_session_and_cameras()` +
write helpers (`set_privacy_mode`, `set_light`, `set_pan`, `set_notifications`).
All 8 tools are wired and tested. Write tools were implemented as new API-call
functions in cli_bridge.py rather than calling the cmd_* functions directly —
the cmd_* functions call print()/sys.exit() and are not suitable as library calls.
Resources and prompts are implemented in `resources.py` and `prompts.py` and
imported at the bottom of `server.py` to self-register via decorator side-effects.
Refactor to Option B (library extraction) is deferred to v0.6.0.

## Resources contract

Three MCP resources are registered in `bosch_camera_mcp/resources.py`.

| URI | Type | Returns | MIME |
|---|---|---|---|
| `bosch://cameras` | Static resource | JSON string — list of camera objects | `application/json` |
| `bosch://cameras/{name}/snapshot.jpg` | Template resource | Raw JPEG bytes | `image/jpeg` |
| `bosch://cameras/{name}/events` | Template resource | JSON string — list of event objects | `application/json` |

**Registration pattern:** Resources self-register via `@mcp.resource(uri)` decorators in `resources.py`. That module is imported at the bottom of `server.py` (after the `mcp` FastMCP instance is created and all tools are defined), so the decorators execute against the correct instance without circular imports.

**Static vs. template resources:** `bosch://cameras` has no URI parameters → registered as a `FunctionResource` (appears in `mcp.list_resources()`). The `{name}` variants have URI parameters → registered as `ResourceTemplate` entries (appear in `mcp.list_resource_templates()`).

**Snapshot cache logic:** `bosch://cameras/{name}/snapshot.jpg` first checks `~/.cache/bosch-camera-mcp/snapshots/<safe_name>/` for the lexicographically latest `.jpg`. On cache hit the file is returned directly. On cache miss the `bosch_camera_snapshot` tool is called inline, which writes the new file and returns its path, which is then read back as bytes. This keeps caching logic in one place.

**Auth errors:** All three resources wrap `get_session_and_cameras()` and re-raise `reauth_required` as `MCPError(code="auth_expired")` so MCP clients receive a consistent error code regardless of whether the call originated from a tool or a resource.

## Prompts contract

Two MCP prompts are registered in `bosch_camera_mcp/prompts.py`.

| Name | Arguments | Purpose |
|---|---|---|
| `daily-camera-summary` | `hours: int = 24` | Multi-step report: iterate cameras, fetch events, summarise per type and time-slot, highlight anomalies |
| `pre-leave-check` | _(none)_ | Pre-departure routine: snapshot + scene description + anomaly flags + indoor privacy recommendation |

**Design principle:** Prompts are pure instruction templates — they return `list[UserMessage]` containing natural-language instructions that tell Claude which tools to call and in which order. They make no API calls themselves. This keeps prompts stateless, trivially testable, and reusable across transport modes.

**Message type:** Both prompts return `[UserMessage(...)]` from `mcp.server.fastmcp.prompts.base`. The `UserMessage` class accepts a plain `str` which is automatically wrapped in `TextContent(type="text", text=...)`.

**Tool references:** Tests in `test_prompts.py::TestPromptToolReferences` assert that every tool name mentioned in prompt text (`bosch_camera_list`, `bosch_camera_events`, `bosch_camera_snapshot`, `bosch_camera_privacy_set`) is actually registered on the `mcp` FastMCP instance, preventing prompt/tool name drift.

## Tool error model

Every tool returns either a success payload (Pydantic model) or raises `MCPError` with a structured reason:

```python
class MCPError(Exception):
    code: Literal["unknown_camera", "auth_expired", "api_unreachable", "permission_denied", ...]
    detail: str
    camera: Optional[str]
```

The MCP runtime translates these to JSON-RPC error responses with the `code` mapped to a friendly message. Critically, **no stack traces or Bosch API internals leak to the LLM** — that would invite prompt-injection chains.

## Auth lifecycle

1. **Startup**: server reads `bosch_config.json` (path from `--config` or env)
2. **Token check**: if `_is_token_near_expiry(token, buffer_secs=300)`, refresh now using the CLI's token endpoint
3. **Per-request**: each tool acquires the session via `make_session(token)`. On `401`, refresh once via `handle_401(cfg)` and retry. If the second attempt also fails, raise `MCPError(code="auth_expired")`.
4. **Shutdown**: persist any refreshed token back to `bosch_config.json` via `save_config(cfg)`.

The MCP server **never** initiates the OAuth browser flow. If a fresh token is needed and the refresh-token is exhausted, the server returns `MCPError(code="reauth_required", detail="run `python3 bosch_camera.py token browser` to refresh")` — the user runs the CLI's browser flow once, then restarts the MCP server.

## Snapshot return strategy

Snapshots are binary blobs (~100–400 KB JPEGs). Two return modes:

1. **`return_path=true` (default)**: server writes to `~/.cache/bosch-camera-mcp/snapshots/<camera>/<ts>.jpg`, returns `{path, method, timestamp}`. LLM can then reference the path via a follow-up resource read.
2. **`return_inline=true`**: server returns the JPEG as MCP image content (base64). Use sparingly — large payloads inflate context.

Default is `return_path` to keep the LLM context lean. Inline mode requires explicit tool argument.

## Concurrency

MCP tools run sequentially within one server instance (single asyncio event loop, FastMCP default). For commands that take >5s (`snapshot` LOCAL = ~15s), the tool returns a `Job` model with a `job_id` and exposes a `bosch_camera_job_status(job_id)` companion tool. Initial v0.1.0 uses synchronous waits with a 30s timeout; async-jobs are a v0.3.0 enhancement.

## Logging

- Default: WARNING level to stderr (visible to Claude Desktop log viewer)
- `--debug` flag: DEBUG level, redacted PII (token bytes → `***`, IPs → `1.2.3.x`)
- Never log `bosch_config.json` contents
- Log path: `~/.cache/bosch-camera-mcp/server.log` (rotating, 5 × 1 MB)

## Security boundary

The MCP server runs **with the local user's privileges** — there is no privilege escalation path. The threat model:

- **LLM hallucination**: tools have strict schemas; bad arguments → 400, never silent action
- **Prompt injection via API responses**: API responses are serialised to plain JSON; no execution path
- **Token exfiltration**: tokens never appear in tool return values; only error codes ever cross the MCP boundary
- **Network exfil**: server only talks to `residential.cbs.boschsecurity.com` and `<camera>.local` (LAN). Outbound allowlist enforced at the HTTP-client layer.

## Open questions

- Should the MCP server expose the **camera live RTSP URL** as a resource? Probably no — LLMs cannot consume RTSP, and exposing the URL leaks credentials.
- Should there be a **virtual "all cameras" pseudo-camera** for bulk operations? Maybe in v0.4.0.
- How to handle **rate limits** from the Bosch cloud? Currently the CLI has implicit DELAY=0.5s; MCP should respect the same.
- **i18n in tool descriptions** — MCP tool descriptions are LLM-facing, not user-facing. Probably keep English. But error messages going to the user should respect the locale of the parent client (Claude Code locale).
