"""Regression tests for Bosch event-timestamp offset preservation (cross-version of HA #34).

Bosch /v11/events timestamps are offset-bearing, e.g.
"2026-06-18T06:06:30.499+02:00[Europe/Berlin]". The server used to truncate
them to 19 chars (ts[:19]), dropping the "+02:00" offset, so a consumer reading
the documented "ISO 8601 timestamp" would assume UTC and be off by the local
offset. clean_bosch_timestamp strips only the RFC-9557 [zone] suffix, keeping
the offset → faithful, valid ISO 8601.
"""

from __future__ import annotations

import datetime

import pytest

from bosch_camera_mcp.time_utils import clean_bosch_timestamp

OFFSET = "2026-06-18T06:06:30.499+02:00[Europe/Berlin]"
OFFSET_CLEAN = "2026-06-18T06:06:30.499+02:00"


def test_offset_preserved_not_truncated() -> None:
    assert clean_bosch_timestamp(OFFSET) == OFFSET_CLEAN
    # The bug truncated to 19 chars, dropping the offset.
    assert clean_bosch_timestamp(OFFSET) != OFFSET[:19]


def test_cleaned_value_is_parseable_with_correct_instant() -> None:
    # fromisoformat parses the offset form → true instant 04:06:30 UTC (not 06:06).
    dt = datetime.datetime.fromisoformat(clean_bosch_timestamp(OFFSET))
    assert dt.utcoffset() == datetime.timedelta(hours=2)
    assert dt.astimezone(datetime.timezone.utc) == datetime.datetime(
        2026, 6, 18, 4, 6, 30, 499000, tzinfo=datetime.timezone.utc
    )


def test_z_suffix_passthrough() -> None:
    assert clean_bosch_timestamp("2026-03-22T14:30:00.000Z") == "2026-03-22T14:30:00.000Z"


@pytest.mark.parametrize("empty", ["", None])
def test_empty_returns_empty(empty: str | None) -> None:
    assert clean_bosch_timestamp(empty) == ""


def test_no_bracket_unchanged() -> None:
    assert clean_bosch_timestamp("2026-06-18T06:06:30+02:00") == "2026-06-18T06:06:30+02:00"
