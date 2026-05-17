"""Structured error type for MCP tools.

MCPError is raised by tool functions when a known failure condition occurs.
The MCP runtime surfaces code + detail to the LLM; internal stack traces and
Bosch API internals are never exposed.
"""

from __future__ import annotations

from typing import Literal, Optional

ErrorCode = Literal[
    "unknown_camera",
    "auth_expired",
    "api_unreachable",
    "permission_denied",
    "reauth_required",
    "local_unavailable",
]


class MCPError(Exception):
    """Structured MCP tool error with a machine-readable code.

    Args:
        code:   Short machine-readable reason (see ErrorCode).
        detail: Human-readable explanation surfaced to the LLM.
        camera: Optional camera name that triggered the error.
    """

    def __init__(
        self,
        code: ErrorCode,
        detail: str,
        camera: Optional[str] = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.camera = camera

    def __repr__(self) -> str:
        cam = f", camera={self.camera!r}" if self.camera else ""
        return f"MCPError(code={self.code!r}, detail={self.detail!r}{cam})"
