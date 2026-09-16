"""Windows approval checks use an isolated registry double, never the user profile."""

from contextlib import nullcontext
from pathlib import Path
import sys

import pytest

from applicant_scout import wow_lifecycle


def _blob(state: int) -> bytes:
    return state.to_bytes(4, "little") + bytes(8)


class _Registry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_BINARY = 3

    def __init__(self):
        self.values = {"Other program.lnk": (_blob(3), self.REG_BINARY)}
        self.opens = []
        self.deleted = []
        self.missing_key = False
        self.deny_read = False
        self.deny_write = False
        self.ignore_delete = False
        self.write_open_value = None

    def OpenKey(self, root, path, reserved, access):
        assert root == self.HKEY_CURRENT_USER
        assert path == wow_lifecycle._STARTUP_APPROVAL_KEY
        assert reserved == 0
        self.opens.append(access)
        if self.missing_key:
            raise FileNotFoundError
        if self.deny_read or (access & self.KEY_SET_VALUE and self.deny_write):
            raise PermissionError("denied")
        if access & self.KEY_SET_VALUE and self.write_open_value is not None:
            self.values[wow_lifecycle.STARTUP_SHORTCUT_NAME] = self.write_open_value
        return nullcontext(self)

    def QueryValueEx(self, key, name):
        assert key is self
        assert name == wow_lifecycle.STARTUP_SHORTCUT_NAME
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name]

    def DeleteValue(self, key, name):
        assert key is self
        assert name == wow_lifecycle.STARTUP_SHORTCUT_NAME
        self.deleted.append(name)
        if not self.ignore_delete:
            del self.values[name]


@pytest.fixture
def approval(monkeypatch, tmp_path):
    shortcut = tmp_path / wow_lifecycle.STARTUP_SHORTCUT_NAME
    shortcut.write_bytes(b"test shortcut")
    registry = _Registry()
    monkeypatch.setattr(wow_lifecycle.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(wow_lifecycle, "startup_shortcut_path", lambda: shortcut)
    return shortcut, registry


@pytest.mark.parametrize("state,expected", [(1, "disabled"), (2, "enabled"), (3, "disabled"), (6, "enabled"), (7, "disabled"), (8, "enabled"), (9, "disabled")])
def test_reads_known_approval_without_mutation(approval, state, expected):
    shortcut, registry = approval
    registry.values[shortcut.name] = (_blob(state), registry.REG_BINARY)
    before = registry.values.copy()

    assert wow_lifecycle.wow_sync_startup_state(shortcut_path=shortcut) == expected
    assert registry.values == before
    assert registry.opens == [registry.KEY_QUERY_VALUE]
    assert registry.deleted == []


@pytest.mark.parametrize("missing_key", [False, True])
def test_missing_registry_entry_means_existing_shortcut_is_enabled(approval, missing_key):
    _shortcut, registry = approval
    registry.missing_key = missing_key
    assert wow_lifecycle.wow_sync_startup_state() == "enabled"
    assert registry.deleted == []


def test_missing_shortcut_does_not_query_registry(approval):
    shortcut, registry = approval
    shortcut.unlink()
    assert wow_lifecycle.wow_sync_startup_state() == "missing"
    assert registry.opens == []


def test_enable_requires_existing_shortcut(approval):
    shortcut, registry = approval
    registry.values[shortcut.name] = (_blob(3), registry.REG_BINARY)
    shortcut.unlink()
    with pytest.raises(OSError, match="missing"):
        wow_lifecycle.enable_wow_sync_startup_approval()
    assert registry.opens == []
    assert registry.deleted == []


def test_directory_in_place_of_shortcut_is_unknown(approval):
    shortcut, registry = approval
    shortcut.unlink()
    shortcut.mkdir()
    assert wow_lifecycle.wow_sync_startup_state() == "unknown"
    assert registry.opens == []


@pytest.mark.parametrize("value,value_type", [(b"", 3), (_blob(2)[:-1], 3), (_blob(2) + b"x", 3), (_blob(255), 3), (_blob(2), 1), ("enabled", 3), (None, 3)])
def test_malformed_registry_values_are_unknown(approval, value, value_type):
    shortcut, registry = approval
    registry.values[shortcut.name] = (value, value_type)
    assert wow_lifecycle.wow_sync_startup_state() == "unknown"
    with pytest.raises(OSError, match="unknown"):
        wow_lifecycle.enable_wow_sync_startup_approval()
    assert registry.deleted == []


def test_registry_permission_failure_is_unknown(approval):
    _shortcut, registry = approval
    registry.deny_read = True
    assert wow_lifecycle.wow_sync_startup_state() == "unknown"


def test_shortcut_permission_failure_is_unknown(approval, monkeypatch):
    shortcut, registry = approval
    original_stat = Path.stat

    def denied_stat(path, *args, **kwargs):
        if path == shortcut:
            raise PermissionError("denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    assert wow_lifecycle.wow_sync_startup_state() == "unknown"
    assert registry.opens == []


def test_other_shortcut_cannot_change_another_program(approval):
    shortcut, registry = approval
    other = shortcut.with_name("Other program.lnk")
    other.write_bytes(b"other")
    assert wow_lifecycle.wow_sync_startup_state(shortcut_path=other) == "unknown"
    with pytest.raises(OSError, match="unknown"):
        wow_lifecycle.enable_wow_sync_startup_approval(shortcut_path=other)
    assert registry.opens == []


def test_unsupported_platform_never_reads_or_writes_registry(approval, monkeypatch):
    _shortcut, registry = approval
    monkeypatch.setattr(wow_lifecycle.sys, "platform", "linux")
    assert wow_lifecycle.wow_sync_startup_state() == "unsupported"
    with pytest.raises(OSError, match="unsupported"):
        wow_lifecycle.enable_wow_sync_startup_approval()
    assert registry.opens == []


def test_explicit_enable_removes_only_own_disabled_entry_and_verifies(approval):
    shortcut, registry = approval
    registry.values[shortcut.name] = (_blob(1), registry.REG_BINARY)
    other_before = registry.values["Other program.lnk"]

    wow_lifecycle.enable_wow_sync_startup_approval()

    assert registry.deleted == [shortcut.name]
    assert registry.values == {"Other program.lnk": other_before}
    assert wow_lifecycle.wow_sync_startup_state() == "enabled"
    assert registry.opens[-1] == registry.KEY_QUERY_VALUE


def test_enable_already_enabled_is_read_only(approval):
    _shortcut, registry = approval
    wow_lifecycle.enable_wow_sync_startup_approval()
    assert registry.opens == [registry.KEY_QUERY_VALUE]
    assert registry.deleted == []


def test_enable_fails_honestly_when_registry_write_is_denied(approval):
    shortcut, registry = approval
    registry.values[shortcut.name] = (_blob(3), registry.REG_BINARY)
    registry.deny_write = True
    with pytest.raises(OSError, match="Windows Settings"):
        wow_lifecycle.enable_wow_sync_startup_approval()
    assert registry.deleted == []
    assert wow_lifecycle.wow_sync_startup_state() == "disabled"


def test_enable_requires_post_write_enabled_state(approval):
    shortcut, registry = approval
    registry.values[shortcut.name] = (_blob(3), registry.REG_BINARY)
    registry.ignore_delete = True
    with pytest.raises(OSError, match="did not become enabled"):
        wow_lifecycle.enable_wow_sync_startup_approval()


def test_changed_unknown_value_is_not_deleted(approval):
    shortcut, registry = approval
    registry.values[shortcut.name] = (_blob(3), registry.REG_BINARY)
    registry.write_open_value = (b"unexpected", registry.REG_BINARY)
    with pytest.raises(OSError, match="Windows Settings"):
        wow_lifecycle.enable_wow_sync_startup_approval()
    assert registry.deleted == []


def test_concurrent_enable_is_preserved(approval):
    shortcut, registry = approval
    registry.values[shortcut.name] = (_blob(3), registry.REG_BINARY)
    registry.write_open_value = (_blob(2), registry.REG_BINARY)
    wow_lifecycle.enable_wow_sync_startup_approval()
    assert registry.deleted == []
    assert wow_lifecycle.wow_sync_startup_state() == "enabled"


@pytest.mark.parametrize("restore", [False, True])
def test_configure_restores_approval_only_when_explicit(approval, monkeypatch, restore):
    shortcut, registry = approval
    registry.values[shortcut.name] = (_blob(3), registry.REG_BINARY)
    operations = []

    def create_shortcut(**kwargs):
        operations.append("created")
        kwargs["shortcut_path"].write_bytes(b"new shortcut")

    monkeypatch.setattr(wow_lifecycle, "_create_shortcut", create_shortcut)
    result = wow_lifecycle.configure_wow_sync_startup(True, restore_windows_approval=restore)
    assert result == shortcut
    assert operations == ["created"]
    assert wow_lifecycle.wow_sync_startup_state() == ("enabled" if restore else "disabled")
    assert registry.deleted == ([shortcut.name] if restore else [])


def test_failed_shortcut_creation_never_restores_approval(approval, monkeypatch):
    shortcut, registry = approval
    registry.values[shortcut.name] = (_blob(3), registry.REG_BINARY)

    def failed_create(**_kwargs):
        raise OSError("shortcut failure")

    monkeypatch.setattr(wow_lifecycle, "_create_shortcut", failed_create)
    with pytest.raises(OSError, match="shortcut failure"):
        wow_lifecycle.configure_wow_sync_startup(True, restore_windows_approval=True)
    assert registry.deleted == []


def test_configure_propagates_denied_approval_after_shortcut_created(approval, monkeypatch):
    shortcut, registry = approval
    registry.values[shortcut.name] = (_blob(3), registry.REG_BINARY)
    registry.deny_write = True
    monkeypatch.setattr(wow_lifecycle, "_create_shortcut", lambda **_kwargs: None)
    with pytest.raises(OSError, match="Windows Settings"):
        wow_lifecycle.configure_wow_sync_startup(True, restore_windows_approval=True)
    assert shortcut.exists()
    assert registry.deleted == []


def test_disabling_sync_does_not_modify_windows_approval(approval):
    shortcut, registry = approval
    registry.values[shortcut.name] = (_blob(3), registry.REG_BINARY)
    assert wow_lifecycle.configure_wow_sync_startup(False, restore_windows_approval=True) is None
    assert not shortcut.exists()
    assert registry.opens == []
    assert registry.deleted == []
