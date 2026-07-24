"""The functional, read-only Phase 5A dashboard page."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QDate, QModelIndex, QPersistentModelIndex, QObject, Qt, QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QDateEdit,
    QGroupBox,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..context import GuiApplicationContext
from ..models.dashboard import DashboardDataProvider, DashboardRecentRun, DashboardSnapshot
from ..widgets.statistics_charts import StatisticsChartPanel

try:
    from ...engine.statistics import BenchmarkStatisticsFilters, ScoreboardStatisticsFilters
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.statistics import BenchmarkStatisticsFilters, ScoreboardStatisticsFilters  # type: ignore[no-redef]


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


def _format_review_availability(reviewed: int, total: int) -> str:
    if total <= 0 or reviewed <= 0:
        return "Unavailable"
    return f"{reviewed} / {total} reviewed"


def _format_known_count(value: int, *, has_records: bool) -> str:
    if not has_records:
        return "0"
    return str(value) if value > 0 else "Unavailable"


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


class _DateRangeControls(QWidget):
    """Reusable optional date-bound controls for a source filter panel."""

    def __init__(self, prefix: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.from_check = QCheckBox("From")
        self.from_check.setObjectName(f"{prefix}DateFromEnabled")
        self.from_check.setAccessibleName(f"{prefix} date from enabled")
        self.from_date = self._date_edit(f"{prefix}DateFrom")
        self.to_check = QCheckBox("To")
        self.to_check.setObjectName(f"{prefix}DateToEnabled")
        self.to_check.setAccessibleName(f"{prefix} date to enabled")
        self.to_date = self._date_edit(f"{prefix}DateTo")
        self.from_date.setEnabled(False)
        self.to_date.setEnabled(False)
        self.from_check.setMinimumWidth(52)
        self.to_check.setMinimumWidth(42)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        from_group = QWidget(self)
        from_group.setAccessibleName(f"{prefix} from date control")
        from_layout = QHBoxLayout(from_group)
        from_layout.setContentsMargins(0, 0, 0, 0)
        from_layout.setSpacing(5)
        from_layout.addWidget(self.from_check)
        from_layout.addWidget(self.from_date)

        to_group = QWidget(self)
        to_group.setAccessibleName(f"{prefix} to date control")
        to_layout = QHBoxLayout(to_group)
        to_layout.setContentsMargins(0, 0, 0, 0)
        to_layout.setSpacing(5)
        to_layout.addWidget(self.to_check)
        to_layout.addWidget(self.to_date)

        layout.addWidget(from_group)
        layout.addStretch(1)
        layout.addWidget(to_group)

        self.from_check.toggled.connect(self.from_date.setEnabled)
        self.to_check.toggled.connect(self.to_date.setEnabled)

    @staticmethod
    def _date_edit(object_name: str) -> QDateEdit:
        editor = QDateEdit(QDate.currentDate())
        editor.setObjectName(object_name)
        editor.setAccessibleName(object_name)
        editor.setDisplayFormat("yyyy-MM-dd")
        editor.setCalendarPopup(True)
        editor.setMinimumDate(QDate(1900, 1, 1))
        editor.setMaximumDate(QDate(2999, 12, 31))
        editor.setMinimumWidth(112)
        editor.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        return editor

    def connect_change_signal(self, callback: Any) -> None:
        self.from_check.toggled.connect(callback)
        self.to_check.toggled.connect(callback)
        self.from_date.dateChanged.connect(callback)
        self.to_date.dateChanged.connect(callback)

    def values(self) -> tuple[date | None, date | None]:
        def selected(enabled: QCheckBox, editor: QDateEdit) -> date | None:
            if not enabled.isChecked():
                return None
            return date.fromisoformat(editor.date().toString("yyyy-MM-dd"))

        return selected(self.from_check, self.from_date), selected(self.to_check, self.to_date)

    def clear(self) -> None:
        blockers = (
            QSignalBlocker(self.from_check),
            QSignalBlocker(self.from_date),
            QSignalBlocker(self.to_check),
            QSignalBlocker(self.to_date),
        )
        self.from_check.setChecked(False)
        self.to_check.setChecked(False)
        self.from_date.setDate(QDate.currentDate())
        self.to_date.setDate(QDate.currentDate())
        del blockers


class BenchmarkFilterPanel(QGroupBox):
    """GUI controls that construct the typed BenchmarkRun filter contract."""

    filters_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Benchmark Run filters", parent)
        self.setObjectName("benchmarkStatisticsFilters")
        self.model_edit = self._line_edit("benchmarkModelFilter", "Any historical model")
        self.benchmark_edit = self._line_edit("benchmarkNameFilter", "Any historical benchmark")
        self.hardware_edit = self._line_edit("benchmarkHardwareFilter", "Any hardware snapshot")
        self.date_range = _DateRangeControls("benchmark", self)
        self.clear_button = QPushButton("Clear filters")
        self.clear_button.setObjectName("clearBenchmarkStatisticsFilters")
        self.clear_button.setAccessibleName("Clear Benchmark Run filters")

        layout = QGridLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(8)
        layout.addWidget(QLabel("Model"), 0, 0)
        layout.addWidget(self.model_edit, 0, 1)
        layout.addWidget(QLabel("Benchmark"), 0, 2)
        layout.addWidget(self.benchmark_edit, 0, 3)
        layout.addWidget(QLabel("Hardware"), 1, 0)
        layout.addWidget(self.hardware_edit, 1, 1)
        layout.addWidget(QLabel("Created"), 1, 2)
        layout.addWidget(self.date_range, 1, 3)
        layout.addWidget(self.clear_button, 0, 4, 2, 1)
        layout.setColumnMinimumWidth(0, 72)
        layout.setColumnMinimumWidth(2, 88)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)

        for editor in (self.model_edit, self.benchmark_edit, self.hardware_edit):
            editor.textChanged.connect(self._emit_changed)
        self.date_range.connect_change_signal(self._emit_changed)
        self.clear_button.clicked.connect(self._on_clear_clicked)

    @staticmethod
    def _line_edit(object_name: str, placeholder: str) -> QLineEdit:
        editor = QLineEdit()
        editor.setObjectName(object_name)
        editor.setAccessibleName(placeholder)
        editor.setPlaceholderText(placeholder)
        editor.setClearButtonEnabled(True)
        return editor

    def _emit_changed(self, *_args: object) -> None:
        self.filters_changed.emit()

    def _on_clear_clicked(self, _checked: bool = False) -> None:
        blockers = (
            QSignalBlocker(self.model_edit),
            QSignalBlocker(self.benchmark_edit),
            QSignalBlocker(self.hardware_edit),
        )
        self.model_edit.clear()
        self.benchmark_edit.clear()
        self.hardware_edit.clear()
        self.date_range.clear()
        del blockers
        self.filters_changed.emit()

    def filters(self) -> BenchmarkStatisticsFilters:
        date_from, date_to = self.date_range.values()
        return BenchmarkStatisticsFilters(
            model=self.model_edit.text().strip(),
            benchmark=self.benchmark_edit.text().strip(),
            hardware=self.hardware_edit.text().strip(),
            date_from=date_from,
            date_to=date_to,
        )


class ScoreboardFilterPanel(QGroupBox):
    """GUI controls that construct the typed ScoreboardEntry filter contract."""

    filters_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Scoreboard filters", parent)
        self.setObjectName("scoreboardStatisticsFilters")
        self.model_edit = QLineEdit()
        self.model_edit.setObjectName("scoreboardModelFilter")
        self.model_edit.setAccessibleName("Scoreboard model filter")
        self.model_edit.setPlaceholderText("Any scoreboard model")
        self.model_edit.setClearButtonEnabled(True)
        self.date_range = _DateRangeControls("scoreboard", self)
        self.clear_button = QPushButton("Clear filters")
        self.clear_button.setObjectName("clearScoreboardStatisticsFilters")
        self.clear_button.setAccessibleName("Clear Scoreboard filters")

        layout = QGridLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(8)
        layout.addWidget(QLabel("Model"), 0, 0)
        layout.addWidget(self.model_edit, 0, 1)
        layout.addWidget(QLabel("Imported date"), 0, 2)
        layout.addWidget(self.date_range, 0, 3)
        layout.addWidget(self.clear_button, 0, 4)
        layout.setColumnMinimumWidth(0, 72)
        layout.setColumnMinimumWidth(2, 88)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)

        self.model_edit.textChanged.connect(self._emit_changed)
        self.date_range.connect_change_signal(self._emit_changed)
        self.clear_button.clicked.connect(self._on_clear_clicked)

    def _emit_changed(self, *_args: object) -> None:
        self.filters_changed.emit()

    def _on_clear_clicked(self, _checked: bool = False) -> None:
        blocker = QSignalBlocker(self.model_edit)
        self.model_edit.clear()
        self.date_range.clear()
        del blocker
        self.filters_changed.emit()

    def filters(self) -> ScoreboardStatisticsFilters:
        date_from, date_to = self.date_range.values()
        return ScoreboardStatisticsFilters(
            model=self.model_edit.text().strip(),
            date_from=date_from,
            date_to=date_to,
        )


def _group_metric_values(groups: Any, field_name: str) -> tuple[tuple[str, float | None], ...]:
    """Bind an engine group summary field without calculating a metric."""

    return tuple((group.label, getattr(group, field_name).mean) for group in groups)


def _distribution_values(distribution: Any) -> tuple[tuple[str, float], ...]:
    return tuple((str(label), float(count)) for label, count in distribution.counts.items())


def _empty_chart_message(source: str, metric: str, record_count: int, filters_active: bool) -> str:
    if record_count == 0 and filters_active:
        return "No records match the selected filters."
    return f"No {source} {metric} data is available."


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
            "unscored_run_count": SummaryCard("Unscored runs"),
            "model_count": SummaryCard("Models"),
            "benchmark_count": SummaryCard("Benchmarks covered"),
            "session_count": SummaryCard("Sessions"),
            "hardware_count": SummaryCard("Hardware environments"),
            "scoreboard_entry_count": SummaryCard("Scoreboard entries"),
            "average_overall_score": SummaryCard("Average overall score"),
            "median_overall_score": SummaryCard("Median overall score"),
            "review_availability": SummaryCard("Review availability"),
        }
        cards = QGridLayout()
        cards.setHorizontalSpacing(12)
        cards.setVerticalSpacing(12)
        for position, card in enumerate(self.summary_cards.values()):
            cards.addWidget(card, position // 3, position % 3)
        for column in range(3):
            cards.setColumnStretch(column, 1)

        self.benchmark_filter_panel = BenchmarkFilterPanel()
        self.scoreboard_filter_panel = ScoreboardFilterPanel()
        self.visualization_tabs = QTabWidget()
        self.visualization_tabs.setObjectName("dashboardVisualizationTabs")
        self.visualization_tabs.setAccessibleName("Dashboard statistics visualizations")

        self.benchmark_score_chart = StatisticsChartPanel("Mean overall score by historical model snapshot")
        self.benchmark_speed_chart = StatisticsChartPanel("Mean tokens per second by historical model snapshot")
        self.benchmark_hallucination_chart = StatisticsChartPanel("BenchmarkRun hallucination levels")
        self.benchmark_reliability_chart = StatisticsChartPanel("BenchmarkRun reliability levels")
        self.scoreboard_score_chart = StatisticsChartPanel("Mean score by Scoreboard model")
        self.scoreboard_speed_chart = StatisticsChartPanel("Mean tokens per second by Scoreboard model")
        self.scoreboard_hallucination_chart = StatisticsChartPanel("Scoreboard hallucination levels")
        self.scoreboard_consistency_chart = StatisticsChartPanel("Scoreboard consistency levels")
        self.scoreboard_reliability_chart = StatisticsChartPanel("Scoreboard reliability levels")

        self._build_visualization_tabs()
        self.benchmark_filter_panel.filters_changed.connect(self._on_filters_changed)
        self.scoreboard_filter_panel.filters_changed.connect(self._on_filters_changed)

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

        body = QWidget()
        body.setObjectName("dashboardScrollableBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)
        body_layout.addLayout(cards)
        visualization_heading = QLabel("Statistics visualizations")
        visualization_heading.setObjectName("sectionTitle")
        body_layout.addWidget(visualization_heading)
        body_layout.addWidget(self.visualization_tabs)
        body_layout.addWidget(recent_heading)
        body_layout.addWidget(self.empty_state)
        body_layout.addWidget(self.recent_table, 1)

        scroll = QScrollArea()
        scroll.setObjectName("dashboardScrollArea")
        scroll.setAccessibleName("Dashboard content")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 30)
        layout.setSpacing(12)
        layout.addLayout(heading_row)
        layout.addWidget(description)
        layout.addSpacing(8)
        layout.addWidget(scroll, 1)

        self.refresh()

    def _build_visualization_tabs(self) -> None:
        benchmark_tab = QWidget()
        benchmark_tab.setObjectName("benchmarkRunVisualizationTab")
        benchmark_tab.setAccessibleName("Benchmark Runs visualizations")
        benchmark_layout = QVBoxLayout(benchmark_tab)
        benchmark_layout.addWidget(self.benchmark_filter_panel)
        self.benchmark_chart_grid = QGridLayout()
        self.benchmark_chart_grid.setHorizontalSpacing(12)
        self.benchmark_chart_grid.setVerticalSpacing(12)
        self.benchmark_chart_grid.addWidget(self.benchmark_score_chart, 0, 0)
        self.benchmark_chart_grid.addWidget(self.benchmark_speed_chart, 0, 1)
        self.benchmark_chart_grid.addWidget(self.benchmark_hallucination_chart, 1, 0)
        self.benchmark_chart_grid.addWidget(self.benchmark_reliability_chart, 1, 1)
        self.benchmark_chart_grid.setColumnStretch(0, 1)
        self.benchmark_chart_grid.setColumnStretch(1, 1)
        benchmark_layout.addLayout(self.benchmark_chart_grid)

        scoreboard_tab = QWidget()
        scoreboard_tab.setObjectName("scoreboardVisualizationTab")
        scoreboard_tab.setAccessibleName("Scoreboard visualizations")
        scoreboard_layout = QVBoxLayout(scoreboard_tab)
        scoreboard_layout.addWidget(self.scoreboard_filter_panel)
        self.scoreboard_chart_grid = QGridLayout()
        self.scoreboard_chart_grid.setHorizontalSpacing(12)
        self.scoreboard_chart_grid.setVerticalSpacing(12)
        self.scoreboard_chart_grid.addWidget(self.scoreboard_score_chart, 0, 0)
        self.scoreboard_chart_grid.addWidget(self.scoreboard_speed_chart, 0, 1)
        self.scoreboard_chart_grid.addWidget(self.scoreboard_hallucination_chart, 1, 0)
        self.scoreboard_chart_grid.addWidget(self.scoreboard_consistency_chart, 1, 1)
        self.scoreboard_chart_grid.addWidget(self.scoreboard_reliability_chart, 2, 0, 1, 2)
        self.scoreboard_chart_grid.setColumnStretch(0, 1)
        self.scoreboard_chart_grid.setColumnStretch(1, 1)
        scoreboard_layout.addLayout(self.scoreboard_chart_grid)

        self.visualization_tabs.addTab(benchmark_tab, "Benchmark Runs")
        self.visualization_tabs.addTab(scoreboard_tab, "Scoreboard")

    def _on_filters_changed(self) -> None:
        if self._has_loaded:
            self.refresh()

    def _active_filters(self) -> tuple[BenchmarkStatisticsFilters, ScoreboardStatisticsFilters]:
        return self.benchmark_filter_panel.filters(), self.scoreboard_filter_panel.filters()

    @staticmethod
    def _filters_active(
        benchmark_filters: BenchmarkStatisticsFilters,
        scoreboard_filters: ScoreboardStatisticsFilters,
    ) -> bool:
        return benchmark_filters != BenchmarkStatisticsFilters() or scoreboard_filters != ScoreboardStatisticsFilters()

    @property
    def has_loaded(self) -> bool:
        return self._has_loaded

    def refresh(self) -> None:
        self.refresh_button.setEnabled(False)
        self.refresh_status.setText("Refreshing...")
        try:
            benchmark_filters, scoreboard_filters = self._active_filters()
            if self._filters_active(benchmark_filters, scoreboard_filters):
                snapshot = self.provider.load(
                    benchmark_filters=benchmark_filters,
                    scoreboard_filters=scoreboard_filters,
                )
            else:
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
            self._clear_visualizations("Dashboard statistics could not be refreshed. Try Refresh again.")
        finally:
            self.refresh_button.setEnabled(True)

    def _apply_snapshot(self, snapshot: DashboardSnapshot) -> None:
        summary = snapshot.summary
        self.summary_cards["benchmark_run_count"].set_value(str(summary.benchmark_run_count))
        self.summary_cards["scored_run_count"].set_value(str(summary.scored_run_count))
        self.summary_cards["unscored_run_count"].set_value(str(summary.unscored_run_count))
        self.summary_cards["model_count"].set_value(
            _format_known_count(summary.model_count, has_records=summary.benchmark_run_count > 0)
        )
        self.summary_cards["benchmark_count"].set_value(
            _format_known_count(summary.benchmark_count, has_records=summary.benchmark_run_count > 0)
        )
        self.summary_cards["session_count"].set_value(str(summary.session_count))
        self.summary_cards["hardware_count"].set_value(
            _format_known_count(summary.known_hardware_count, has_records=summary.benchmark_run_count > 0)
        )
        self.summary_cards["scoreboard_entry_count"].set_value(str(summary.scoreboard_entry_count))
        self.summary_cards["average_overall_score"].set_value(_format_score(summary.average_overall_score))
        self.summary_cards["median_overall_score"].set_value(_format_score(summary.median_overall_score))
        self.summary_cards["review_availability"].set_value(
            _format_review_availability(summary.reviewed_run_count, summary.benchmark_run_count)
        )
        self.table_model.set_rows(snapshot.recent_runs)
        self._apply_visualization(snapshot)
        has_runs = summary.benchmark_run_count > 0
        if has_runs:
            self.empty_state.setText("")
        elif summary.scoreboard_entry_count > 0:
            self.empty_state.setText(
                "No benchmark runs have been recorded yet. Scoreboard entries are available separately."
            )
        else:
            self.empty_state.setText("No benchmark runs have been recorded yet.")
        self.empty_state.setVisible(not has_runs)

    def _apply_visualization(self, snapshot: DashboardSnapshot) -> None:
        data = snapshot.visualization
        benchmark_filters, scoreboard_filters = self._active_filters()
        benchmark_active = benchmark_filters != BenchmarkStatisticsFilters()
        scoreboard_active = scoreboard_filters != ScoreboardStatisticsFilters()

        self.benchmark_score_chart.set_bars(
            _group_metric_values(data.benchmark_model_groups, "overall_score"),
            value_title="Mean score",
            empty_message=_empty_chart_message(
                "BenchmarkRun",
                "score",
                data.benchmark_record_count,
                benchmark_active,
            ),
            unavailable_count=data.benchmark_score_missing_count,
        )
        self.benchmark_speed_chart.set_bars(
            _group_metric_values(data.benchmark_model_groups, "tokens_per_second"),
            value_title="Mean tok/s",
            empty_message=_empty_chart_message(
                "BenchmarkRun",
                "speed",
                data.benchmark_record_count,
                benchmark_active,
            ),
            unavailable_count=data.benchmark_speed_missing_count,
        )
        self.benchmark_hallucination_chart.set_distribution(
            _distribution_values(data.benchmark_hallucination),
            empty_message=_empty_chart_message(
                "BenchmarkRun",
                "hallucination category",
                data.benchmark_record_count,
                benchmark_active,
            ),
            unavailable_count=data.benchmark_hallucination.missing_count,
        )
        self.benchmark_reliability_chart.set_distribution(
            _distribution_values(data.benchmark_reliability),
            empty_message=_empty_chart_message(
                "BenchmarkRun",
                "reliability category",
                data.benchmark_record_count,
                benchmark_active,
            ),
            unavailable_count=data.benchmark_reliability.missing_count,
        )

        self.scoreboard_score_chart.set_bars(
            _group_metric_values(data.scoreboard_model_groups, "score"),
            value_title="Mean score",
            empty_message=_empty_chart_message(
                "Scoreboard",
                "score",
                data.scoreboard_record_count,
                scoreboard_active,
            ),
            unavailable_count=data.scoreboard_score_missing_count,
        )
        self.scoreboard_speed_chart.set_bars(
            _group_metric_values(data.scoreboard_model_groups, "tokens_per_second"),
            value_title="Mean tok/s",
            empty_message=_empty_chart_message(
                "Scoreboard",
                "speed",
                data.scoreboard_record_count,
                scoreboard_active,
            ),
            unavailable_count=data.scoreboard_speed_missing_count,
        )
        self.scoreboard_hallucination_chart.set_distribution(
            _distribution_values(data.scoreboard_hallucination),
            empty_message=_empty_chart_message(
                "Scoreboard",
                "hallucination category",
                data.scoreboard_record_count,
                scoreboard_active,
            ),
            unavailable_count=data.scoreboard_hallucination.missing_count,
        )
        self.scoreboard_consistency_chart.set_distribution(
            _distribution_values(data.scoreboard_consistency),
            empty_message=_empty_chart_message(
                "Scoreboard",
                "consistency category",
                data.scoreboard_record_count,
                scoreboard_active,
            ),
            unavailable_count=data.scoreboard_consistency.missing_count,
        )
        self.scoreboard_reliability_chart.set_distribution(
            _distribution_values(data.scoreboard_reliability),
            empty_message=_empty_chart_message(
                "Scoreboard",
                "reliability category",
                data.scoreboard_record_count,
                scoreboard_active,
            ),
            unavailable_count=data.scoreboard_reliability.missing_count,
        )

    def _clear_visualizations(self, message: str) -> None:
        for chart in (
            self.benchmark_score_chart,
            self.benchmark_speed_chart,
            self.benchmark_hallucination_chart,
            self.benchmark_reliability_chart,
            self.scoreboard_score_chart,
            self.scoreboard_speed_chart,
            self.scoreboard_hallucination_chart,
            self.scoreboard_consistency_chart,
            self.scoreboard_reliability_chart,
        ):
            chart.set_empty(message)
