# Bosch Smart Home Camera — MCP Server

> **Model Context Protocol (MCP) server** that exposes the Bosch Smart Home Camera cloud API
> as MCP tools. Drop-in for Claude Code, Claude Desktop, and any MCP-compatible client.
> Reuses the proven reverse-engineered API client from the sister
> [Python CLI tool](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-Python).
>
> **Status:** concept / draft (v0.1.0-alpha — not yet released)

[![License][license-shield]](LICENSE)
[![Project Maintenance][maintenance-shield]][user_profile]

[license-shield]: https://img.shields.io/badge/license-MIT-blue.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/badge/maintainer-%40mosandlt-blue.svg?style=for-the-badge
[user_profile]: https://github.com/mosandlt

## Disclaimer

This project is an independent, community-developed tool. It is **not affiliated with, endorsed by, sponsored by, or in any way officially connected to Robert Bosch GmbH, Bosch Smart Home GmbH, or any of their subsidiaries or affiliates**. "Bosch", "Bosch Smart Home", and related names and logos are registered trademarks of Robert Bosch GmbH.

The tool communicates with a reverse-engineered, undocumented, unofficial API. Provided "as is", without warranty of any kind. Use entirely at your own risk.

## Why a separate MCP server?

The sister projects target different runtimes:

| Project | Runtime | User-facing surface |
|---|---|---|
| [HA Integration](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant) | Home Assistant | UI entities, Lovelace card, automations |
| [Python CLI](https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-Python) | terminal | `bosch_camera ...` commands |
| [ioBroker Adapter](https://github.com/mosandlt/iobroker.bosch-smart-home-camera) | ioBroker | datapoints, JSON-config admin UI |
| **MCP Server (this repo)** | **Claude clients** | **MCP tools callable from LLMs** |

LLM use-cases the existing sisters don't cover:
- "Take a snapshot of the garden camera and describe what you see."
- "What was the last motion event on the terrace, and at what time?"
- "Enable privacy mode on the indoor camera until 22:00, then disable it."
- "Pan the 360° camera to the left and grab a snapshot."
- "Summarise today's motion events across all cameras."

These flows require an LLM in the loop — which is exactly what MCP is for.

## Architecture

```
┌─────────────────────────┐      stdio / SSE / streamable HTTP      ┌─────────────────────────┐
│  Claude Code / Desktop  │ ←─────────────────────────────────────→ │  bosch-smart-home-      │
│  (MCP host)             │             MCP protocol                │  camera-mcp server      │
└─────────────────────────┘                                         └────────────┬────────────┘
                                                                                 │
                                                              imports / shared API client
                                                                                 │
                                                                                 ▼
                                                                  ┌─────────────────────────┐
                                                                  │ bosch_camera.py         │
                                                                  │ (sister Python CLI tool)│
                                                                  └────────────┬────────────┘
                                                                               │ HTTPS (OAuth2 PKCE)
                                                                               ▼
                                                                  ┌─────────────────────────┐
                                                                  │ residential.cbs.bosch-  │
                                                                  │ security.com (cloud)    │
                                                                  └─────────────────────────┘
```

The MCP server is a thin wrapper around the Python CLI's API layer. It does **not** re-implement OAuth, token refresh, FCM push, RTSP, or RCP — it imports them.

## Planned MCP tools (v0.1.0)

| Tool | Description | Returns |
|---|---|---|
| `bosch_camera_list` | List all configured cameras | array of `{id, name, model, hw_version, status}` |
| `bosch_camera_status` | Get online/offline + privacy state for one camera | `{name, status, privacy_mode, light_on, last_event_at}` |
| `bosch_camera_snapshot` | Capture a fresh snapshot (cloud or local) | `{path, method, timestamp}` + optional inline image content |
| `bosch_camera_events` | List recent motion/person/audio events | array of `{event_id, type, timestamp, has_clip}` |
| `bosch_camera_privacy_set` | Turn privacy mode on/off | `{camera, privacy_mode}` |
| `bosch_camera_light_set` | Turn camera spotlight on/off (Gen1+Gen2) | `{camera, light_on}` |
| `bosch_camera_pan` | Pan the 360° camera | `{camera, position}` |
| `bosch_camera_notifications_set` | Toggle push notifications | `{camera, notifications_on}` |
| `bosch_camera_info` | Verbose camera info (firmware, IP, stream URLs) | full dict |

Tools intentionally NOT exposed to LLMs (write-risky / time-consuming):
- Live RTSP stream URLs (no LLM use case)
- Token refresh (handled silently by the underlying client)
- Camera sharing / friends (require user-driven flow)
- Cloud clip download (large payloads)
- Audio intercom (timing-sensitive)

## Planned MCP resources

| Resource URI | Description |
|---|---|
| `bosch://cameras` | JSON list of configured cameras |
| `bosch://cameras/{name}/snapshot.jpg` | Latest cached snapshot |
| `bosch://cameras/{name}/events` | Recent events JSON |

## Planned MCP prompts

| Prompt | Description |
|---|---|
| `daily-camera-summary` | Walk through today's events on all cameras and summarise |
| `pre-leave-check` | Cycle through cameras: snapshot each, list anomalies |

## Auth model

Server runs **with the user's existing `bosch_config.json`** from the sister Python tool — no separate OAuth flow. Two startup modes:

1. `--config-from-cli` (default): expects `bosch_config.json` next to `bosch_camera.py` in a sibling checkout
2. `--config <path>`: explicit path to a `bosch_config.json`

The MCP server never reads or writes credentials beyond what the CLI tool already does (token refresh on 401, atomic save).

## Transport

- **stdio** (default for Claude Code / Desktop)
- **streamable HTTP** (planned v0.2.0) for remote access from non-local hosts

## Tech stack

- Python 3.10+
- [`mcp`](https://github.com/modelcontextprotocol/python-sdk) — official Anthropic Python SDK
- `pydantic` (already a transitive dep of `mcp`) for tool schemas
- Reuse: `bosch_camera.py` from sister repo as a Git submodule **or** as a Python import path

## Installation (planned)

```bash
pipx install bosch-smart-home-camera-mcp
# or
uvx bosch-smart-home-camera-mcp
```

Add to Claude Code:

```bash
claude mcp add bosch-camera -- bosch-smart-home-camera-mcp \
  --config ~/.config/bosch-camera/bosch_config.json
```

## Repo layout

```
Bosch-Smart-Home-Camera-Tool-MCP/
├── README.md                         this file
├── LICENSE                           MIT
├── pyproject.toml                    build + tool config
├── requirements.txt                  runtime pins (mcp, etc.)
├── requirements-test.txt             pytest, pytest-asyncio, mocks
├── src/
│   └── bosch_camera_mcp/
│       ├── __init__.py
│       ├── server.py                 FastMCP server entrypoint
│       ├── tools/                    one file per tool group
│       │   ├── cameras.py
│       │   ├── snapshots.py
│       │   ├── events.py
│       │   └── controls.py
│       ├── resources.py
│       ├── prompts.py
│       └── config.py                 config loader, bridging to sister CLI
├── tests/
│   ├── conftest.py
│   └── test_tools_*.py
├── docs/
│   ├── architecture.md
│   ├── tools-reference.md
│   └── installation.md
└── .gitignore
```

## Roadmap

- **v0.1.0** — concept doc + skeleton server, all tools defined but not yet implemented (returns `NotImplementedError`)
- **v0.2.0** — read-only tools wired to live Bosch API (list, status, events, snapshot)
- **v0.3.0** — write tools (privacy, light, pan, notifications)
- **v0.4.0** — resources + prompts
- **v0.5.0** — streamable-HTTP transport, packaging for `pipx`/`uvx`
- **v1.0.0** — published to PyPI

## License

MIT — see [LICENSE](LICENSE).
