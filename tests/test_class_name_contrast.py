import pytest
from PySide6.QtGui import QColor

from applicant_scout.constants import CLASS_COLOURS, CLASS_TEXT_COLOURS
from applicant_scout.overlay import COL_NAME, COL_SPEC
from applicant_scout.state import AppState
from test_info_panel_widget import _app
from test_overlay_fetch_identity import _window


def _luminance(colour):
    channels = [value / 255 for value in bytes.fromhex(colour[1:])]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return sum(
        weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear)
    )


@pytest.mark.parametrize("class_name", CLASS_COLOURS)
def test_class_names_are_readable_and_badges_keep_original_palette(
    qtbot, tmp_path, class_name
):
    state = AppState()
    applicant = _app(cls=class_name)
    state.add_or_update(applicant)
    window, client = _window(qtbot, tmp_path, state)
    try:
        window._refresh_table()
        row = window._row_for_id[applicant.applicant_id]
        text = window._table.item(row, COL_NAME).foreground().color().name()
        assert QColor(text) == QColor(CLASS_TEXT_COLOURS[class_name])
        assert text.lower() in window._panel._name_label.styleSheet().lower()
        assert window._table.item(row, COL_SPEC).background().color() == QColor(
            CLASS_COLOURS[class_name]
        )
        # Includes a conservative brighter bound above the table's alternating
        # rows and both scout-card gradient endpoints.
        for background in ("#292a30", "#12141b", "#0b0d13", "#121011"):
            assert (_luminance(text) + 0.05) / (_luminance(background) + 0.05) >= 4.5
    finally:
        window.shutdown_fetches()
        client.close()
