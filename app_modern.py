"""Modern PySide6 shell for the Gamma to Nederwiek migration application.

Place this file beside app.py and run app_modern.py. The existing backend,
workers, settings, reports, and migration methods remain owned by app.Main.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QScrollArea,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import app as legacy


APP_TITLE = "Project Cloner"
APP_SUBTITLE = "Revision Migration Control Center"


MODERN_QSS = r"""
QMainWindow, QWidget {
    background-color: #F4F7FB;
    color: #172033;
    font-family: "Segoe UI";
    font-size: 10pt;
}
QFrame#sidebar {
    background-color: #111827;
    border: none;
}
QLabel {
    background-color: transparent;
    font-family: "Plus Jakarta Sans", "Segoe UI";
    color: #172033;
}
QLabel#brandTitle {
    color: #FFFFFF;
    font-size: 17pt;
    font-weight: 700;
    background-color: transparent;
}
QLabel#brandSubtitle {
    color: #94A3B8;
    font-size: 9pt;
    background-color: transparent;
}
QLabel#sectionLabel {
    color: #64748B;
    font-size: 9pt;
    font-weight: 700;
    padding: 4px 2px;
    background-color: transparent;
}
QToolButton#navButton {
    background: transparent;
    color: #CBD5E1;
    border: none;
    border-radius: 9px;
    padding: 11px 13px;
    text-align: left;
    font-weight: 600;
    /* Gap between the SVG icon and the caption. A leading space in the caption
       would work too, but it widens the label enough to elide long page names. */
    spacing: 9px;
}
QToolButton#navButton:hover {
    background-color: #1F2937;
    color: #FFFFFF;
}
QToolButton#navButton:checked {
    background-color: #2563EB;
    color: #FFFFFF;
}
QFrame#header {
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 14px;
}
QLabel#pageTitle {
    color: #0F172A;
    font-size: 18pt;
    font-weight: 700;
}
QLabel#pageSubtitle {
    color: #64748B;
    font-size: 9.5pt;
}
QLabel#statusBadge {
    background-color: #DCFCE7;
    color: #166534;
    border: 1px solid #BBF7D0;
    border-radius: 10px;
    padding: 5px 11px;
    font-weight: 700;
}
QLabel#statusBadge[busy="true"] {
    background-color: #DBEAFE;
    color: #1D4ED8;
    border-color: #BFDBFE;
}
QGroupBox {
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 12px;
    margin-top: 14px;
    padding: 16px 12px 12px 12px;
    font-weight: 700;
    color: #334155;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: #334155;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTextEdit,
QPlainTextEdit, QListWidget, QTableWidget {
    background-color: #FFFFFF;
    color: #172033;
    border: 1px solid #CBD5E1;
    border-radius: 8px;
    padding: 7px 9px;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit {
    min-height: 22px;
}
QTextEdit, QPlainTextEdit, QListWidget, QTableWidget {
    selection-background-color: #DBEAFE;
    selection-color: #1E3A8A;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QTextEdit:focus, QPlainTextEdit:focus, QTableWidget:focus {
    border: 1px solid #2563EB;
}
QSpinBox#workerInput {
    min-width: 82px;
    min-height: 36px;
    padding: 0 34px 0 12px;
    background-color: #FFFFFF;
    color: #0F172A;
    border: 1px solid #CBD5E1;
    border-radius: 9px;
    font-weight: 700;
    selection-background-color: #DBEAFE;
}

QSpinBox#workerInput:hover {
    border-color: #94A3B8;
    background-color: #F8FAFC;
}

QSpinBox#workerInput:focus {
    border: 2px solid #3B82F6;
    background-color: #FFFFFF;
}

QSpinBox#workerInput::up-button,
QSpinBox#workerInput::down-button {
    subcontrol-origin: border;
    width: 28px;
    background-color: #EFF6FF;
    border-left: 1px solid #BFDBFE;
}

QSpinBox#workerInput::up-button {
    subcontrol-position: top right;
    border-top-right-radius: 8px;
    border-bottom: 1px solid #BFDBFE;
}

QSpinBox#workerInput::down-button {
    subcontrol-position: bottom right;
    border-bottom-right-radius: 8px;
}

QSpinBox#workerInput::up-button:hover,
QSpinBox#workerInput::down-button:hover {
    background-color: #DBEAFE;
}

QSpinBox#workerInput::up-button:pressed,
QSpinBox#workerInput::down-button:pressed {
    background-color: #BFDBFE;
}

QLabel#workerLabel {
    background-color: transparent;
    color: #475569;
    font-weight: 600;
}
QPushButton {
    background-color: #334155;
    color: #FFFFFF;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 8px 14px;
    font-weight: 600;
}

QPushButton:hover {
    background-color: #475569;
    border-color: #475569;
}

QPushButton:pressed {
    background-color: #1E293B;
}

QPushButton:disabled {
    background-color: #E2E8F0;
    color: #94A3B8;
    border-color: #CBD5E1;
}
QPushButton#primaryButton {
    background-color: #16A34A;
    color: #FFFFFF;
    border: 1px solid #16A34A;
}

QPushButton#primaryButton:hover {
    background-color: #15803D;
}
QPushButton#secondaryButton {
    background-color: #FFFFFF;
    color: #1D4ED8;
    border: 1px solid #93C5FD;
}

QPushButton#secondaryButton:hover {
    background-color: #EFF6FF;
    border-color: #60A5FA;
}

QPushButton#secondaryButton:pressed {
    background-color: #DBEAFE;
}

QPushButton#secondaryButton:disabled {
    background-color: #F8FAFC;
    color: #CBD5E1;
    border-color: #E2E8F0;
}
QPushButton#dangerButton {
    background-color: #FFFFFF;
    color: #B91C1C;
    border-color: #FCA5A5;
}
QPushButton#dangerButton:hover {
    background-color: #FEF2F2;
}
QHeaderView::section {
    background-color: #F8FAFC;
    color: #475569;
    border: none;
    border-bottom: 1px solid #E2E8F0;
    padding: 8px;
    font-weight: 700;
}
QTableWidget {
    gridline-color: #E2E8F0;
    alternate-background-color: #F8FAFC;
}
QTableWidget::item {
    padding: 5px;
}
QProgressBar {
    background-color: #E2E8F0;
    border: none;
    border-radius: 6px;
    min-height: 12px;
    text-align: center;
    color: #0F172A;
}
QProgressBar::chunk {
    background-color: #2563EB;
    border-radius: 6px;
}
QCheckBox, QRadioButton {
    spacing: 7px;
}
QScrollBar:vertical {
    background: transparent;
    width: 11px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #CBD5E1;
    min-height: 28px;
    border-radius: 5px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
QStatusBar {
    background-color: #FFFFFF;
    color: #64748B;
    border-top: 1px solid #E2E8F0;
}
"""


PAGE_DESCRIPTIONS = {
    "Setup": "Choose the source, destination, AutoCAD console, and migration mapping.",
    "Drawing Candidates": "Run the controlled migration stages and monitor every handoff report.",
    "Supporting File Clone": "Review and clone part lists, NC1 files, DXF files, and related assets.",
    "Title Block Discovery": "Inspect title-block entities discovered from each DWG.",
    "Revision Plan": "Review proposed revision actions before controlled writing.",
    "Reports": "Read pipeline KPIs, stage health, blocking issues, and evidence.",
}

# Sidebar captions, keyed by the real tab titles from app.py::Main.__init__.
# Short label on the left, full tab title on the right.
NAV_CAPTIONS = {
    "Setup": "Setup",
    "Drawing Candidates": "Drawing Candidates",
    "Supporting File Clone": "Supporting Files",
    "Title Block Discovery": "Discovery Results",
    "Revision Plan": "Revision Plan",
    "Reports": "Reports",
}

# One outline icon per page. The files are recoloured at render time, so a page
# without an entry here would render a blank slot; tests/test_app_shell_parity.py
# checks this map covers every real tab title and that each file exists.
ICON_DIR = Path(__file__).resolve().parent / "resources" / "icons"
NAV_ICONS = {
    "Setup": "setup.svg",
    "Drawing Candidates": "candidates.svg",
    "Supporting File Clone": "supporting.svg",
    "Title Block Discovery": "discovery.svg",
    "Revision Plan": "revision.svg",
    "Reports": "reports.svg",
}
ICON_SIZE = 18
# The SVGs are authored in the idle colour. QSvgRenderer does not resolve
# `currentColor` for us, so that literal *is* the token: _nav_icon() swaps it for
# white on the checked state. Tests assert every file still contains it.
ICON_COLOR_IDLE = "#CBD5E1"
ICON_COLOR_ACTIVE = "#FFFFFF"


def _render_icon(svg_text: str, color: str, size: int = ICON_SIZE,
                 ratio: float = 1.0) -> QPixmap:
    """Render one recoloured SVG into a transparent, HiDPI-ready pixmap."""
    renderer = QSvgRenderer(QByteArray(svg_text.replace(ICON_COLOR_IDLE, color).encode("utf-8")))
    if not renderer.isValid():
        return QPixmap()
    pixmap = QPixmap(int(round(size * ratio)), int(round(size * ratio)))
    pixmap.fill(Qt.transparent)
    pixmap.setDevicePixelRatio(ratio)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return pixmap


def _nav_icon(title: str) -> QIcon:
    """The sidebar icon for one page, white while that page is selected.

    Returns a null QIcon when the SVG is missing or malformed; the button then
    falls back to text only instead of leaving an empty slot or crashing.
    """
    name = NAV_ICONS.get(title)
    if not name:
        return QIcon()
    try:
        svg_text = (ICON_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return QIcon()
    screen = QApplication.primaryScreen()
    ratio = max(1.0, float(screen.devicePixelRatio())) if screen is not None else 1.0
    icon = QIcon()
    for state, color in ((QIcon.Off, ICON_COLOR_IDLE), (QIcon.On, ICON_COLOR_ACTIVE)):
        pixmap = _render_icon(svg_text, color, ICON_SIZE, ratio)
        if not pixmap.isNull():
            icon.addPixmap(pixmap, QIcon.Normal, state)
    return icon


class ModernMain(legacy.Main):
    """Modern shell around the existing proven application controller."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE} - {APP_SUBTITLE}")
        self.setMinimumSize(1180, 720)
        self.resize(1540, 900)
        self._install_modern_shell()
        self._apply_widget_roles()
        # Apply QSS only after all objectName roles have been assigned.
        # Qt can otherwise keep the generic QPushButton rendering.
        self.setStyleSheet(MODERN_QSS)
        self._repolish_button_palette()
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_modern_status)
        self._status_timer.start(350)
        self._refresh_modern_status()

    def _install_modern_shell(self) -> None:
        old_tabs = self.centralWidget()
        if not isinstance(old_tabs, QTabWidget):
            return

        try:
            old_tabs.currentChanged.disconnect()
        except (RuntimeError, TypeError):
            pass

        self._page_names: list[str] = []
        pages: list[QWidget] = []
        while old_tabs.count():
            pages.append(old_tabs.widget(0))
            self._page_names.append(old_tabs.tabText(0))
            old_tabs.removeTab(0)

        shell = QWidget(self)
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        sidebar = QFrame(shell)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(232)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(18, 22, 18, 18)
        side_layout.setSpacing(8)

        brand = QLabel(APP_TITLE)
        brand.setObjectName("brandTitle")
        subtitle = QLabel(APP_SUBTITLE)
        subtitle.setObjectName("brandSubtitle")
        subtitle.setWordWrap(True)
        side_layout.addWidget(brand)
        side_layout.addWidget(subtitle)
        side_layout.addSpacing(22)

        nav_label = QLabel("WORKSPACE")
        nav_label.setObjectName("sectionLabel")
        side_layout.addWidget(nav_label)

        self.page_stack = QStackedWidget(shell)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QToolButton] = []

        for index, (title, page) in enumerate(zip(self._page_names, pages)):
            if title == "Setup":
                scroll = QScrollArea(self.page_stack)
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QFrame.NoFrame)
                scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
                scroll.setWidget(page)
                page_container = scroll
            else:
                page_container = page
            self.page_stack.addWidget(page_container)
            button = QToolButton(sidebar)
            button.setObjectName("navButton")
            button.setText(self._nav_caption(title))
            icon = _nav_icon(title)
            if not icon.isNull():
                button.setIcon(icon)
            button.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.clicked.connect(lambda checked=False, i=index: self._show_page(i))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            side_layout.addWidget(button)

        side_layout.addStretch(1)
        version = QLabel("Modern shell\nPySide6 / Qt 6")
        version.setObjectName("brandSubtitle")
        side_layout.addWidget(version)

        content = QWidget(shell)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(20, 18, 20, 16)
        content_layout.setSpacing(14)

        header = QFrame(content)
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 13, 18, 13)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.modern_page_title = QLabel()
        self.modern_page_title.setObjectName("pageTitle")
        self.modern_page_subtitle = QLabel()
        self.modern_page_subtitle.setObjectName("pageSubtitle")
        self.modern_page_subtitle.setWordWrap(True)
        title_box.addWidget(self.modern_page_title)
        title_box.addWidget(self.modern_page_subtitle)
        header_layout.addLayout(title_box, 1)

        self.modern_status_badge = QLabel("READY")
        self.modern_status_badge.setObjectName("statusBadge")
        self.modern_status_badge.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(self.modern_status_badge, 0, Qt.AlignVCenter)

        content_layout.addWidget(header)
        content_layout.addWidget(self.page_stack, 1)
        shell_layout.addWidget(sidebar)
        shell_layout.addWidget(content, 1)

        old_tabs.deleteLater()
        self.setCentralWidget(shell)
        if self.nav_buttons:
            self.nav_buttons[0].setChecked(True)
            self._show_page(0)

    @staticmethod
    def _nav_caption(title: str) -> str:
        # Keys must match the real tab titles built in app.py::Main.__init__; the
        # old map used pre-rename names ("Pipeline", "Supporting Files",
        # "Discovery") and silently fell back to "  <full title>". The leading
        # spaces are gone now that the icon provides the left padding.
        return NAV_CAPTIONS.get(title, title)

    def _show_page(self, index: int) -> None:
        if not 0 <= index < self.page_stack.count():
            return
        self.page_stack.setCurrentIndex(index)
        title = self._page_names[index]
        self.modern_page_title.setText(title)
        self.modern_page_subtitle.setText(PAGE_DESCRIPTIONS.get(title, ""))
        if title == "Reports":
            self.refresh_reports()

    def show_reports_tab(self) -> None:
        """Bring the Reports page forward in the sidebar shell.

        app.Main assumes its central widget is still the legacy QTabWidget. The
        modern shell swaps that for a QStackedWidget, so the inherited method
        raised ``AttributeError: 'QWidget' object has no attribute 'count'`` as
        soon as "Open Reports" (or the scan row) called it. Navigation is pure
        presentation, so it belongs here rather than in the controller.
        """
        try:
            index = self._page_names.index("Reports")
        except (AttributeError, ValueError):
            return
        self._show_page(index)
        if 0 <= index < len(self.nav_buttons):
            self.nav_buttons[index].setChecked(True)

    def _apply_widget_roles(self) -> None:
        if hasattr(self, "primary"):
            self.primary.setObjectName("primaryButton")
        # Secondary action directly under the primary button; it only enables
        # once the controller reports the job finished (app.Main::job_finished).
        if hasattr(self, "newjobb"):
            self.newjobb.setObjectName("secondaryButton")
        # The QSS targets QSpinBox#workerInput / QLabel#workerLabel; without these
        # object names the worker controls fell back to the default Fusion look.
        if hasattr(self, "workers"):
            self.workers.setObjectName("workerInput")
        if hasattr(self, "adv"):
            self.adv.setObjectName("advancedGroup")
        for line_edit in self.findChildren(legacy.QLineEdit):
            line_edit.setMinimumHeight(38)
            line_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for form in self.findChildren(legacy.QFormLayout):
            form.setVerticalSpacing(10)
            form.setHorizontalSpacing(14)
            form.setFieldGrowthPolicy(legacy.QFormLayout.AllNonFixedFieldsGrow)
        if hasattr(self, "adv"):
            self.adv.setVisible(True)
            self.adv.setMinimumHeight(82)
        for widget in self.findChildren(QLabel):
            if widget.text().strip() == "Parallel workers":
                widget.setObjectName("workerLabel")
        for widget in self.findChildren(QPushButton):
            text = widget.text().strip().lower()
            if any(token in text for token in ("cancel", "stop")):
                widget.setObjectName("dangerButton")
            widget.setCursor(Qt.PointingHandCursor)
        for widget in self.findChildren(QAbstractButton):
            widget.setFocusPolicy(Qt.StrongFocus)

        for table in self.findChildren(legacy.QTableWidget):
            table.setAlternatingRowColors(True)
            table.setShowGrid(False)
            table.setSelectionBehavior(legacy.QAbstractItemView.SelectRows)
            table.verticalHeader().setVisible(False)

    def _repolish_button_palette(self) -> None:
        """Force Qt to re-evaluate object-name based QSS selectors."""
        widgets = [self]
        widgets.extend(self.findChildren(QPushButton))
        if hasattr(self, "adv"):
            widgets.append(self.adv)
        for widget in widgets:
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)
            widget.update()

    def _refresh_modern_status(self) -> None:
        busy = bool(getattr(self, "_busy", False))
        stage = getattr(self, "_running_stage", None)
        if busy:
            label = str(stage or "WORKING").replace("_", " ").upper()
        elif getattr(self, "_chain", False):
            label = "PIPELINE ACTIVE"
            busy = True
        else:
            label = "READY"
        self.modern_status_badge.setText(label)
        self.modern_status_badge.setProperty("busy", busy)
        self.modern_status_badge.style().unpolish(self.modern_status_badge)
        self.modern_status_badge.style().polish(self.modern_status_badge)

    # Publish-gate loop protection lives in app.Main. ModernMain deliberately does
    # not re-define next_stage/_stage_complete/refresh_pipeline/_advance_pipeline:
    # the previous copies drifted from the originals (skipped-stage handling and
    # refresh_primary were both lost), which is exactly the bug this shell exists
    # to avoid. tests/test_app_shell_parity.py enforces the rule.


def main() -> int:
    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName(APP_TITLE)
    application.setOrganizationName("Seatrium")
    application.setStyle("Fusion")
    window = ModernMain()
    # Parent-less dialogs inherit the application icon; the main window already
    # carries it for the title bar and taskbar button.
    application.setWindowIcon(window.windowIcon())
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())