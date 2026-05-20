"""LAN-local RCP write helpers for Gen2 cameras.

Ported from the HA integration's rcp.py (rcp_local_write / rcp_local_write_privacy /
rcp_local_write_front_light).  Uses httpx.AsyncClient so the MCP server stays on its
existing async I/O stack (no aiohttp / no requests).

Protocol: HTTPS GET to https://<cam_ip>/rcp.xml with HTTP Digest auth (WRITE direction).
Gen2 cameras (HOME_Eyes_Outdoor, HOME_Eyes_Indoor, FW 9.40.25+) only accept HTTPS on
port 443 and require Digest auth — plain HTTP on port 80 returns connection refused.
Confirmed against live Gen2 hardware 2026-05-20.
Best-effort — a False return means the caller must fall back to the cloud API.

Cred-rotation (v1.3.4): Bosch rotates Digest creds on every PUT /connection LOCAL.
Static bosch_config.json creds go stale → HTTP 401 on RCP writes.  rcp_local_write
accepts an optional ``on_401`` async callback ``() -> tuple[str, str] | None``.
On HTTP 401, the callback is awaited (it should do PUT /connection LOCAL, extract
fresh creds, and persist them); the write is then retried once with the new creds.
Cap: max 1 retry per call to avoid infinite loops.  If on_401 is absent or raises,
the 401 is silently returned as False (original best-effort behaviour).
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import ssl
from collections.abc import Callable, Coroutine
from typing import Any, Optional

import httpx

_LOGGER = logging.getLogger(__name__)

_RCP_TIMEOUT: float = 5.0

_CFG_KEY = "cam_cert_fingerprints"


class CertPinningError(Exception):
    """Raised when a camera's live TLS certificate does not match the stored fingerprint.

    Indicates cert rotation (run cert-reset) or a potential MITM attack.
    """


def _fetch_fingerprint_sync(host: str, port: int = 443, timeout: float = 3.0) -> str:
    """Synchronous TLS connect to obtain the SHA-256 certificate fingerprint.

    Used before async httpx calls so we can verify the cert before sending
    any application data.  Does not verify CA chain — cameras are self-signed.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                der: bytes = tls.getpeercert(binary_form=True)
                if not der:
                    raise CertPinningError(f"No certificate from {host}:{port}")
                return hashlib.sha256(der).hexdigest()
    except CertPinningError:
        raise
    except Exception as exc:
        raise CertPinningError(f"Cannot fetch cert from {host}:{port}: {exc}") from exc


def pin_or_verify_cam(
    host: str,
    cfg: Optional[dict[str, Any]],
    port: int = 443,
    timeout: float = 3.0,
) -> None:
    """TOFU fingerprint check for a LAN camera.

    First call for a host: stores the SHA-256 fingerprint in ``cfg[_CFG_KEY][host]``.
    Subsequent calls: compare live fingerprint to stored one; raise CertPinningError
    on mismatch.  When ``cfg`` is None, logs a warning and allows the call (legacy
    verify=False behaviour).

    Side-effect: mutates ``cfg`` in-memory when a new fingerprint is stored.
    Caller must persist cfg to disk after the call if desired.
    """
    if cfg is None:
        _LOGGER.warning("lan_rcp: no cfg passed for %s:%d — skipping fingerprint check", host, port)
        return

    stored: dict[str, str] = cfg.setdefault(_CFG_KEY, {})
    live_fp = _fetch_fingerprint_sync(host, port, timeout)

    if host not in stored:
        stored[host] = live_fp
        _LOGGER.info("lan_rcp: stored new fingerprint for %s — %s…", host, live_fp[:16])
        return

    if stored[host] != live_fp:
        raise CertPinningError(
            f"Certificate fingerprint mismatch for {host}:{port}!\n"
            f"  Stored: {stored[host]}\n  Live:   {live_fp}"
        )

# Type alias for the optional credential-rotation callback.
# Signature: async () -> tuple[str, str] | None
# Returns (new_user, new_password) on success, None on failure.
_On401Callback = Optional[Callable[[], Coroutine[Any, Any, Optional[tuple[str, str]]]]]


async def rcp_local_write(
    cam_ip: str,
    command: str,
    payload_hex: str,
    type_: str = "P_OCTET",
    num: int = 0,
    *,
    user: Optional[str] = None,
    password: Optional[str] = None,
    on_401: _On401Callback = None,
    cfg: Optional[dict[str, Any]] = None,
) -> bool:
    """Write an RCP value directly to the camera's LAN HTTPS endpoint.

    Bosch SHC Gen2 cameras listen on HTTPS port 443 only (no plain HTTP) and
    require HTTP Digest auth on rcp.xml. Pass ``user`` + ``password`` from the
    camera's local_username / local_password fields in bosch_config.json so the
    write authorises. Without credentials the camera returns HTTP 401.

    Cred-rotation: when ``on_401`` is provided and the camera returns HTTP 401,
    the callback is awaited to fetch fresh credentials (PUT /connection LOCAL).
    The write is then retried once with the new credentials.  If ``on_401`` is
    absent, raises, or returns None, the 401 is returned as False immediately.
    Cap: max 1 retry per call (no infinite loop).

    TOFU fingerprint pinning: when ``cfg`` is provided, the camera's SHA-256
    cert fingerprint is stored on first contact and verified on subsequent calls.
    Pass the loaded bosch_config.json dict; caller must persist it to disk.
    Without ``cfg`` the call degrades to verify=False (legacy behaviour).

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

    # TOFU: verify fingerprint before sending any application data
    try:
        pin_or_verify_cam(cam_ip, cfg)
    except CertPinningError:
        raise

    current_user: Optional[str] = user
    current_password: Optional[str] = password

    for attempt in range(2):  # attempt 0 = initial, attempt 1 = retry after cred-rotation
        auth = (
            httpx.DigestAuth(current_user, current_password)
            if (current_user and current_password)
            else None
        )
        try:
            async with httpx.AsyncClient(verify=False, timeout=_RCP_TIMEOUT, auth=auth) as client:  # noqa: S501 — CA skipped; fingerprint pinned above
                resp = await client.get(base, params=params)
                if resp.status_code == 200:
                    if b"<err>" in resp.content.lower():
                        _LOGGER.debug(
                            "rcp_local_write: %s@%s RCP error in response", command, cam_ip
                        )
                        return False
                    return True
                if resp.status_code == 401 and attempt == 0 and on_401 is not None:
                    # Attempt credential rotation — invoke callback, then retry
                    _LOGGER.debug(
                        "rcp_local_write: %s@%s HTTP 401 — attempting cred-rotation",
                        command,
                        cam_ip,
                    )
                    try:
                        new_creds = await on_401()
                    except Exception as cb_err:  # noqa: BLE001
                        _LOGGER.debug(
                            "rcp_local_write: on_401 callback raised: %s", cb_err
                        )
                        return False
                    if new_creds is None:
                        _LOGGER.debug(
                            "rcp_local_write: on_401 returned None — cloud unavailable"
                        )
                        return False
                    current_user, current_password = new_creds
                    continue  # retry with new creds
                _LOGGER.debug(
                    "rcp_local_write: %s@%s HTTPS %d", command, cam_ip, resp.status_code
                )
                return False
        except (httpx.TimeoutException, httpx.RequestError) as err:
            _LOGGER.debug("rcp_local_write: %s@%s %s", command, cam_ip, err)
            return False
    return False  # pragma: no cover — loop always returns before this


async def rcp_local_write_privacy(
    cam_ip: str,
    enabled: bool,
    *,
    user: Optional[str] = None,
    password: Optional[str] = None,
    on_401: _On401Callback = None,
) -> bool:
    """Write privacy-mode state via direct LOCAL RCP (Gen2 HTTPS + Digest auth).

    Best-effort fallback used when the cloud API is unreachable or the caller
    has ``prefer_local=True``.  Pass ``user`` + ``password`` from the camera's
    local credentials so the Digest challenge is answered.  Returns False when
    the camera rejects the write or is not reachable.

    Pass ``on_401`` to enable transparent credential rotation on Digest-auth
    failures (see ``rcp_local_write`` for details).

    RCP command 0x0d00 (privacy mask): 4-byte payload where byte[1] carries the
    mode (0x01 = ON, 0x00 = OFF).
    """
    payload = "00010000" if enabled else "00000000"
    return await rcp_local_write(
        cam_ip, "0x0d00", payload, "P_OCTET",
        user=user, password=password, on_401=on_401,
    )


async def rcp_local_write_front_light(
    cam_ip: str,
    brightness: int,
    *,
    user: Optional[str] = None,
    password: Optional[str] = None,
    on_401: _On401Callback = None,
) -> bool:
    """Write front-light brightness via direct LOCAL RCP (Gen2 HTTPS + Digest auth).

    ``brightness`` is 0-100 (0 = off).  Maps to RCP 0x0c22 (LED dimmer,
    T_WORD, num=1).  Pass ``user`` + ``password`` from the camera's local
    credentials so the Digest challenge is answered.

    Pass ``on_401`` to enable transparent credential rotation on Digest-auth
    failures (see ``rcp_local_write`` for details).

    Wallwasher RGB is still cloud-only (write payload too complex for the
    local RCP path).  Best-effort: False return means the caller must use cloud.
    """
    val = max(0, min(100, int(brightness)))
    payload = f"{val:04x}"
    return await rcp_local_write(
        cam_ip, "0x0c22", payload, "T_WORD", num=1,
        user=user, password=password, on_401=on_401,
    )


async def refresh_local_creds(
    cam_id: str,
    session: Any,
    cfg: dict[str, Any],
    cam_name: str,
    config_path: Optional[str],
) -> Optional[tuple[str, str]]:
    """Fetch fresh Digest credentials via PUT /v11/video_inputs/{cam_id}/connection LOCAL.

    Called as the ``on_401`` callback when rcp_local_write receives HTTP 401.
    Bosch rotates Digest creds on every PUT /connection LOCAL, so the returned
    ``user`` / ``password`` supersede whatever was stored in bosch_config.json.

    On success: updates ``cfg["cameras"][cam_name]["local_username/password"]``
    in-memory AND writes the updated config back to ``config_path`` (if provided).

    Returns ``(user, password)`` on success, ``None`` on any failure (network error,
    non-200 response, or response missing user/password fields).
    """
    from bosch_camera_mcp.adapters.cli_bridge import CLOUD_API  # noqa: PLC0415

    url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/connection"
    try:
        resp = session.put(url, json={"type": "LOCAL", "highQualityVideo": False}, timeout=10)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("refresh_local_creds: PUT /connection raised: %s", exc)
        return None

    if resp.status_code != 200:
        _LOGGER.debug(
            "refresh_local_creds: PUT /connection %d — cannot refresh", resp.status_code
        )
        return None

    try:
        data: dict[str, Any] = resp.json()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("refresh_local_creds: bad JSON in /connection response: %s", exc)
        return None

    new_user: str = data.get("user") or ""
    new_pass: str = data.get("password") or ""
    if not new_user or not new_pass:
        _LOGGER.debug(
            "refresh_local_creds: /connection response missing user/password fields"
        )
        return None

    # Persist to in-memory cfg
    cam_entry = cfg.get("cameras", {}).get(cam_name)
    if cam_entry is not None:
        cam_entry["local_username"] = new_user
        cam_entry["local_password"] = new_pass

    # Persist to disk
    if config_path:
        try:
            with open(config_path) as fh:
                on_disk: dict[str, Any] = json.load(fh)
            disk_entry = on_disk.get("cameras", {}).get(cam_name)
            if disk_entry is not None:
                disk_entry["local_username"] = new_user
                disk_entry["local_password"] = new_pass
            with open(config_path, "w") as fh:
                json.dump(on_disk, fh, indent=2)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "refresh_local_creds: could not persist creds to %s: %s", config_path, exc
            )

    _LOGGER.info(
        "refresh_local_creds: rotated Digest creds for %s (user=%r)", cam_name, new_user
    )
    return new_user, new_pass


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
