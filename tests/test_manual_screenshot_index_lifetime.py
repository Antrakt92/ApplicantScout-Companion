import pytest

from applicant_scout import screenshot


def _key(number):
    return screenshot._ScreenshotWorkKey(f"synthetic-{number}.jpg", number, 100)


@pytest.mark.parametrize("removal", ["resolved", "scan", "prune"])
def test_resolved_generation_history_does_not_accumulate(removal):
    index = screenshot._ManualScreenshotIndex(None)
    retained = _key(0)
    index.note_manual(retained, flush=False)
    for number in range(1, 20001):
        key = _key(number)
        if removal == "resolved":
            index.note_deferred(key, flush=False)
            index.forget_deferred(key, flush=False)
        else:
            index.note_manual(key, flush=False)
            if removal == "scan":
                index.scan_view({retained})
            else:
                index.prune_missing({key}, set())
    assert index.snapshot() == {retained}
    assert list(index._insertion_order) == [retained]


def test_reintroduced_generation_has_fresh_fifo_ownership(monkeypatch):
    monkeypatch.setattr(screenshot, "_MANUAL_INDEX_MAX_KEYS", 2)
    index = screenshot._ManualScreenshotIndex(None)
    first, second, third = map(_key, (1, 2, 3))
    index.note_manual(first, flush=False)
    index.note_manual(second, flush=False)
    index.prune_missing({first}, set())
    index.note_deferred(first, flush=False)
    index.note_manual(third, flush=False)
    assert index.snapshot() == {first, third}
    assert list(index._insertion_order) == [first, third]


def test_deferred_to_manual_transition_keeps_one_history_entry(tmp_path):
    path = tmp_path / "index.json"
    index = screenshot._ManualScreenshotIndex(path)
    key = _key(1)
    index.note_deferred(key, flush=False)
    index.note_manual(key, flush=True)
    assert list(index._insertion_order) == [key]
    reloaded = screenshot._ManualScreenshotIndex(path)
    assert reloaded.snapshot() == {key}
    assert list(reloaded._insertion_order) == [key]
