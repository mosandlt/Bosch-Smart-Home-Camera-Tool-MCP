"""Local data interface: cloud status lookup and local stream URL helper.

Status endpoint (per camera, GET):
  200 {"username": str}  -> active
  404                    -> inactive
  449                    -> firmware not supported
Anything else (other status, network error, malformed body) keeps the last
known value. Only Gen2 cameras on firmware >= MIN_FIRMWARE are queried.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import threading
from typing import Any, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

STATUS_PATH = "onvif_user"
MIN_FIRMWARE: tuple[int, ...] = (9, 40, 105)
LOCAL_USERNAME = "localuser"
LOCAL_PORT = 9554

STATE_ACTIVE = "active"
STATE_INACTIVE = "inactive"
STATE_UNSUPPORTED = "unsupported"
STATE_UNKNOWN = "unknown"

_PASSWORD_MAX_LEN = 128
_PASSWORD_RE = re.compile(r"[\x21-\x7e]+")

# Last known state per camera id; a failed poll must not flip the state.
_last_state: dict[str, str] = {}
_last_lock = threading.Lock()


def parse_firmware(version: object) -> Optional[tuple[int, ...]]:
    """Parse a dotted numeric firmware string; None for anything else."""
    if not isinstance(version, str):
        return None
    parts = version.strip().split(".")
    # Length cap: int() raises ValueError on absurdly long digit strings.
    if not all(p.isascii() and p.isdigit() and len(p) <= 9 for p in parts):
        return None
    return tuple(int(p) for p in parts)


def is_queryable(model: object, firmware: object) -> bool:
    """True for Gen2 cameras whose firmware is at or above MIN_FIRMWARE."""
    if not isinstance(model, str) or not model.startswith("HOME_"):
        return False
    parsed = parse_firmware(firmware)
    return parsed is not None and parsed >= MIN_FIRMWARE


def state_from_response(status: int, body: object) -> Optional[str]:
    """Map an HTTP result to a state, or None to keep the last value."""
    if status == 200:
        if isinstance(body, dict) and isinstance(body.get("username"), str):
            return STATE_ACTIVE
        return None
    if status == 404:
        return STATE_INACTIVE
    if status == 449:
        return STATE_UNSUPPORTED
    return None


def fetch_state(
    session: requests.Session, cloud_api: str, cam_id: str, model: object, firmware: object
) -> str:
    """Return the camera's local data interface state (no request when not queryable)."""
    if not is_queryable(model, firmware):
        return STATE_UNSUPPORTED
    with _last_lock:
        last = _last_state.get(cam_id, STATE_UNKNOWN)
    try:
        r = session.get(f"{cloud_api}/v11/video_inputs/{cam_id}/{STATUS_PATH}", timeout=10)
        try:
            body: object = r.json()
        except ValueError:
            body = None
        new = state_from_response(r.status_code, body)
    except requests.RequestException as exc:
        logger.debug("local data status poll failed: %s", type(exc).__name__)
        return last
    if new is None:
        return last
    with _last_lock:
        _last_state[cam_id] = new
    return new


def get_password(cam_name: str, cam_info: dict[str, Any]) -> Optional[str]:
    """Sticker password from config (``local_data_password``) or env; None if unset/invalid.

    Env: BOSCH_CAMERA_LDI_PASSWORD_<NAME> (name upper-cased, non-alphanumerics -> "_").
    """
    env_key = "BOSCH_CAMERA_LDI_PASSWORD_" + re.sub(r"[^A-Za-z0-9]", "_", cam_name).upper()
    raw = os.environ.get(env_key) or cam_info.get("local_data_password") or ""
    if not isinstance(raw, str):
        return None
    pw = raw.strip()
    if not pw or len(pw) > _PASSWORD_MAX_LEN or not _PASSWORD_RE.fullmatch(pw):
        return None
    return pw


def safe_lan_ip(value: object) -> Optional[str]:
    """Validated private IPv4 address, or None."""
    if not isinstance(value, str):
        return None
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if ip.version != 4 or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return None
    if not ip.is_private:
        return None
    return str(ip)


def build_url(ip: str, password: Optional[str]) -> str:
    """Local RTSPS URL; the password is URL-quoted, or masked when None."""
    secret = quote(password, safe="") if password is not None else "***"
    return f"rtsps://{LOCAL_USERNAME}:{secret}@{ip}:{LOCAL_PORT}/live"
