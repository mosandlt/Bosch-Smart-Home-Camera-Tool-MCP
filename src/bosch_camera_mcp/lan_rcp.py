"""LAN-local RCP write helpers for Gen2 cameras.

Ported from the HA integration's rcp.py (rcp_local_write / rcp_local_write_privacy /
rcp_local_write_front_light).  Uses httpx.AsyncClient so the MCP server stays on its
existing async I/O stack (no aiohttp / no requests).

Protocol: HTTPS GET to https://<cam_ip>/rcp.xml with HTTP Digest auth (WRITE direction).
Gen2 cameras (HOME_Eyes_Outdoor, HOME_Eyes_Indoor, FW 9.40.25+) only accept HTTPS on
port 443 and require Digest auth — plain HTTP on port 80 returns connection refused.
Confirmed against live Gen2 hardware 2026-05-20.
Best-effort — a False return means the caller must fall back to the cloud API.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

_LOGGER = logging.getLogger(__name__)

_RCP_TIMEOUT: float = 5.0


async def rcp_local_write(
    cam_ip: str,
    command: str,
    payload_hex: str,
    type_: str = "P_OCTET",
    num: int = 0,
    *,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> bool:
    """Write an RCP value directly to the camera's LAN HTTPS endpoint.

    Bosch SHC Gen2 cameras listen on HTTPS port 443 only (no plain HTTP) and
    require HTTP Digest auth on rcp.xml. Pass ``user`` + ``password`` from the
    camera's local_username / local_password fields in bosch_config.json so the
    write authorises. Without credentials the camera returns HTTP 401.

    Returns True on success.  ``payload_hex`` may start with ``"0x"`` or not.
    Some commands require ``num=1`` (e.g. T_WORD-typed writes like 0x0c22 LED
    dimmer); the default 0 keeps backward compatibility.
    Best-effort: any network / protocol error returns False.
    """
    base = f"https://{cam_ip}/rcp.xml"
    if not payload_hex.lower().startswith("0x"):
        payload_hex = "0x" + payload_hex
    params: dict[str, str] = {
        "command": command,
        "direction": "WRITE",
        "type": type_,
        "payload": payload_hex,
    }
    if num:
        params["num"] = str(num)
    auth = httpx.DigestAuth(user, password) if (user and password) else None
    try:
        async with httpx.AsyncClient(verify=False, timeout=_RCP_TIMEOUT, auth=auth) as client:  # noqa: S501
            resp = await client.get(base, params=params)
            if resp.status_code != 200:
                _LOGGER.debug(
                    "rcp_local_write: %s@%s HTTPS %d", command, cam_ip, resp.status_code
                )
                return False
            if b"<err>" in resp.content.lower():
                _LOGGER.debug("rcp_local_write: %s@%s RCP error in response", command, cam_ip)
                return False
            return True
    except (httpx.TimeoutException, httpx.RequestError) as err:
        _LOGGER.debug("rcp_local_write: %s@%s %s", command, cam_ip, err)
    return False


async def rcp_local_write_privacy(
    cam_ip: str,
    enabled: bool,
    *,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> bool:
    """Write privacy-mode state via direct LOCAL RCP (Gen2 HTTPS + Digest auth).

    Best-effort fallback used when the cloud API is unreachable or the caller
    has ``prefer_local=True``.  Pass ``user`` + ``password`` from the camera's
    local credentials so the Digest challenge is answered.  Returns False when
    the camera rejects the write or is not reachable.

    RCP command 0x0d00 (privacy mask): 4-byte payload where byte[1] carries the
    mode (0x01 = ON, 0x00 = OFF).
    """
    payload = "00010000" if enabled else "00000000"
    return await rcp_local_write(cam_ip, "0x0d00", payload, "P_OCTET", user=user, password=password)


async def rcp_local_write_front_light(
    cam_ip: str,
    brightness: int,
    *,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> bool:
    """Write front-light brightness via direct LOCAL RCP (Gen2 HTTPS + Digest auth).

    ``brightness`` is 0-100 (0 = off).  Maps to RCP 0x0c22 (LED dimmer,
    T_WORD, num=1).  Pass ``user`` + ``password`` from the camera's local
    credentials so the Digest challenge is answered.
    Wallwasher RGB is still cloud-only (write payload too complex for the
    local RCP path).  Best-effort: False return means the caller must use cloud.
    """
    val = max(0, min(100, int(brightness)))
    payload = f"{val:04x}"
    return await rcp_local_write(cam_ip, "0x0c22", payload, "T_WORD", num=1, user=user, password=password)


async def lan_tcp_ping(ip: str, port: int = 443, timeout: float = 1.5) -> tuple[bool, float]:
    """TCP-connect to ``ip:port`` and return ``(reachable, latency_ms)``.

    Uses ``httpx.AsyncClient`` HEAD request over HTTPS (ignoring TLS errors) so
    we probe the real HTTPS stack, not just TCP.  Falls back to a raw TCP probe
    via asyncio if httpx raises before connecting (e.g. DNS failure).

    Returns ``(False, -1.0)`` on any failure.
    """
    import asyncio
    import time

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(
            verify=False,  # noqa: S501 — LAN self-signed cert
            timeout=httpx.Timeout(timeout),
        ) as client:
            await client.head(f"https://{ip}:{port}/", follow_redirects=False)
        latency_ms = (time.monotonic() - t0) * 1000.0
        return True, round(latency_ms, 1)
    except httpx.ConnectError:
        # ConnectError includes TLS errors — we reached the host, it responded.
        latency_ms = (time.monotonic() - t0) * 1000.0
        return True, round(latency_ms, 1)
    except httpx.TimeoutException:
        return False, -1.0
    except Exception:  # noqa: BLE001 — best-effort
        # Raw asyncio fallback for any other error (DNS, etc.)
        pass

    # asyncio raw TCP fallback
    t0 = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        latency_ms = (time.monotonic() - t0) * 1000.0
        return True, round(latency_ms, 1)
    except (OSError, asyncio.TimeoutError):
        return False, -1.0
