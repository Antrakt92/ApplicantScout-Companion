"""Widget-group builders for the settings dialog (P7 extraction).

Build half of ``SettingsDialog.__init__``: the WARCRAFT LOGS section, the
Screenshots path row, the update status/cancel controls, and the usage
statistics section — no dialog state. ``SettingsDialog`` stays a thin
composer that calls :func:`build_wcl_section`, :func:`build_paths_section`,
:func:`build_updates_section`, and :func:`build_usage_section`, inserts the
returned groups into its layouts, and owns every signal connection exactly
where it was (connection order and widget defaults are unchanged).

The tiny widget helpers :func:`_settings_section` and
:func:`_set_tooltip_and_accessibility` live here because the builders are
their main callers; ``settings_dialog`` re-imports them so existing call
sites keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


def _set_tooltip_and_accessibility(
    button: QAbstractButton,
    *,
    tooltip: str,
    accessible_name: str,
    accessible_description: str | None = None,
) -> None:
    button.setToolTip(tooltip)
    button.setAccessibleName(accessible_name)
    button.setAccessibleDescription(accessible_description or tooltip)


def _settings_section(
    parent: QWidget,
    *,
    object_name: str,
    title: str,
    hint: str,
) -> tuple[QWidget, QVBoxLayout]:
    section = QWidget(parent)
    section.setObjectName(object_name)
    layout = QVBoxLayout(section)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(7)

    section_title = QLabel(title, section)
    section_title.setObjectName("settingsSectionTitle")
    layout.addWidget(section_title)

    if hint:
        section_hint = QLabel(hint, section)
        section_hint.setObjectName("settingsSectionHint")
        section_hint.setWordWrap(True)
        layout.addWidget(section_hint)
    return section, layout


@dataclass(slots=True)
class UsageSection:
    """Usage statistics group; the dialog owns the consent connection."""

    section: QWidget
    consent_check: QCheckBox
    privacy_link: QLabel
    unavailable_label: QLabel | None


def build_usage_section(
    parent: QWidget,
    *,
    consent_checked: bool,
    consent_enabled: bool,
    show_unavailable: bool,
) -> UsageSection:
    """Build the usage statistics section (no signal connections)."""
    usage_section = QWidget(parent)
    usage_section.setObjectName("usageStatisticsSection")
    usage_root = QHBoxLayout(usage_section)
    usage_root.setContentsMargins(12, 9, 12, 9)
    usage_root.setSpacing(10)
    usage_check = QCheckBox("Share usage statistics", usage_section)
    usage_check.setObjectName("shareUsageStatistics")
    usage_check.setChecked(consent_checked)
    usage_check.setEnabled(consent_enabled)
    usage_details = (
        "Optional; off until you opt in. Existing choices are preserved. "
        "Shares a random installation ID, app version and daily setup/use milestones. "
        "No names, screenshots, credentials or folder paths. "
        "Events leave active storage after 90 UTC days; recovery history can last 7 more days. "
        "Changes save immediately, even if setup is incomplete. "
        "Turning this off stops future reporting and clears queued events and the local ID."
    )
    usage_check.setToolTip(usage_details)
    usage_check.setAccessibleDescription(usage_details)
    usage_root.addWidget(usage_check)
    usage_root.addStretch(1)
    usage_privacy = QLabel(
        '<a href="https://github.com/Antrakt92/ApplicantScout-Companion/blob/main/docs/PRIVACY.md">'
        'Privacy</a>', usage_section
    )
    usage_privacy.setObjectName("usagePrivacyLink")
    usage_privacy.setToolTip(usage_details)
    usage_privacy.setAccessibleName("Usage statistics privacy details")
    usage_privacy.setOpenExternalLinks(True)
    usage_privacy.setTextInteractionFlags(
        Qt.TextInteractionFlag.LinksAccessibleByMouse
        | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
    )
    usage_privacy.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    usage_privacy.setWordWrap(True)
    usage_root.addWidget(usage_privacy)
    unavailable_label: QLabel | None = None
    if show_unavailable:
        unavailable_label = QLabel(
            "Reporting unavailable", usage_section
        )
        unavailable_label.setObjectName("usageUnavailableStatus")
        unavailable_label.setToolTip(
            "Sending is disabled for this build or installation."
        )
        usage_root.insertWidget(1, unavailable_label)
    return UsageSection(
        section=usage_section,
        consent_check=usage_check,
        privacy_link=usage_privacy,
        unavailable_label=unavailable_label,
    )


@dataclass(slots=True)
class WclSection:
    """WARCRAFT LOGS group; the dialog owns the link/example/reveal connections."""

    section: QWidget
    clients_link: QPushButton
    example_arrow: QLabel
    example_button: QPushButton
    client_id_edit: QLineEdit
    client_secret_edit: QLineEdit
    reveal_secret_button: QPushButton
    region_combo: QComboBox


def build_wcl_section(
    parent: QWidget,
    *,
    client_id: str,
    client_secret: str,
    region: str,
    create_client_redirect_url: str,
) -> WclSection:
    """Build the WARCRAFT LOGS section (no signal connections)."""
    wcl_section, wcl_root = _settings_section(
        parent,
        object_name="warcraftLogsSection",
        title="WARCRAFT LOGS",
        hint="",
    )

    wcl_link_row = QWidget(wcl_section)
    wcl_link_layout = QHBoxLayout(wcl_link_row)
    wcl_link_layout.setContentsMargins(0, 0, 0, 0)
    wcl_link_layout.setSpacing(8)
    wcl_clients_link = QPushButton("Warcraft Logs API clients")
    wcl_clients_link.setObjectName("wclClientsLink")
    wcl_clients_link.setFlat(True)
    wcl_clients_link.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    wcl_clients_link.setAccessibleName("Open Warcraft Logs API clients")
    wcl_clients_link.setAccessibleDescription(
        "Open the Warcraft Logs Create Client page in the default browser."
    )
    wcl_link_layout.addWidget(wcl_clients_link)
    wcl_example_arrow = QLabel("→")
    wcl_example_arrow.setObjectName("wclClientsToExampleArrow")
    wcl_example_arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
    wcl_example_arrow.setToolTip(
        "Open the example to see exactly what to enter on Warcraft Logs."
    )
    wcl_link_layout.addWidget(wcl_example_arrow)
    wcl_example_button = QPushButton("Show example")
    wcl_example_button.setObjectName("showWclSetupExample")
    _set_tooltip_and_accessibility(
        wcl_example_button,
        tooltip="Show the Warcraft Logs Create Client form values to copy.",
        accessible_name="Show WCL setup example",
        accessible_description=(
            "Show the Warcraft Logs Create Client form values to copy."
        ),
    )
    wcl_link_layout.addWidget(wcl_example_button)
    wcl_link_layout.addStretch(1)
    credentials_help = (
        "Create a Warcraft Logs API client with Redirect URL "
        f"{create_client_redirect_url} and leave Public Client unchecked. Copy the "
        "generated Client ID and Client Secret into the credential fields."
    )
    wcl_link_row.setToolTip(credentials_help)

    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    form.setHorizontalSpacing(12)
    form.setVerticalSpacing(8)
    wcl_root.addLayout(form)

    client_id_edit = QLineEdit(client_id)
    client_id_edit.setObjectName("wclClientId")
    client_id_edit.setPlaceholderText("Paste your Client ID")
    client_id_edit.setToolTip("Client ID generated by your Warcraft Logs API client.")
    client_id_edit.setAccessibleName("Warcraft Logs Client ID")
    client_id_edit.setAccessibleDescription(
        "Client ID generated by the private Warcraft Logs API client."
    )
    form.addRow("Client ID", client_id_edit)

    client_secret_edit = QLineEdit(client_secret)
    client_secret_edit.setObjectName("wclClientSecret")
    client_secret_edit.setPlaceholderText("Paste your Client Secret")
    client_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
    client_secret_edit.setToolTip(
        "Client Secret generated by your Warcraft Logs API client."
    )
    client_secret_edit.setAccessibleName("Warcraft Logs Client Secret")
    client_secret_edit.setAccessibleDescription(
        "Secret generated by the private Warcraft Logs API client; the value is masked."
    )
    secret_row = QWidget(wcl_section)
    secret_layout = QHBoxLayout(secret_row)
    secret_layout.setContentsMargins(0, 0, 0, 0)
    secret_layout.setSpacing(6)
    secret_layout.addWidget(client_secret_edit, stretch=1)
    reveal_secret_button = QPushButton("Show", secret_row)
    reveal_secret_button.setObjectName("revealWclClientSecret")
    reveal_secret_button.setCheckable(True)
    reveal_secret_button.setAutoDefault(False)
    reveal_secret_button.setMinimumWidth(56)
    form.addRow("Client Secret", secret_row)

    region_combo = QComboBox()
    region_combo.setObjectName("region")
    region_combo.addItems(["EU", "US", "KR", "TW", "CN"])
    region_combo.setToolTip(
        "Fallback region used when an applicant name does not include a known realm."
    )
    region_combo.setAccessibleName("Warcraft Logs fallback region")
    region_combo.setAccessibleDescription(
        "Fallback region used when the applicant's character realm cannot determine it."
    )
    region_idx = region_combo.findText((region or "EU").upper())
    region_combo.setCurrentIndex(max(0, region_idx))
    form.addRow("Region fallback", region_combo)
    wcl_root.addWidget(wcl_link_row)
    return WclSection(
        section=wcl_section,
        clients_link=wcl_clients_link,
        example_arrow=wcl_example_arrow,
        example_button=wcl_example_button,
        client_id_edit=client_id_edit,
        client_secret_edit=client_secret_edit,
        reveal_secret_button=reveal_secret_button,
        region_combo=region_combo,
    )


@dataclass(slots=True)
class PathsSection:
    """Screenshots path row; the dialog owns the edit/browse connections."""

    row: QWidget
    screenshots_edit: QLineEdit
    browse_button: QPushButton


def build_paths_section(
    parent: QWidget,
    *,
    initial_path: str,
) -> PathsSection:
    """Build the Screenshots path row (no signal connections)."""
    path_row = QWidget(parent)
    path_layout = QHBoxLayout(path_row)
    path_layout.setContentsMargins(0, 0, 0, 0)
    path_layout.setSpacing(6)
    screenshots_edit = QLineEdit(initial_path)
    screenshots_edit.setObjectName("screenshotsPath")
    screenshots_edit.setPlaceholderText(
        r"Example: C:\Program Files (x86)\World of Warcraft\_retail_\Screenshots"
    )
    screenshots_edit.setToolTip(
        "Select the active WoW client's Screenshots folder under _retail_, _ptr_, or _xptr_."
    )
    screenshots_edit.setAccessibleName("WoW Screenshots folder")
    screenshots_edit.setAccessibleDescription(
        r"Path to the active _retail_, _ptr_, or _xptr_ Screenshots folder."
    )
    path_layout.addWidget(screenshots_edit, stretch=1)
    browse_button = QPushButton("Browse")
    browse_button.setObjectName("browseScreenshots")
    _set_tooltip_and_accessibility(
        browse_button,
        tooltip="Browse to WoW's in-game Screenshots folder.",
        accessible_name="Browse WoW Screenshots folder",
    )
    path_layout.addWidget(browse_button)
    return PathsSection(
        row=path_row,
        screenshots_edit=screenshots_edit,
        browse_button=browse_button,
    )


@dataclass(slots=True)
class UpdatesSection:
    """Update status/cancel controls; the dialog owns the cancel connection."""

    status_label: QLabel
    cancel_button: QPushButton


def build_updates_section(parent: QWidget) -> UpdatesSection:
    """Build the update status label and cancel button (no signal connections)."""
    status_label = QLabel("", parent)
    status_label.setObjectName("settingsStatus")
    status_label.setWordWrap(True)
    status_label.setAccessibleName("Settings status")
    status_label.setAccessibleDescription("")
    status_label.setProperty("statusState", "idle")
    status_label.hide()
    cancel_update_button = QPushButton("Cancel", parent)
    cancel_update_button.setObjectName("cancelUpdate")
    cancel_update_button.setAccessibleName("Cancel update download")
    cancel_update_button.setToolTip("Cancel before the installer starts.")
    cancel_update_button.hide()
    return UpdatesSection(
        status_label=status_label,
        cancel_button=cancel_update_button,
    )
