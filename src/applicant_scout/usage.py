"""Consent-gated usage milestones; never accepts player or configuration data."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import threading
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from .atomic_io import atomic_write_text


USAGE_ENDPOINT = "https://applicantscout-usage.applicantscout-usage-service.workers.dev/v1/events"
DEFAULT_USAGE_CONSENT = True
USAGE_EVENTS = frozenset(
    {"consent_started", "version_seen", "setup_completed", "addon_received", "wcl_result"}
)
USAGE_STATE_FILENAME = "usage.json"
USAGE_TEST_MARKER_FILENAME = "usage-test-installation"
_VERSION = re.compile(r"(?:0|[1-9][0-9]{0,4})(?:\.(?:0|[1-9][0-9]{0,4})){2}\Z", re.ASCII)
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
_MAX_QUEUE = 32
_MAX_SEEN = 512
_MAX_STATE_BYTES = 65536
_RETRY_DELAYS = (1.0, 5.0)


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def valid_usage_endpoint(endpoint: str) -> bool:
    """Require a plain HTTPS destination without embedded credentials or metadata."""
    try:
        url = urlsplit(endpoint)
        return bool(
            endpoint == endpoint.strip()
            and not any(ord(char) < 33 for char in endpoint)
            and url.scheme == "https"
            and url.hostname
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
            and url.port in (None, 443)
        )
    except ValueError:
        return False


def _production_runtime() -> bool:
    return bool(getattr(sys, "frozen", False)) and not any(
        os.environ.get(key)
        for key in ("CI", "GITHUB_ACTIONS", "PYTEST_CURRENT_TEST", "APSCOUT_USAGE_DISABLED")
    )


def _send_event(endpoint: str, payload: dict[str, str | int]) -> int:
    # No cookies, proxy credentials, redirects, or response body are needed.
    with httpx.Client(
        timeout=httpx.Timeout(3.0, connect=3.0, pool=1.0),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "ApplicantScout-Usage/1"},
    ) as client:
        with client.stream("POST", endpoint, json=payload) as response:
            return response.status_code


class UsagePersistenceError(RuntimeError):
    """The local usage preference could not be saved; reporting stays off."""


class UsageClient:
    """Nonblocking, bounded reporting of allowlisted milestones after consent.

    Opt-out discards queued events and retries. An HTTP request already handed to
    the transport can finish. No offline payload queue is persisted. Successful
    events are saved after acknowledgement; failed events are attempted at most
    once per session/day. The service must deduplicate crash-after-send retries.
    """

    def __init__(
        self,
        state_dir: Path,
        version: str,
        *,
        endpoint: str = USAGE_ENDPOINT,
        consent: bool | None = None,
        test_installation: bool = False,
        _sender: Callable[[str, dict[str, str | int]], int] | None = None,
        _day: Callable[[], str] = _utc_day,
    ) -> None:
        self._path = state_dir / USAGE_STATE_FILENAME
        self._version = version
        self._endpoint = endpoint
        self._sender = _sender or _send_event
        self._day = _day
        try:
            excluded_installation = test_installation or (state_dir / USAGE_TEST_MARKER_FILENAME).exists()
        except OSError:
            excluded_installation = True
        self._available = bool(
            valid_usage_endpoint(endpoint)
            and _VERSION.fullmatch(version)
            and not excluded_installation
            and (_sender is not None or _production_runtime())
        )
        self._condition = threading.Condition()
        self._state_lock = threading.Lock()
        self._queue: deque[tuple[str, str, int]] = deque()
        self._pending: set[tuple[str, str]] = set()
        self._attempted: dict[tuple[str, str], None] = {}
        self._seen: list[str] = []
        self._install_id = ""
        self._failed = False
        self._consent = False
        self._closed = False
        self._generation = 0
        self._thread: threading.Thread | None = None
        try:
            saved_consent = self._load()
        except (OSError, ValueError, TypeError, UnicodeError):
            saved_consent = False
            self._failed = True
        if saved_consent is None:
            initial_consent = DEFAULT_USAGE_CONSENT if consent is None else consent is True
            try:
                # Persist missing preferences before reporting; a saved opt-out
                # must survive restart even when collection is unavailable.
                self._write_state(
                    initial_consent,
                    self._install_id if initial_consent else "",
                    self._seen if initial_consent else [],
                )
            except OSError as exc:
                self._failed = True
                if consent is not None:
                    raise UsagePersistenceError(
                        "Could not save the usage preference. Reporting is off for this session."
                    ) from exc
            else:
                if initial_consent:
                    self._enable()
        elif consent is not None and consent != saved_consent:
            self.set_consent(consent)
        elif saved_consent:
            self._enable()

    @property
    def consent_enabled(self) -> bool:
        with self._condition:
            return self._consent

    @property
    def reporting_available(self) -> bool:
        return self._available

    @property
    def collection_available(self) -> bool:
        return self._available

    def set_consent(self, enabled: bool) -> None:
        with self._condition:
            consent = enabled is True
            if self._closed or (consent == self._consent and not self._failed):
                return
            # Stop immediately, including when the subsequent opt-out save fails.
            self._consent = False
            self._generation += 1
            self._queue.clear()
            self._pending.clear()
            self._attempted.clear()
            self._condition.notify_all()
        try:
            with self._state_lock:
                self._write_state(consent, "", [])
                self._install_id = ""
                self._seen = []
                self._failed = False
        except OSError as exc:
            self._failed = True
            raise UsagePersistenceError(
                "Could not save the usage preference. Reporting is off for this session."
            ) from exc
        if consent:
            self._enable()

    def _enable(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._consent = True
            if self._available and self._thread is None:
                try:
                    worker = threading.Thread(
                        target=self._run, name="usage-milestones", daemon=True
                    )
                    worker.start()
                except RuntimeError:
                    # Optional reporting must not interrupt startup or Settings
                    # when the OS cannot allocate another background thread.
                    self._failed = True
                    return
                self._thread = worker
            self._condition.notify_all()
        self.record("consent_started")
        self.record("version_seen")

    def record(self, event: str) -> bool:
        if event not in USAGE_EVENTS:
            return False
        day = self._day()
        if not _DAY.fullmatch(day):
            return False
        with self._condition:
            key = (event, day)
            if (
                not self._available or not self._consent or self._closed
                or self._failed or key in self._pending or key in self._attempted
                or len(self._queue) >= _MAX_QUEUE
            ):
                return False
            self._pending.add(key)
            self._queue.append((event, day, self._generation))
            self._condition.notify_all()
            return True

    def close(self) -> None:
        """Cancel without waiting for disk/network on the GUI thread."""
        with self._condition:
            self._closed = True
            self._generation += 1
            self._queue.clear()
            self._pending.clear()
            self._condition.notify_all()

    def _current(self, generation: int) -> bool:
        return self._consent and not self._closed and generation == self._generation

    def _load(self) -> bool | None:
        try:
            with self._path.open("r", encoding="utf-8") as source:
                raw = source.read(_MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            return None
        else:
            if len(raw.encode("utf-8")) > _MAX_STATE_BYTES:
                raise ValueError("Usage state is too large")
            state = json.loads(raw)
            if (
                not isinstance(state, dict) or type(state.get("schema")) is not int
                or state.get("schema") != 1
                or "consent" in state and type(state["consent"]) is not bool
            ):
                raise ValueError("Invalid usage state")
            if state.get("consent") is False:
                return False
            install_id = state.get("install_id", "")
            if not isinstance(install_id, str) or (
                install_id and (str(UUID(install_id)) != install_id or UUID(install_id).version != 4)
            ):
                raise ValueError("Invalid usage identity")
            seen = state.get("seen", [])
            if not isinstance(seen, list) or len(seen) > _MAX_SEEN:
                raise ValueError("Invalid usage history")
            for key in seen:
                if not isinstance(key, str) or not self._valid_seen_key(key):
                    raise ValueError("Invalid usage reservation")
            self._install_id = install_id
            self._seen = seen
            return state.get("consent")

    @staticmethod
    def _valid_seen_key(key: str) -> bool:
        parts = key.split("|")
        return bool(
            len(parts) == 3 and parts[0] in USAGE_EVENTS
            and _VERSION.fullmatch(parts[1]) and _DAY.fullmatch(parts[2])
        )

    def _prepare(self, event: str, day: str) -> bool:
        key = f"{event}|{self._version}|{day}"
        if key in self._seen:
            return False
        if event == "consent_started" and any(k.startswith("consent_started|") for k in self._seen):
            return False
        if event == "version_seen" and any(
            k.startswith(f"version_seen|{self._version}|") for k in self._seen
        ):
            return False
        if not self._install_id:
            install_id = str(uuid4())
            self._write_state(True, install_id, self._seen)
            self._install_id = install_id
        return True

    def _mark_seen(self, event: str, day: str) -> None:
        key = f"{event}|{self._version}|{day}"
        updated = [*self._seen, key]
        if len(updated) > _MAX_SEEN:
            # Keep the original enrollment marker across daily history pruning.
            first = next((k for k in updated if k.startswith("consent_started|")), None)
            updated = updated[-(_MAX_SEEN - bool(first)):]
            if first and first not in updated:
                updated.insert(0, first)
        self._write_state(True, self._install_id, updated)
        self._seen = updated

    def _write_state(self, consent: bool, install_id: str, seen: list[str]) -> None:
        state: dict[str, object] = {"schema": 1, "consent": consent}
        if consent:
            state.update(install_id=install_id, seen=seen)
        atomic_write_text(self._path, json.dumps(state), private=True)

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or bool(self._queue))
                if self._closed:
                    return
                event, day, generation = self._queue.popleft()
                if not self._current(generation):
                    continue
                self._attempted[(event, day)] = None
                if len(self._attempted) > _MAX_SEEN:
                    del self._attempted[next(iter(self._attempted))]
            try:
                with self._state_lock:
                    with self._condition:
                        if not self._current(generation):
                            continue
                    prepared = self._prepare(event, day)
                    install_id = self._install_id
                if prepared:
                    payload: dict[str, str | int] = {
                        "schema": 1, "install_id": install_id,
                        "version": self._version, "event": event, "day": day,
                    }
                    if self._deliver(payload, generation):
                        with self._state_lock:
                            with self._condition:
                                if not self._current(generation):
                                    continue
                            self._mark_seen(event, day)
            except (OSError, ValueError, TypeError, UnicodeError):
                # Corruption never silently resets the identity and inflates users.
                with self._condition:
                    if self._current(generation):
                        self._failed = True
                        self._queue.clear()
                        self._pending.clear()
                continue
            with self._condition:
                if generation == self._generation:
                    self._pending.discard((event, day))

    def _deliver(self, payload: dict[str, str | int], generation: int) -> bool:
        for attempt in range(len(_RETRY_DELAYS) + 1):
            with self._condition:
                if not self._current(generation):
                    return False
            try:
                status = self._sender(self._endpoint, dict(payload))
            except (httpx.HTTPError, OSError):
                status = 503
            if 200 <= status < 300:
                return True
            if status < 500 and status not in (408, 429):
                return False
            if attempt == len(_RETRY_DELAYS):
                return False
            with self._condition:
                if self._condition.wait_for(
                    lambda: not self._current(generation),
                    timeout=_RETRY_DELAYS[attempt],
                ):
                    return False
        return False
