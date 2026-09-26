"""Regression coverage for log throttling and quarantine collisions."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from applicant_scout import log_throttle
from applicant_scout.log_throttle import warn_once_per_interval
from applicant_scout.state import _quarantine_corrupt_file as quarantine_state
from applicant_scout.usage import _quarantine_corrupt_file as quarantine_usage
from applicant_scout.wcl import _quarantine_corrupt_file as quarantine_wcl


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _throttle_logger() -> tuple[logging.Logger, _ListHandler]:
    logger = logging.getLogger("applicant_scout.test_log_throttle")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return logger, handler


def test_warn_once_per_interval_emits_first_then_summarizes_repeats():
    log_throttle.reset_for_tests()
    logger, handler = _throttle_logger()
    try:
        clock = [1000.0]

        assert warn_once_per_interval(
            "test-key",
            "boom",
            interval_s=60,
            logger=logger,
            monotonic=lambda: clock[0],
        )
        assert len(handler.records) == 1
        assert handler.records[0].getMessage() == "boom"

        # Repeats inside the window are buffered, not emitted.
        assert not warn_once_per_interval(
            "test-key",
            "boom",
            interval_s=60,
            logger=logger,
            monotonic=lambda: clock[0] + 1.0,
        )
        assert not warn_once_per_interval(
            "test-key",
            "boom",
            interval_s=60,
            logger=logger,
            monotonic=lambda: clock[0] + 2.0,
        )
        assert len(handler.records) == 1
        assert log_throttle.suppressed_count("test-key") == 2

        # After the interval the summary emission includes the repeat count.
        assert warn_once_per_interval(
            "test-key",
            "boom",
            interval_s=60,
            logger=logger,
            monotonic=lambda: clock[0] + 61.0,
        )
        assert len(handler.records) == 2
        assert "repeated 2 times" in handler.records[1].getMessage()
        assert log_throttle.suppressed_count("test-key") == 0
    finally:
        logger.removeHandler(handler)
        log_throttle.reset_for_tests()


def test_quarantine_collision_keeps_both_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "applicant_scout.state.time.strftime", lambda *_args: "20260101-000000"
    )
    monkeypatch.setattr(
        "applicant_scout.usage.time.strftime", lambda *_args: "20260101-000000"
    )
    monkeypatch.setattr(
        "applicant_scout.wcl.time.strftime", lambda *_args: "20260101-000000"
    )

    state_path = tmp_path / "window.json"
    state_path.write_text("{bad", encoding="utf-8")
    first = quarantine_state(state_path)
    assert first is not None and first.exists()
    assert not state_path.exists()

    state_path.write_text("{bad again", encoding="utf-8")
    second = quarantine_state(state_path)
    assert second is not None and second.exists()
    assert second != first
    assert first.exists() and second.exists()

    usage_path = tmp_path / "usage.json"
    usage_path.write_text("{bad", encoding="utf-8")
    quarantine_usage(usage_path)
    leftovers = list(tmp_path.glob("usage.json.corrupt-*"))
    assert len(leftovers) == 1
    usage_path.write_text("{bad again", encoding="utf-8")
    quarantine_usage(usage_path)
    leftovers = list(tmp_path.glob("usage.json.corrupt-*"))
    assert len(leftovers) == 2
    assert not usage_path.exists()

    cache_path = tmp_path / "character-cache.json"
    cache_path.write_text("{bad", encoding="utf-8")
    first_cache = quarantine_wcl(cache_path)
    assert first_cache is not None and first_cache.exists()
    cache_path.write_text("{bad again", encoding="utf-8")
    second_cache = quarantine_wcl(cache_path)
    assert second_cache is not None and second_cache.exists()
    assert second_cache != first_cache
