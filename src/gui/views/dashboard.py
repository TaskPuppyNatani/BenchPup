"""The functional, read-only Phase 5A dashboard page."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..context import GuiApplicationContext
from ..models.dashboard import DashboardDataProvider, DashboardRecentRun, DashboardSnapshot


TABLE_HEADERS = ("Recorded", "Model", "Benchmark", "Overall score", "Tokens / second")


def _format_recorded_at(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value or "Unavailable"


def _format_score(value: float | None) -> str:
    return "Unavailable" if value is None else f"{value:.2f} / 5"


def _format_speed(value: Any) -> str:
    if value is None or value == "":
        return "Unavailable"
    if isinstance(value, bool):
        return "Unavailable"
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return "Unavailable"
        return f"{float(value):.1f} tok/s"
    return str(value)


class DashboardTableModel(QAbstractTableModel):
    """Small immutable-source table adapter with no write actions."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: tuple[DashboardRecentRun, ...] = ()

    def set_rows(self, rows: tuple[DashboardRecentRun, ...]) -> None:
        self.beginResetModel()
        self._rows = tuple(rows)
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(TABLE_HEADERS)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        values = (
            _format_recorded_at(row.recorded_at),
            row.model,
            row.benchmark,
            _format_score(row.overall_score),
            _format_speed(row.tokens_per_second),
        )
        if not 0 <= index.column() < len(values):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            raw_values = (
                row.recorded_at,
                row.model,
                row.benchmark,
                _format_score(row.overall_score),
                _format_speed(row.tokens_per_second),
            )
            return raw_values[index.column()]
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (3, 4):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.UserRole:
            return row.run_id
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(TABLE_HEADERS):
            return TABLE_HEADERS[section]
        if orientation == Qt.Orientation.Vertical:
            return section + 1
        return None


class SummaryCard(QFrame):
    """Accessible label/value card used by the dashboard summary."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("summaryCard")
        self.setAccessibleName(f"{title} summary")
        self.title_label = QLabel(title)
        self.title_label.setObjectName("summaryCardTitle")
        self.value_label = QLabel("Unavailable")
        self.value_label.setObjectName("summaryCardValue")
        self.value_label.setTextFormat(Qt.TextFormat.PlainText)
        self.value_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


class DashboardView(QWidget):
    """Read-only dashboard using typed service results supplied by the context."""

    refreshed = Signal()

    def __init__(
        self,
        context: GuiApplicationContext,
        parent: QWidget | None = None,
        provider: DashboardDataProvider | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dashboardPage")
        self.setAccessibleName("Dashboard page")
        self.context = context
        self.provider = provider or DashboardDataProvider(context)
        self._has_loaded = False
        self._last_error: BaseException | None = None

        heading = QLabel("Dashboard")
        heading.setObjectName("pageTitle")
        description = QLabel("A read-only view of the benchmark data already recorded in BenchPup.")
        description.setObjectName("pageDescription")
        description.setWordWrap(True)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.setAccessibleName("Refresh dashboard")
        self.refresh_button.setToolTip("Read the latest dashboard data from the shared engine services")
        self.refresh_button.clicked.connect(self.refresh)

        self.refresh_status = QLabel("Ready")
        self.refresh_status.setObjectName("refreshStatus")
        self.refresh_status.setAccessibleName("Dashboard refresh status")

        heading_row = QHBoxLayout()
        heading_row.setSpacing(12)
        heading_row.addWidget(heading)
        heading_row.addStretch(1)
        heading_row.addWidget(self.refresh_status)
        heading_row.addWidget(self.refresh_button)

        self.summary_cards: dict[str, SummaryCard] = {
            "benchmark_run_count": SummaryCard("Benchmark runs"),
            "scored_run_count": SummaryCard("Scored runs"),
            "model_count": SummaryCard("Models"),
            "session_count": SummaryCard("Sessions"),
            "scoreboard_entry_count": SummaryCard("Scoreboard entries"),
            "average_overall_score": SummaryCard("Average overall score"),
        }
        cards = QGridLayout()
        cards.setHorizontalSpacing(12)
        cards.setVerticalSpacing(12)
        for position, card in enumerate(self.summary_cards.values()):
            cards.addWidget(card, position // 3, position % 3)
        for column in range(3):
            cards.setColumnStretch(column, 1)

        recent_heading = QLabel("Recent runs")
        recent_heading.setObjectName("sectionTitle")
        self.empty_state = QLabel("No benchmark runs have been recorded yet.")
        self.empty_state.setObjectName("emptyState")
        self.empty_state.setWordWrap(True)

        self.table_model = DashboardTableModel(self)
        self.recent_table = QTableView()
        self.recent_table.setObjectName("recentRunsTable")
        self.recent_table.setAccessibleName("Recent benchmark runs")
        self.recent_table.setModel(self.table_model)
        self.recent_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.recent_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.recent_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.recent_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.recent_table.setWordWrap(False)
        self.recent_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.recent_table.setAlternatingRowColors(True)
        self.recent_table.setSortingEnabled(False)
        self.recent_table.verticalHeader().setVisible(False)
        self.recent_table.horizontalHeader().setStretchLastSection(True)
        self.recent_table.horizontalHeader().setMinimumSectionSize(110)
        self.recent_table.setMinimumHeight(220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 30)
        layout.setSpacing(12)
        layout.addLayout(heading_row)
        layout.addWidget(description)
        layout.addSpacing(8)
        layout.addLayout(cards)
        layout.addSpacing(12)
        layout.addWidget(recent_heading)
        layout.addWidget(self.empty_state)
        layout.addWidget(self.recent_table, 1)

        self.refresh()

    @property
    def has_loaded(self) -> bool:
        return self._has_loaded

    def refresh(self) -> None:
        self.refresh_button.setEnabled(False)
        self.refresh_status.setText("Refreshing...")
        try:
            snapshot = self.provider.load()
            self._apply_snapshot(snapshot)
            self._has_loaded = True
            self.refresh_status.setText("Updated")
            self.refreshed.emit()
        except Exception as error:
            self.context.logger.exception("Dashboard refresh failed")
            self.empty_state.setText("Dashboard data could not be refreshed. See logs/error.log for details.")
            self.empty_state.setVisible(True)
            self.refresh_status.setText("Refresh failed")
            self._has_loaded = False
            self._last_error = error
        finally:
            self.refresh_button.setEnabled(True)

    def _apply_snapshot(self, snapshot: DashboardSnapshot) -> None:
        summary = snapshot.summary
        self.summary_cards["benchmark_run_count"].set_value(str(summary.benchmark_run_count))
        self.summary_cards["scored_run_count"].set_value(str(summary.scored_run_count))
        self.summary_cards["model_count"].set_value(str(summary.model_count))
        self.summary_cards["session_count"].set_value(str(summary.session_count))
        self.summary_cards["scoreboard_entry_count"].set_value(str(summary.scoreboard_entry_count))
        self.summary_cards["average_overall_score"].set_value(_format_score(summary.average_overall_score))
        self.table_model.set_rows(snapshot.recent_runs)
        has_runs = summary.benchmark_run_count > 0
        self.empty_state.setText("No benchmark runs have been recorded yet." if not has_runs else "")
        self.empty_state.setVisible(not has_runs)
