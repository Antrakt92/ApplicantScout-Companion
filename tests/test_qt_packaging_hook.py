from pathlib import Path
import runpy

from PyInstaller.utils.hooks import qt
import pytest


HOOK = Path(__file__).resolve().parents[1] / "packaging/pyinstaller/hooks/hook-PySide6.QtGui.py"


@pytest.mark.parametrize("plugin_count", [0, 1, 2])
def test_gui_hook_excludes_optional_virtual_keyboard(monkeypatch, plugin_count):
    retained = ("Qt/plugins/platforms/qwindows.dll", "PySide6/plugins/platforms")
    plugins = [
        (f"Qt/{index}/qtvirtualkeyboardplugin.dll", "PySide6/plugins/platforminputcontexts")
        for index in range(plugin_count)
    ]
    monkeypatch.setattr(
        qt, "add_qt6_dependencies",
        lambda _hook: (["PySide6.QtCore"], [retained, *plugins], [("data", ".")]),
    )
    if plugin_count > 1:
        with pytest.raises(RuntimeError, match="Virtual Keyboard"):
            runpy.run_path(str(HOOK))
        return

    result = runpy.run_path(str(HOOK))
    assert result["binaries"] == [retained]
    assert result["hiddenimports"] == ["PySide6.QtCore"]
    assert result["datas"] == [("data", ".")]
