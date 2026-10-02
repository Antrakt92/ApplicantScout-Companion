from threading import Event, Thread

from applicant_scout.fetch_operation import FetchOperation


def test_retirement_prevents_blocked_worker_from_starting_network():
    operation = FetchOperation()
    arrived = Event()
    release = Event()
    calls: list[str] = []

    def worker():
        arrived.set()
        if release.wait(2) and operation.is_active():
            calls.append("network")

    thread = Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert arrived.wait(2)
        operation.retire()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert calls == []


def test_repeated_retirement_does_not_cancel_replacement_worker():
    old = FetchOperation()
    replacement = FetchOperation()
    calls: list[str] = []
    old.retire()
    old.retire()

    def worker():
        for name, operation in (("old", old), ("replacement", replacement)):
            if operation.is_active():
                calls.append(name)

    thread = Thread(target=worker, daemon=True)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert calls == ["replacement"]
    assert old is not replacement
    assert old != replacement


def test_concurrent_retirement_is_visible_after_join():
    operation = FetchOperation()
    release = Event()

    def retire():
        if release.wait(2):
            operation.retire()

    threads = [Thread(target=retire, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert not operation.is_active()
