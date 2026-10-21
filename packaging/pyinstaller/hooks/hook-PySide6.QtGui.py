"""Keep the unused GPL-only Qt Virtual Keyboard plugin out of the widget app."""

from pathlib import Path

from PyInstaller.utils.hooks.qt import add_qt6_dependencies


hiddenimports, binaries, datas = add_qt6_dependencies(__file__)

# QtGui's generic collection may include Virtual Keyboard when Addons is
# installed. Essentials-only environments do not contain this unused plugin.
_virtual_keyboard = [
    entry for entry in binaries
    if Path(entry[0]).name.casefold() == "qtvirtualkeyboardplugin.dll"
]
if len(_virtual_keyboard) > 1:
    raise RuntimeError("Unexpected duplicate Qt Virtual Keyboard plugins")
binaries = [entry for entry in binaries if entry not in _virtual_keyboard]
