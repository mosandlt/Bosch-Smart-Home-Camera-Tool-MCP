"""Timestamp helpers for Bosch /v11/events data."""

from __future__ import annotations


def clean_bosch_timestamp(ts: str | None) -> str:
    """Return a Bosch event timestamp as a valid ISO-8601 string.

    Bosch `/v11/events` timestamps are offset-bearing, in Java's ZonedDateTime
    form, e.g. ``"2026-06-18T06:06:30.499+02:00[Europe/Berlin]"`` (historically
    some accounts send a trailing ``Z``). The previous code did ``ts[:19]``,
    which DROPPED the ``+02:00`` offset — an LLM/consumer reading the result
    (documented as "ISO 8601 timestamp") would then assume UTC and be off by the
    local offset (e.g. +2h in CEST). Strip only the RFC-9557 ``[zone]`` suffix
    so the explicit offset is preserved and the value stays valid ISO 8601.

    Returns ``""`` for empty/None input.
    """
    if not ts:
        return ""
    return ts.split("[", 1)[0]
