"""PySide6 dashboard for the pipeline's JSON handoff reports.

The window uses only Qt: no charting package, no HTML engine, no extra runtime.
``reporting.dashboard_data`` does the parsing; this module only draws the
snapshot it returns.  Refreshing is explicit (tab activation, Refresh Reports,
or a stage completion) so an idle window does no work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from reporting.dashboard_data import (
    DRAWING_SOURCES,
    Snapshot,
    StageInfo,
    build_snapshot,
    status_tone,
)


MAX_TABLE_ROWS = 300
MAX_VISIBLE_ISSUES = 12

TONE_COLORS = {
    "ok": "#166534",
    "warn": "#B45309",
    "bad": "#B91C1C",
    "info": "#1D4ED8",
    "idle": "#64748B",
}
PILL_BACKGROUNDS = {
    "ok": "#DCFCE7",
    "warn": "#FEF3C7",
    "bad": "#FEE2E2",
    "info": "#DBEAFE",
    "idle": "#F1F5F9",
}
STATE_TITLES = {
    "COMPLETE": "DONE",
    "OPTIONAL": "OPTIONAL",
    "PENDING": "PENDING",
    "INCOMPLETE": "INCOMPLETE",
    "STALE": "STALE",
    "UNREADABLE": "UNREADABLE",
}
TABLE_STAGES = tuple(key for key, _collection in DRAWING_SOURCES)
TABLE_HEADERS = {
    "discovery": "Discovery",
    "plan": "Plan",
    "writer": "Writer",
    "validator": "Validated",
    "dryrun": "Dry run",
    "publish": "Published",
    "pdf": "PDF",
}


DASHBOARD_QSS = r"""
QWidget#dashRoot, QWidget#dashContent, QWidget#dashIssueList,
QScrollArea#dashScroll, QScrollArea#dashScroll > QWidget > QWidget {
    background: transparent;
    border: none;
}
QFrame#dashHero {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                stop:0 #FFFFFF, stop:1 #F8FAFC);
    border: 1px solid #E2E8F0;
    border-radius: 14px;
}
QLabel#dashEyebrow {
    color: #64748B;
    font-size: 8.5pt;
    font-weight: 700;
}
QLabel#dashBundle {
    color: #0F172A;
    font-size: 17pt;
    font-weight: 700;
}
QLabel#dashPath {
    color: #64748B;
    font-size: 8.5pt;
}
QLabel#dashVerdict {
    border-radius: 10px;
    padding: 6px 12px;
    font-weight: 700;
    font-size: 9pt;
}
QLabel#dashVerdict[tone="ok"] {
    background: #DCFCE7; color: #166534; border: 1px solid #BBF7D0;
}
QLabel#dashVerdict[tone="warn"] {
    background: #FEF3C7; color: #92400E; border: 1px solid #FDE68A;
}
QLabel#dashVerdict[tone="bad"] {
    background: #FEE2E2; color: #991B1B; border: 1px solid #FECACA;
}
QLabel#dashVerdict[tone="info"] {
    background: #DBEAFE; color: #1D4ED8; border: 1px solid #BFDBFE;
}
QLabel#dashVerdict[tone="idle"] {
    background: #F1F5F9; color: #475569; border: 1px solid #E2E8F0;
}
QLabel#dashUpdated {
    color: #64748B;
    font-size: 8.5pt;
}
QFrame#dashStatCard {
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 12px;
}
QLabel#dashKpiLabel {
    color: #64748B;
    font-size: 8pt;
    font-weight: 700;
}
QLabel#dashKpiValue {
    color: #0F172A;
    font-size: 20pt;
    font-weight: 700;
}
QLabel#dashKpiValue[tone="ok"] { color: #15803D; }
QLabel#dashKpiValue[tone="bad"] { color: #B91C1C; }
QLabel#dashKpiValue[tone="info"] { color: #1D4ED8; }
QLabel#dashKpiCaption {
    color: #64748B;
    font-size: 8pt;
}
QFrame#dashAccent {
    border-radius: 2px;
    min-height: 4px;
    max-height: 4px;
    background: #CBD5E1;
}
QFrame#dashAccent[tone="ok"] { background: #16A34A; }
QFrame#dashAccent[tone="bad"] { background: #DC2626; }
QFrame#dashAccent[tone="info"] { background: #2563EB; }
QFrame#dashAccent[tone="warn"] { background: #D97706; }
QGroupBox#dashGroup {
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 12px;
    margin-top: 14px;
    padding: 16px 12px 12px 12px;
    font-weight: 700;
    color: #334155;
}
QGroupBox#dashGroup::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: #334155;
}
QFrame#dashStageChip {
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 10px;
}
QFrame#dashStageChip[tone="ok"] {
    background: #F0FDF4;
    border-color: #BBF7D0;
}
QFrame#dashStageChip[tone="warn"] {
    background: #FFFBEB;
    border-color: #FDE68A;
}
QFrame#dashStageChip[tone="bad"] {
    background: #FEF2F2;
    border-color: #FECACA;
}
QLabel#dashDot {
    background: #CBD5E1;
    border-radius: 4px;
    min-width: 8px;
    max-width: 8px;
    min-height: 8px;
    max-height: 8px;
}
QLabel#dashDot[tone="ok"] { background: #16A34A; }
QLabel#dashDot[tone="warn"] { background: #D97706; }
QLabel#dashDot[tone="bad"] { background: #DC2626; }
QLabel#dashDot[tone="info"] { background: #2563EB; }
QLabel#dashStageName {
    color: #334155;
    font-size: 9pt;
    font-weight: 700;
}
QLabel#dashStageState {
    color: #64748B;
    font-size: 8pt;
    font-weight: 700;
}
QLabel#dashStageState[tone="ok"] { color: #15803D; }
QLabel#dashStageState[tone="warn"] { color: #B45309; }
QLabel#dashStageState[tone="bad"] { color: #B91C1C; }
QLabel#dashStageHeadline {
    color: #64748B;
    font-size: 8pt;
}
QFrame#dashStageCard {
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-left: 4px solid #CBD5E1;
    border-radius: 12px;
}
QFrame#dashStageCard[tone="ok"] { border-left-color: #16A34A; }
QFrame#dashStageCard[tone="warn"] { border-left-color: #D97706; }
QFrame#dashStageCard[tone="bad"] { border-left-color: #DC2626; }
QLabel#dashCardTitle {
    color: #0F172A;
    font-size: 9.5pt;
    font-weight: 700;
}
QLabel#dashCardState {
    border-radius: 8px;
    padding: 2px 7px;
    font-size: 7.5pt;
    font-weight: 700;
}
QLabel#dashCardState[tone="ok"] { background: #DCFCE7; color: #166534; }
QLabel#dashCardState[tone="warn"] { background: #FEF3C7; color: #92400E; }
QLabel#dashCardState[tone="bad"] { background: #FEE2E2; color: #991B1B; }
QLabel#dashCardState[tone="idle"] { background: #F1F5F9; color: #475569; }
QLabel#dashCardHeadline {
    color: #0F172A;
    font-size: 12pt;
    font-weight: 700;
}
QLabel#dashCardDetail {
    color: #64748B;
    font-size: 8pt;
}
QLabel#dashCardIssues {
    color: #15803D;
    font-size: 8pt;
    font-weight: 700;
}
QLabel#dashCardIssues[tone="bad"] { color: #B91C1C; }
QLabel#dashCardIssues[tone="warn"] { color: #B45309; }
QProgressBar#dashMeter {
    background: #E2E8F0;
    border: none;
    border-radius: 4px;
    min-height: 7px;
    max-height: 7px;
    text-align: center;
}
QProgressBar#dashMeter::chunk {
    background: #2563EB;
    border-radius: 4px;
}
QProgressBar#dashMeter[tone="ok"]::chunk { background: #16A34A; }
QProgressBar#dashMeter[tone="bad"]::chunk { background: #DC2626; }
QProgressBar#dashMeter[tone="warn"]::chunk { background: #D97706; }
QLabel#dashIssue {
    color: #334155;
    font-size: 8pt;
}
QLabel#dashIssuesEmpty {
    color: #15803D;
    font-size: 9pt;
    font-weight: 600;
}
QLabel#dashMoreIssues {
    color: #64748B;
    font-size: 8pt;
    font-style: italic;
}
QLineEdit#dashSearch, QComboBox#dashFilter {
    background: #FFFFFF;
    color: #172033;
    border: 1px solid #CBD5E1;
    border-radius: 8px;
    padding: 7px 10px;
    min-height: 20px;
}
QLineEdit#dashSearch:focus, QComboBox#dashFilter:focus {
    border-color: #2563EB;
}
QLabel#dashCount {
    color: #64748B;
    font-size: 8.5pt;
}
QTableWidget#dashTable {
    background: #FFFFFF;
    alternate-background-color: #F8FAFC;
    border: 1px solid #E2E8F0;
    border-radius: 8px;
    gridline-color: #E2E8F0;
}
QTableWidget#dashTable::item {
    padding: 5px;
}
QToolButton#dashEvidenceToggle {
    text-align: left;
    padding: 11px 14px;
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 10px;
    font-weight: 700;
    color: #334155;
}
QToolButton#dashEvidenceToggle:hover { background: #F8FAFC; }
QLabel#dashNote {
    color: #64748B;
    font-size: 8pt;
}
"""


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def _set_tone(widget: QWidget, tone: str) -> None:
    tone = tone or "idle"
    if widget.property("tone") == tone:
        return
    widget.setProperty("tone", tone)
    _repolish(widget)


class ElidedLabel(QLabel):
    """A single-line label that ellipsises instead of widening the whole page."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        self.setToolTip(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt signature
        self._full_text = text
        self.setToolTip(text)
        super().setText(self._elided())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().resizeEvent(event)
        super().setText(self._elided())

    def _elided(self) -> str:
        width = max(0, self.width() - 2)
        return self.fontMetrics().elidedText(
            self._full_text, Qt.ElideMiddle, width
        )


class CompletionRing(QWidget):
    """Tiny QPainter arc -- the whole 'chart library' of this dashboard."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(126, 126)
        self._completed = 0
        self._total = 0
        self._percent = 0
        self._tone = "idle"
        self.setToolTip("Pipeline stages completed")

    def set_value(self, completed: int, total: int, percent: int, tone: str) -> None:
        self._completed = completed
        self._total = total
        self._percent = max(0, min(100, int(percent)))
        self._tone = tone
        self.setToolTip(f"{completed} of {total} stages complete ({self._percent}%)")
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(11, 11, self.width() - 22, self.height() - 22)

        track = QPen(QColor("#E2E8F0"), 10)
        track.setCapStyle(Qt.RoundCap)
        painter.setPen(track)
        painter.drawArc(rect, 0, 360 * 16)

        color = {
            "ok": "#16A34A",
            "warn": "#D97706",
            "bad": "#DC2626",
            "info": "#2563EB",
        }.get(self._tone, "#94A3B8")
        arc = QPen(QColor(color), 10)
        arc.setCapStyle(Qt.RoundCap)
        painter.setPen(arc)
        painter.drawArc(rect, 90 * 16, int(-self._percent * 360 * 16 / 100))

        number = QFont(self.font())
        number.setPointSize(16)
        number.setBold(True)
        painter.setFont(number)
        painter.setPen(QColor("#0F172A"))
        painter.drawText(
            QRectF(0, 42, self.width(), 30),
            Qt.AlignCenter,
            f"{self._completed}/{self._total}",
        )

        caption = QFont(self.font())
        caption.setPointSize(7)
        caption.setBold(True)
        painter.setFont(caption)
        painter.setPen(QColor("#64748B"))
        painter.drawText(
            QRectF(0, 72, self.width(), 18),
            Qt.AlignCenter,
            "STAGES",
        )
        painter.end()


class StatusDelegate(QStyledItemDelegate):
    """Paint matrix statuses as small coloured pills instead of plain text."""

    def paint(self, painter, option, index) -> None:  # noqa: N802 - Qt signature
        text = str(index.data(Qt.DisplayRole) or "").strip()
        if not text or text == "—":
            super().paint(painter, option, index)
            return
        tone = status_tone(text)
        rect = option.rect.adjusted(5, 4, -5, -4)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(PILL_BACKGROUNDS.get(tone, "#F1F5F9")))
        painter.drawRoundedRect(rect, 6, 6)
        font = option.font
        font.setBold(tone in ("bad", "warn"))
        font.setPointSizeF(max(7.5, font.pointSizeF()))
        painter.setFont(font)
        painter.setPen(QColor(TONE_COLORS.get(tone, "#64748B")))
        painter.drawText(rect, Qt.AlignCenter, text)
        painter.restore()


class StatCard(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashStatCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        self.accent = QFrame(self)
        self.accent.setObjectName("dashAccent")
        layout.addWidget(self.accent)

        self.label = QLabel(self)
        self.label.setObjectName("dashKpiLabel")
        self.label.setWordWrap(True)
        layout.addWidget(self.label)

        self.value = QLabel(self)
        self.value.setObjectName("dashKpiValue")
        layout.addWidget(self.value)

        self.caption = QLabel(self)
        self.caption.setObjectName("dashKpiCaption")
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        layout.addStretch(1)

    def update_kpi(self, label: str, value: str, caption: str, tone: str) -> None:
        self.label.setText(label.upper())
        self.value.setText(value)
        self.caption.setText(caption)
        _set_tone(self.accent, tone)
        _set_tone(self.value, tone)


class StageChip(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashStageChip")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 8, 11, 8)
        layout.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(7)
        self.dot = QLabel(self)
        self.dot.setObjectName("dashDot")
        self.name = QLabel(self)
        self.name.setObjectName("dashStageName")
        self.name.setWordWrap(True)
        top.addWidget(self.dot, 0, Qt.AlignVCenter)
        top.addWidget(self.name, 1)
        layout.addLayout(top)

        self.state = QLabel(self)
        self.state.setObjectName("dashStageState")
        layout.addWidget(self.state)

        self.headline = QLabel(self)
        self.headline.setObjectName("dashStageHeadline")
        self.headline.setWordWrap(True)
        layout.addWidget(self.headline)

    def update_stage(self, stage: StageInfo) -> None:
        self.name.setText(stage.label)
        self.state.setText(STATE_TITLES.get(stage.state, stage.state))
        self.headline.setText(stage.headline)
        _set_tone(self, stage.tone)
        _set_tone(self.dot, stage.tone)
        _set_tone(self.state, stage.tone)
        tip = f"{stage.headline}\n{stage.detail}"
        if stage.generated_at:
            tip += f"\nGenerated: {stage.generated_at}"
        if stage.issue_count:
            tip += f"\n{stage.issue_count} issue(s)"
            for issue in stage.issues[:4]:
                tip += f"\n• {issue}"
        self.setToolTip(tip)
        self.name.setToolTip(stage.label)
        self.headline.setToolTip(stage.headline)


class StageCard(QFrame):
    activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashStageCard")
        self._path = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 11, 13, 11)
        layout.setSpacing(5)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.title = QLabel(self)
        self.title.setObjectName("dashCardTitle")
        self.title.setWordWrap(True)
        self.state = QLabel(self)
        self.state.setObjectName("dashCardState")
        self.state.setAlignment(Qt.AlignCenter)
        title_row.addWidget(self.title, 1)
        title_row.addWidget(self.state, 0, Qt.AlignTop)
        layout.addLayout(title_row)

        self.headline = QLabel(self)
        self.headline.setObjectName("dashCardHeadline")
        self.headline.setWordWrap(True)
        layout.addWidget(self.headline)

        self.detail = QLabel(self)
        self.detail.setObjectName("dashCardDetail")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        self.meter = QProgressBar(self)
        self.meter.setObjectName("dashMeter")
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        layout.addWidget(self.meter)

        self.issues = QLabel(self)
        self.issues.setObjectName("dashCardIssues")
        layout.addWidget(self.issues)
        layout.addStretch(1)

    def update_stage(self, stage: StageInfo) -> None:
        self.title.setText(stage.label)
        self.state.setText(STATE_TITLES.get(stage.state, stage.state))
        self.headline.setText(stage.headline)
        self.detail.setText(stage.detail)
        self.meter.setValue(stage.percent)
        self.meter.setToolTip(f"{stage.percent}% complete")
        if stage.issue_count:
            self.issues.setText(f"{stage.issue_count} issue(s)")
            issue_tone = "bad"
        else:
            self.issues.setText("No issues")
            issue_tone = "ok" if stage.state == "COMPLETE" else "idle"
        _set_tone(self, stage.tone)
        _set_tone(self.state, stage.tone)
        _set_tone(self.meter, stage.tone)
        _set_tone(self.issues, issue_tone)

        self._path = str(stage.path) if stage.path else ""
        self.setCursor(Qt.PointingHandCursor if self._path else Qt.ArrowCursor)
        tip = f"{stage.headline}\n{stage.detail}"
        if stage.generated_at:
            tip += f"\nGenerated: {stage.generated_at}"
        if stage.path:
            tip += f"\nDouble-click to open {stage.path.name}"
        if stage.issues:
            tip += "\n\n" + "\n".join(f"• {issue}" for issue in stage.issues[:8])
        self.setToolTip(tip)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt signature
        if self._path:
            self.activated.emit(self._path)
        super().mouseDoubleClickEvent(event)


class CollapsibleEvidence(QWidget):
    def __init__(
        self,
        table: QTableWidget | None,
        buttons: Iterable[QPushButton],
        note: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._count = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.toggle = QToolButton(self)
        self.toggle.setObjectName("dashEvidenceToggle")
        self.toggle.setCheckable(True)
        self.toggle.setChecked(False)
        self.toggle.toggled.connect(self._set_open)
        layout.addWidget(self.toggle)

        self.body = QWidget(self)
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 8, 0, 0)
        body_layout.setSpacing(8)
        if table is not None:
            table.setParent(self.body)
            table.setMinimumHeight(210)
            header = table.horizontalHeader()
            if table.columnCount() >= 3:
                header.setStretchLastSection(False)
                header.setSectionResizeMode(0, QHeaderView.Stretch)
                header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
                header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
            body_layout.addWidget(table)
        if buttons:
            row = QHBoxLayout()
            for button in buttons:
                button.setParent(self.body)
                row.addWidget(button)
            row.addStretch(1)
            body_layout.addLayout(row)
        if note:
            label = QLabel(note, self.body)
            label.setObjectName("dashNote")
            label.setWordWrap(True)
            body_layout.addWidget(label)
        self.body.setVisible(False)
        layout.addWidget(self.body)
        self._update_text()

    def _set_open(self, opened: bool) -> None:
        self.body.setVisible(opened)
        self._update_text()

    def _update_text(self) -> None:
        action = "Hide" if self.toggle.isChecked() else "Show"
        self.toggle.setText(f"{action} evidence files ({self._count})")

    def set_count(self, count: int) -> None:
        self._count = int(count)
        self._update_text()


class ReportDashboard(QWidget):
    """The Reports page: KPIs, stage health, issues, matrix, raw evidence."""

    stageActivated = Signal(str)

    def __init__(
        self,
        evidence_table: QTableWidget | None = None,
        evidence_buttons: Iterable[QPushButton] = (),
        evidence_note: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dashRoot")
        self.setStyleSheet(DASHBOARD_QSS)
        self._snapshot: Snapshot | None = None
        self._kpi_cards: list[StatCard] = []
        self._chips: dict[str, StageChip] = {}
        self._cards: dict[str, StageCard] = {}

        scroll = QScrollArea(self)
        scroll.setObjectName("dashScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        content = QWidget(scroll)
        content.setObjectName("dashContent")
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 6, 4)
        layout.setSpacing(14)

        layout.addWidget(self._build_hero())
        layout.addWidget(self._build_kpis())
        layout.addWidget(self._build_health())
        layout.addLayout(self._build_detail_row())
        layout.addWidget(self._build_matrix())
        self.evidence = CollapsibleEvidence(
            evidence_table, evidence_buttons, evidence_note, content
        )
        layout.addWidget(self.evidence)

        self._snapshot = Snapshot(root=Path())
        self._apply(self._snapshot)

    # -- construction ----------------------------------------------------

    def _build_hero(self) -> QWidget:
        hero = QFrame(self)
        hero.setObjectName("dashHero")
        row = QHBoxLayout(hero)
        row.setContentsMargins(20, 17, 20, 17)
        row.setSpacing(18)

        left = QVBoxLayout()
        left.setSpacing(3)
        eyebrow = QLabel("PIPELINE REPORT", hero)
        eyebrow.setObjectName("dashEyebrow")
        self.bundle_label = QLabel("No bundle selected", hero)
        self.bundle_label.setObjectName("dashBundle")
        self.path_label = ElidedLabel("", hero)
        self.path_label.setObjectName("dashPath")
        self.verdict_detail = QLabel("", hero)
        self.verdict_detail.setObjectName("dashPath")
        self.verdict_detail.setWordWrap(True)
        left.addWidget(eyebrow)
        left.addWidget(self.bundle_label)
        left.addWidget(self.path_label)
        left.addSpacing(4)
        left.addWidget(self.verdict_detail)
        row.addLayout(left, 1)

        self.ring = CompletionRing(hero)
        row.addWidget(self.ring, 0, Qt.AlignVCenter)

        right = QVBoxLayout()
        right.setSpacing(6)
        self.verdict = QLabel("NOT STARTED", hero)
        self.verdict.setObjectName("dashVerdict")
        self.verdict.setAlignment(Qt.AlignCenter)
        self.updated = QLabel("No reports yet", hero)
        self.updated.setObjectName("dashUpdated")
        self.updated.setAlignment(Qt.AlignRight)
        right.addWidget(self.verdict, 0, Qt.AlignRight)
        right.addWidget(self.updated, 0, Qt.AlignRight)
        right.addStretch(1)
        row.addLayout(right, 0)
        return hero

    def _build_kpis(self) -> QWidget:
        holder = QWidget(self)
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        for index in range(8):
            card = StatCard(holder)
            self._kpi_cards.append(card)
            grid.addWidget(card, index // 4, index % 4)
        for column in range(4):
            grid.setColumnStretch(column, 1)
        return holder

    def _build_health(self) -> QWidget:
        group = QGroupBox("Stage health", self)
        group.setObjectName("dashGroup")
        grid = QGridLayout(group)
        grid.setContentsMargins(12, 18, 12, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        for index, key in enumerate(
            ("scan", "destination", "clone", "discovery", "plan",
             "writer", "validator", "dryrun", "publish", "pdf")
        ):
            chip = StageChip(group)
            self._chips[key] = chip
            grid.addWidget(chip, index // 5, index % 5)
        for column in range(5):
            grid.setColumnStretch(column, 1)
        return group

    def _build_detail_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(14)

        detail = QGroupBox("Stage detail", self)
        detail.setObjectName("dashGroup")
        grid = QGridLayout(detail)
        grid.setContentsMargins(12, 18, 12, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        keys = ("destination", "clone", "discovery", "plan", "writer",
                "validator", "dryrun", "publish", "pdf")
        for index, key in enumerate(keys):
            card = StageCard(detail)
            card.activated.connect(self.stageActivated.emit)
            self._cards[key] = card
            grid.addWidget(card, index // 3, index % 3)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        row.addWidget(detail, 3)

        self.issues_group = QGroupBox("Blocking issues", self)
        self.issues_group.setObjectName("dashGroup")
        self.issues_host = QWidget(self.issues_group)
        self.issues_layout = QVBoxLayout(self.issues_host)
        self.issues_layout.setContentsMargins(0, 0, 0, 0)
        self.issues_layout.setSpacing(6)
        self.issues_layout.addStretch(1)
        issues_outer = QVBoxLayout(self.issues_group)
        # The nested host needs its own gap below the group-box title; without
        # it the first issue label draws under the title.
        issues_outer.setContentsMargins(12, 32, 12, 12)
        issues_outer.addWidget(self.issues_host)
        self.issues_group.setMinimumWidth(260)
        row.addWidget(self.issues_group, 1)
        return row

    def _build_matrix(self) -> QWidget:
        group = QGroupBox("Per-drawing status", self)
        group.setObjectName("dashGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 18, 12, 12)
        layout.setSpacing(9)

        bar = QHBoxLayout()
        bar.setSpacing(9)
        self.search = QLineEdit(group)
        self.search.setObjectName("dashSearch")
        self.search.setPlaceholderText("Filter by identity, type, status, or issue...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._render_table)
        self.filter_box = QComboBox(group)
        self.filter_box.setObjectName("dashFilter")
        self.filter_box.addItems(["All drawings", "Issues only"])
        self.filter_box.currentIndexChanged.connect(self._render_table)
        self.count_label = QLabel("0 of 0 drawings", group)
        self.count_label.setObjectName("dashCount")
        bar.addWidget(self.search, 1)
        bar.addWidget(self.filter_box, 0)
        bar.addWidget(self.count_label, 0)
        layout.addLayout(bar)

        headers = ["Identity", "Type"] + [
            TABLE_HEADERS[key] for key in TABLE_STAGES
        ] + ["Issues"]
        self.table = QTableWidget(0, len(headers), group)
        self.table.setObjectName("dashTable")
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setStretchLastSection(False)
        self._status_delegate = StatusDelegate(self.table)
        for column in range(2, 2 + len(TABLE_STAGES)):
            self.table.setItemDelegateForColumn(column, self._status_delegate)
        layout.addWidget(self.table)
        return group

    # -- refresh ---------------------------------------------------------

    def refresh(self, reports_root: str | Path | None) -> None:
        if reports_root is None:
            snapshot = Snapshot(root=Path())
        else:
            snapshot = build_snapshot(reports_root)
        self._snapshot = snapshot
        self._apply(snapshot)

    def _apply(self, snapshot: Snapshot) -> None:
        self.bundle_label.setText(snapshot.bundle)
        self.path_label.setText(str(snapshot.root) if snapshot.root.name else "")
        self.path_label.setToolTip(str(snapshot.root))
        self.verdict.setText(snapshot.verdict)
        _set_tone(self.verdict, snapshot.verdict_tone)
        self.verdict_detail.setText(snapshot.verdict_detail)
        self.updated.setText(
            f"Updated {snapshot.last_updated}" if snapshot.last_updated
            else "No reports yet"
        )
        self.ring.set_value(
            snapshot.completed, snapshot.total, snapshot.percent, snapshot.verdict_tone
        )

        for card, kpi in zip(self._kpi_cards, snapshot.kpis):
            card.update_kpi(kpi.label, kpi.value, kpi.caption, kpi.tone)
            card.setVisible(True)
        for card in self._kpi_cards[len(snapshot.kpis):]:
            card.setVisible(False)

        for stage in snapshot.stages:
            chip = self._chips.get(stage.key)
            if chip is not None:
                chip.update_stage(stage)
            card = self._cards.get(stage.key)
            if card is not None:
                card.update_stage(stage)

        self._update_issues(snapshot)
        self._render_table()
        self.evidence.set_count(snapshot.evidence_count)

    def _update_issues(self, snapshot: Snapshot) -> None:
        while self.issues_layout.count():
            item = self.issues_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        if snapshot.issue_count:
            self.issues_group.setTitle(f"Blocking issues ({snapshot.issue_count})")
            for issue in snapshot.issues[:MAX_VISIBLE_ISSUES]:
                label = QLabel(f"• {issue}", self.issues_host)
                label.setObjectName("dashIssue")
                label.setWordWrap(True)
                label.setToolTip(issue)
                self.issues_layout.addWidget(label)
            remaining = snapshot.issue_count - min(
                len(snapshot.issues), MAX_VISIBLE_ISSUES
            )
            if remaining > 0:
                more = QLabel(f"... and {remaining} more", self.issues_host)
                more.setObjectName("dashMoreIssues")
                self.issues_layout.addWidget(more)
        else:
            self.issues_group.setTitle("Blocking issues")
            label = QLabel("No blocking issues found.", self.issues_host)
            label.setObjectName("dashIssuesEmpty")
            label.setWordWrap(False)
            self.issues_layout.addWidget(label)
        self.issues_layout.addStretch(1)

    def _filtered_drawings(self):
        if self._snapshot is None:
            return []
        query = self.search.text().strip()
        issues_only = self.filter_box.currentIndex() == 1
        return [
            drawing
            for drawing in self._snapshot.drawings
            if drawing.matches(query) and (not issues_only or drawing.issues)
        ]

    def _render_table(self, *_args) -> None:
        drawings = self._filtered_drawings()
        total = len(self._snapshot.drawings) if self._snapshot else 0
        shown = drawings[:MAX_TABLE_ROWS]
        self.count_label.setText(
            f"{len(shown)} of {total} drawings"
            + ("" if len(shown) == len(drawings)
               else f" · {len(drawings) - len(shown)} more match")
        )

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(shown))
        for row, drawing in enumerate(shown):
            identity_item = QTableWidgetItem(drawing.identity)
            self.table.setItem(row, 0, identity_item)
            type_item = QTableWidgetItem(drawing.drawing_type)
            type_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 1, type_item)
            for offset, key in enumerate(TABLE_STAGES, start=2):
                text = drawing.statuses.get(key, "—")
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, offset, item)
            issue_item = QTableWidgetItem(str(len(drawing.issues)) if drawing.issues else "")
            issue_item.setTextAlignment(Qt.AlignCenter)
            if drawing.issues:
                issue_item.setForeground(QColor(TONE_COLORS["bad"]))
                issue_item.setToolTip("\n".join(drawing.issues))
            self.table.setItem(row, len(TABLE_STAGES) + 2, issue_item)

        header_height = self.table.horizontalHeader().sizeHint().height()
        row_height = self.table.verticalHeader().defaultSectionSize()
        table_height = header_height + max(1, len(shown)) * row_height + 14
        self.table.setFixedHeight(min(360, max(130, table_height)))
