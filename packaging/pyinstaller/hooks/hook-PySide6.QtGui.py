"""Keep the unused GPL-only Qt Virtual Keyboard plugin out of the widget app."""

from pathlib import Path

from PyInstaller.utils.hooks.qt import add_qt6_dependencies


hiddenimports, binaries, datas = add_qt6_dependencies(__file__)

# QtGui's generic platform-input plugin collection includes Virtual Keyboard,
# although ApplicantScout uses only QWidget text fields and never this module.
_virtual_keyboard = [
    entry for entry in binaries
    if Path(entry[0]).name.casefold() == "qtvirtualkeyboardplugin.dll"
]
if len(_virtual_keyboard) != 1:
    raise RuntimeError("Expected one Qt Virtual Keyboard plugin in pinned PySide6")
binaries = [entry for entry in binaries if entry not in _virtual_keyboard]
