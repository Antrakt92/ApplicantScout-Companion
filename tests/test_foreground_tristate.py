"""Tri-state game-foreground regression tests: raid-join auto-hide bug.

During raid joins / loading screens GetForegroundWindow() returns NULL or an
unresolvable transient window, so the old boolean "WoW not foreground" check
hid the overlay with no other app actually focused. The foreground is now
tri-state ("game" / "other" / "unknown"); only positive "other" evidence may
auto-hide a visible overlay, and user-initiated re-opens while the game is
not positively foreground stamp a short manual latch.

Never touches real Win32 state: both the boolean and tri-state probes are
injected dict lambdas and time.monotonic is patched.
"""

from __future__ import annotations

import pytest

import applicant_scout.overlay as overlay_mod
from applicant_scout import wow_lifecycle
from applicant_scout.overlay import OverlayWindow
from applicant_scout.state import AppState
from applicant_scout.wcl import CharacterCache, WCLAuth, WCLClient


def _make_window(qtbot, tmp_path, monkeypatch, foreground, now, *, kind="none"):
    """Open-overlay harness with boolean + tri-state foreground probes."""
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
        game_foreground_state_probe=lambda: foreground["state"],
        system_ui_foreground_kind_probe=lambda: kind,
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)
    monkeypatch.setattr(overlay_mod.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(window, "_cursor_over_open_overlay", lambda: False)
    monkeypatch.setattr(window, "isActiveWindow", lambda: False)
    return window, client


def _open_overlay(qtbot, window):
    qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
    window.restore_from_launcher()
    qtbot.waitUntil(window.isVisible, timeout=1000)


def test_loading_transient_unknown_never_hides_visible_overlay(
    qtbot, tmp_path, monkeypatch
):
    """NULL/unresolvable foreground during a raid join must not hide."""
    foreground = {"active": True, "state": "game"}
    now = {"value": 1000.0}
    window, client = _make_window(qtbot, tmp_path, monkeypatch, foreground, now)
    try:
        _open_overlay(qtbot, window)

        # Raid join: no window focused, pid/name unresolvable.
        foreground["active"] = False
        foreground["state"] = "unknown"
        for _ in range(6):
            window._sync_game_foreground_visibility()
            now["value"] += overlay_mod.OPEN_OVERLAY_FOREGROUND_LOSS_GRACE_S + 0.5

        assert window.isVisible()
        assert window._launcher.isVisible()
        # The loss grace is never armed into a hide on UNKNOWN samples.
        assert window._open_overlay_foreground_loss_grace_until == 0.0
        assert window._game_foreground
    finally:
        client.close()


def test_null_hwnd_transient_without_hold_does_not_immediate_hide(
    qtbot, tmp_path, monkeypatch
):
    """Live NULL-hwnd sample (system-UI "transient", no interaction hold).

    The old system-UI branch hid immediately here whenever the overlay was
    not the active window; now a transient without a switcher class falls
    under UNKNOWN and never hides.
    """
    foreground = {"active": True, "state": "game"}
    now = {"value": 2000.0}
    auth = WCLAuth("client", "secret", tmp_path)
    client = WCLClient(auth)
    cache = CharacterCache(tmp_path)
    window = OverlayWindow(
        AppState(),
        client,
        cache,
        tmp_path,
        game_foreground_probe=lambda: foreground["active"],
        game_foreground_state_probe=lambda: foreground["state"],
        system_ui_foreground_probe=lambda: True,
        system_ui_foreground_kind_probe=lambda: "transient",
    )
    qtbot.addWidget(window)
    qtbot.addWidget(window._launcher)
    monkeypatch.setattr(overlay_mod.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(window, "_cursor_over_open_overlay", lambda: False)
    monkeypatch.setattr(window, "isActiveWindow", lambda: False)
    try:
        _open_overlay(qtbot, window)

        foreground["active"] = False
        foreground["state"] = "unknown"
        window._sync_game_foreground_visibility()
        now["value"] += overlay_mod.OPEN_OVERLAY_FOREGROUND_LOSS_GRACE_S + 0.5
        window._sync_game_foreground_visibility()

        assert window.isVisible()
        assert window._launcher.isVisible()
        assert window._open_overlay_foreground_loss_grace_until == 0.0
    finally:
        client.close()


def test_real_other_app_still_hides_after_grace(qtbot, tmp_path, monkeypatch):
    """Positive OTHER evidence keeps today's grace-then-hide contract."""
    foreground = {"active": True, "state": "game"}
    now = {"value": 3000.0}
    window, client = _make_window(qtbot, tmp_path, monkeypatch, foreground, now)
    try:
        _open_overlay(qtbot, window)

        foreground["active"] = False
        foreground["state"] = "other"
        window._sync_game_foreground_visibility()
        assert window.isVisible()
        assert (
            window._open_overlay_foreground_loss_grace_until
            == pytest.approx(
                now["value"] + overlay_mod.OPEN_OVERLAY_FOREGROUND_LOSS_GRACE_S
            )
        )

        now["value"] += overlay_mod.OPEN_OVERLAY_FOREGROUND_LOSS_GRACE_S + 0.1
        window._sync_game_foreground_visibility()

        assert not window.isVisible()
        assert not window._launcher.isVisible()
    finally:
        client.close()


def test_manual_reopen_survives_nonforeground_tick_inside_latch(
    qtbot, tmp_path, monkeypatch
):
    """Tray re-open outside the game survives ticks inside the latch."""
    foreground = {"active": False, "state": "other"}
    now = {"value": 4000.0}
    window, client = _make_window(qtbot, tmp_path, monkeypatch, foreground, now)
    try:
        monkeypatch.setattr(window, "activateWindow", lambda: None)
        window.restore_from_tray()
        qtbot.waitUntil(window.isVisible, timeout=1000)
        assert window._manual_reopen_grace_until == pytest.approx(
            now["value"] + overlay_mod.MANUAL_REOPEN_FOREGROUND_GRACE_S
        )

        # Non-foreground tick inside the latch: no hide, but the real
        # foreground state is still tracked for post-expiry recovery.
        window._sync_game_foreground_visibility()
        assert window.isVisible()
        assert window._launcher.isVisible()
        assert not window._game_foreground

        # Past the latch the ordinary loss path applies again: one tick arms
        # the short grace, the next tick past it hides.
        now["value"] = (
            window._manual_reopen_grace_until
            + overlay_mod.OPEN_OVERLAY_FOREGROUND_LOSS_GRACE_S
            + 0.1
        )
        window._sync_game_foreground_visibility()
        assert window.isVisible()
        now["value"] += overlay_mod.OPEN_OVERLAY_FOREGROUND_LOSS_GRACE_S + 0.1
        window._sync_game_foreground_visibility()
        assert not window.isVisible()
        assert not window._launcher.isVisible()
    finally:
        client.close()


def test_require_foreground_restore_succeeds_during_unknown(
    qtbot, tmp_path, monkeypatch
):
    """Badge-click restore during a loading screen no longer re-collapses."""
    foreground = {"active": True, "state": "game"}
    now = {"value": 5000.0}
    window, client = _make_window(qtbot, tmp_path, monkeypatch, foreground, now)
    try:
        _open_overlay(qtbot, window)
        window.show_launcher_only()
        assert not window.isVisible()

        # Loading screen: boolean probe drops but nothing else is focused.
        window._game_foreground = False
        foreground["active"] = False
        foreground["state"] = "unknown"
        window._launcher.show_at(window._default_launcher_position())
        assert window._launcher.isVisible()

        window.restore_from_launcher()

        assert window.isVisible()
        assert not window._collapsed_to_launcher
        # Re-opened while the game was not positively foreground: latched.
        assert window._manual_reopen_grace_until == pytest.approx(
            now["value"] + overlay_mod.MANUAL_REOPEN_FOREGROUND_GRACE_S
        )

        # Follow-up UNKNOWN ticks must not hide it again.
        now["value"] += 1.0
        window._sync_game_foreground_visibility()
        assert window.isVisible()
    finally:
        client.close()


def test_maybe_show_during_unknown_does_not_hide_visible_overlay(
    qtbot, tmp_path, monkeypatch
):
    """A decode burst during a loading screen keeps a visible overlay."""
    foreground = {"active": True, "state": "game"}
    now = {"value": 6000.0}
    window, client = _make_window(qtbot, tmp_path, monkeypatch, foreground, now)
    try:
        _open_overlay(qtbot, window)

        foreground["active"] = False
        foreground["state"] = "unknown"
        window._game_foreground = False
        window._maybe_show()

        assert window.isVisible()
        assert window._launcher.isVisible()
    finally:
        client.close()


def test_maybe_show_during_other_still_hides_visible_overlay(
    qtbot, tmp_path, monkeypatch
):
    """The UNKNOWN guard must not protect against a real other app."""
    foreground = {"active": True, "state": "game"}
    now = {"value": 7000.0}
    window, client = _make_window(qtbot, tmp_path, monkeypatch, foreground, now)
    try:
        _open_overlay(qtbot, window)

        foreground["active"] = False
        foreground["state"] = "other"
        window._game_foreground = False
        window._maybe_show()

        assert not window.isVisible()
    finally:
        client.close()


def test_snapshot_auto_show_does_not_stamp_manual_latch(
    qtbot, tmp_path, monkeypatch
):
    """Snapshot-driven _maybe_show must not arm the manual latch."""
    foreground = {"active": True, "state": "game"}
    now = {"value": 8000.0}
    window, client = _make_window(qtbot, tmp_path, monkeypatch, foreground, now)
    try:
        qtbot.waitUntil(window._launcher.isVisible, timeout=1000)
        assert window._manual_reopen_grace_until == 0.0
        window._maybe_show()
        assert window._manual_reopen_grace_until == 0.0
    finally:
        client.close()


def test_in_game_restore_does_not_stamp_manual_latch(
    qtbot, tmp_path, monkeypatch
):
    """In-game restores keep today's alt-tab hide timing (no 5s linger)."""
    foreground = {"active": True, "state": "game"}
    now = {"value": 9000.0}
    window, client = _make_window(qtbot, tmp_path, monkeypatch, foreground, now)
    try:
        _open_overlay(qtbot, window)
        assert window._manual_reopen_grace_until == 0.0

        foreground["active"] = False
        foreground["state"] = "other"
        window._sync_game_foreground_visibility()
        assert window.isVisible()
        now["value"] += overlay_mod.OPEN_OVERLAY_FOREGROUND_LOSS_GRACE_S + 0.1
        window._sync_game_foreground_visibility()
        assert not window.isVisible()
    finally:
        client.close()


class TestClassifyForegroundState:
    @pytest.mark.parametrize("hwnd", [0, None])
    def test_null_hwnd_is_unknown(self, hwnd):
        assert (
            wow_lifecycle.classify_foreground_state(hwnd, 1234, "Wow.exe")
            == "unknown"
        )

    @pytest.mark.parametrize("pid", [None, 0, -1])
    def test_unresolvable_pid_is_unknown(self, pid):
        assert (
            wow_lifecycle.classify_foreground_state(0x1234, pid, "Wow.exe")
            == "unknown"
        )

    @pytest.mark.parametrize("name", [None, ""])
    def test_unresolvable_process_name_is_unknown(self, name):
        assert (
            wow_lifecycle.classify_foreground_state(0x1234, 1234, name)
            == "unknown"
        )

    @pytest.mark.parametrize("name", ["Wow.exe", "WOW.EXE", "WowT.exe"])
    def test_wow_exe_is_game(self, name):
        assert (
            wow_lifecycle.classify_foreground_state(0x1234, 1234, name) == "game"
        )

    @pytest.mark.parametrize("name", ["chrome.exe", "Discord.exe", "explorer.exe"])
    def test_other_exe_is_other(self, name):
        assert (
            wow_lifecycle.classify_foreground_state(0x1234, 1234, name)
            == "other"
        )

    def test_is_wow_foreground_matches_game_state(self, monkeypatch):
        monkeypatch.setattr(
            wow_lifecycle, "foreground_state", lambda *_args: "game"
        )
        assert wow_lifecycle.is_wow_foreground() is True
        monkeypatch.setattr(
            wow_lifecycle, "foreground_state", lambda *_args: "other"
        )
        assert wow_lifecycle.is_wow_foreground() is False
        monkeypatch.setattr(
            wow_lifecycle, "foreground_state", lambda *_args: "unknown"
        )
        assert wow_lifecycle.is_wow_foreground() is False

    def test_foreground_state_off_windows_is_game(self, monkeypatch):
        monkeypatch.setattr(wow_lifecycle.sys, "platform", "linux")
        assert wow_lifecycle.foreground_state() == "game"

    def test_foreground_state_null_hwnd_is_unknown(self, monkeypatch):
        monkeypatch.setattr(wow_lifecycle.sys, "platform", "win32")
        monkeypatch.setattr(
            wow_lifecycle, "foreground_window_handle", lambda: None
        )
        assert wow_lifecycle.foreground_state() == "unknown"


def test_normalize_legacy_bool_probes():
    assert overlay_mod._normalize_foreground_state(True) == "game"
    assert overlay_mod._normalize_foreground_state(False) == "other"
    assert overlay_mod._normalize_foreground_state("unknown") == "unknown"
    assert overlay_mod.MANUAL_REOPEN_FOREGROUND_GRACE_S == pytest.approx(5.0)
