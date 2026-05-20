"""Bosch Smart Home Camera — MCP Server.

Exposes the reverse-engineered Bosch cloud API as MCP tools, resources, and
prompts for use from Claude Code, Claude Desktop, and other MCP-compatible
clients.

Status: v1.1.0 — LAN-only media (privacy hardened). Snapshot + stream_url
go directly to camera over LAN; no Bosch cloud roundtrip for media.
"""

__version__ = "1.3.4"
