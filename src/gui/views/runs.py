"""Runs browser and its read-only filtering interactions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..context import GuiApplicationContext
from ..models.run_table_model import ROW_ROLE, RunTableModel
from ..models.runs import RunBrowserRow, RunsDataProvider
from .run_details import RunDetailsDialog

try:
    from ...engine.statistics import parse_utc_timestamp
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.statistics import parse_utc_timestamp  # type: ignore[no-redef]


ALL_FILTER = ""
SCORE_FILTERS = (("All", ""), ("Scored", "scored"), ("Unscored", "unscored"))


class RunFilterProxyModel(QSortFilterProxyModel):
    """Presentation-only filtering over an engine-selected, non-deleted set."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._query = ""
        self._model_filter = ALL_FILTER
        self._benchmark_filter = ALL_FILTER
        self._session_filter = ALL_FILTER
        self._score_filter = ALL_FILTER
        self.setDynamicSortFilter(True)

    def set_filters(
        self,
        *,
        query: str = "",
        model: str = "",
        benchmark: str = "",
        session: str = "",
        score_state: str = "",
    ) -> None:
        self._query = query.strip().casefold()
        self._model_filter = model
        self._benchmark_filter = benchmark
        self._session_filter = session
        self._score_filter = score_state
        begin_change = getattr(self, "beginFilterChange", None)
        end_change = getattr(self, "endFilterChange", None)
        if callable(begin_change) and callable(end_change):
            begin_change()
            end_change(QSortFilterProxyModel.Direction.Rows)
        else:  # Qt 6.8 compatibility.
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex) -> bool:
        source = self.sourceModel()
        if source is None:
            return False
        row = source.index(source_row, 0, source_parent).data(ROW_ROLE)
        if not isinstance(row, RunBrowserRow):
            return False
        if self._query and self._query not in row.search_text:
            return False
        if self._model_filter and row.model != self._model_filter:
            return False
        if self._benchmark_filter and row.benchmark != self._benchmark_filter:
            return False
        if self._session_filter and row.session != self._session_filter:
            return False
        if self._score_filter == "scored" and not row.is_scored:
            return False
        if self._score_filter == "unscored" and row.is_scored:
            return False
        return True

    def lessThan(
        self,
        left: QModelIndex | QPersistentModelIndex,
        right: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        left_row = left.data(ROW_ROLE)
        right_row = right.data(ROW_ROLE)
        if not isinstance(left_row, RunBrowserRow) or not isinstance(right_row, RunBrowserRow):
            return super().lessThan(left, right)
        left_value = self._sort_value(left_row, left.column())
        right_value = self._sort_value(right_row, right.column())
        return left_value < right_value

    @staticmethod
    def _sort_value(row: RunBrowserRow, column: int) -> tuple[Any, ...]:
        if column == 0:
            return (row.run_id is None, row.run_id or -1)
        if column == 1:
            parsed = parse_utc_timestamp(row.recorded_at)
            return (parsed is None, parsed or datetime.min.replace(tzinfo=timezone.utc), row.recorded_at, row.run_id or -1)
        if column == 5:
            return (row.overall_score is None, row.overall_score if row.overall_score is not None else -1.0, row.run_id or -1)
        if column == 6:
            return (row.accuracy_score is None, row.accuracy_score if row.accuracy_score is not None else -1.0, row.run_id or -1)
        if column == 9:
            value = row.tokens_per_second
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            return (not numeric, float(value) if numeric else -1.0, str(value), row.run_id or -1)
        values = {
            2: row.model,
            3: row.benchmark,
            4: row.session,
            7: row.hallucination_level or "",
            8: row.reliability_level or "",
        }
        return (str(values.get(column, row.prompt_name)).casefold(), row.run_id or -1)


class RunsView(QWidget):
    """Read-only Runs browser over the shared GUI application context."""

    add_run_requested = Signal()
    refreshed = Signal()

    def __init__(
        self,
        context: GuiApplicationContext,
        parent: QWidget | None = None,
        provider: RunsDataProvider | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("runsPage")
        self.setAccessibleName("Runs page")
        self.context = context
        self.provider = provider or RunsDataProvider(context)
        self._has_loaded = False
        self._last_error: BaseException | None = None

        heading = QLabel("Runs")
        heading.setObjectName("pageTitle")
        description = QLabel("Browse recorded benchmark runs and open their historical, read-only details.")
        description.setObjectName("pageDescription")
        description.setWordWrap(True)

        self.add_run_button = QPushButton("Add Run")
        self.add_run_button.setObjectName("primaryButton")
        self.add_run_button.setAccessibleName("Add benchmark run")
        self.add_run_button.clicked.connect(self.add_run_requested)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.setAccessibleName("Refresh runs")
        self.refresh_button.clicked.connect(self.refresh)
        self.refresh_status = QLabel("Ready")
        self.refresh_status.setObjectName("refreshStatus")

        heading_row = QHBoxLayout()
        heading_row.setSpacing(12)
        heading_row.addWidget(heading)
        heading_row.addStretch(1)
        heading_row.addWidget(self.refresh_status)
        heading_row.addWidget(self.add_run_button)
        heading_row.addWidget(self.refresh_button)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("runSearch")
        self.search_edit.setAccessibleName("Search runs")
        self.search_edit.setPlaceholderText("Search model, benchmark, session, or prompt name")
        self.search_edit.textChanged.connect(self._apply_filters)

        self.model_filter = self._filter_combo("Model filter")
        self.benchmark_filter = self._filter_combo("Benchmark filter")
        self.session_filter = self._filter_combo("Session filter")
        self.score_filter = self._filter_combo("Score-state filter")
        for combo in (self.model_filter, self.benchmark_filter, self.session_filter):
            combo.currentIndexChanged.connect(self._apply_filters)
        self.score_filter.clear()
        for label, value in SCORE_FILTERS:
            self.score_filter.addItem(label, value)
        self.score_filter.currentIndexChanged.connect(self._apply_filters)

        self.clear_filters_button = QPushButton("Clear Filters")
        self.clear_filters_button.setObjectName("secondaryButton")
        self.clear_filters_button.setAccessibleName("Clear run filters")
        self.clear_filters_button.clicked.connect(self.clear_filters)

        filters = QFrame()
        filters.setObjectName("runFilterPanel")
        filter_layout = QGridLayout(filters)
        filter_layout.setContentsMargins(12, 12, 12, 12)
        filter_layout.setHorizontalSpacing(10)
        filter_layout.setVerticalSpacing(8)
        filter_layout.addWidget(QLabel("Search"), 0, 0)
        filter_layout.addWidget(self.search_edit, 0, 1, 1, 3)
        filter_layout.addWidget(QLabel("Model"), 1, 0)
        filter_layout.addWidget(self.model_filter, 1, 1)
        filter_layout.addWidget(QLabel("Benchmark"), 1, 2)
        filter_layout.addWidget(self.benchmark_filter, 1, 3)
        filter_layout.addWidget(QLabel("Session"), 2, 0)
        filter_layout.addWidget(self.session_filter, 2, 1)
        filter_layout.addWidget(QLabel("Score"), 2, 2)
        filter_layout.addWidget(self.score_filter, 2, 3)
        filter_layout.addWidget(self.clear_filters_button, 0, 4, 3, 1)
        for column in range(1, 4):
            filter_layout.setColumnStretch(column, 1)

        self.result_count = QLabel("0 visible of 0 runs")
        self.result_count.setObjectName("resultCount")
        self.result_count.setAccessibleName("Visible run count")
        self.empty_state = QLabel("No benchmark runs have been recorded yet.")
        self.empty_state.setObjectName("emptyState")
        self.empty_state.setWordWrap(True)
        self.error_state = QLabel()
        self.error_state.setObjectName("errorState")
        self.error_state.setWordWrap(True)
        self.error_state.setVisible(False)

        self.table_model = RunTableModel(self)
        self.proxy_model = RunFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.table_model)
        self.runs_table = QTableView()
        self.runs_table.setObjectName("runsTable")
        self.runs_table.setAccessibleName("Benchmark runs")
        self.runs_table.setModel(self.proxy_model)
        self.runs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.runs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.runs_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.runs_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.runs_table.setTabKeyNavigation(True)
        self.runs_table.setWordWrap(False)
        self.runs_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.runs_table.setAlternatingRowColors(True)
        self.runs_table.setSortingEnabled(True)
        self.runs_table.verticalHeader().setVisible(False)
        self.runs_table.horizontalHeader().setStretchLastSection(True)
        self.runs_table.horizontalHeader().setMinimumSectionSize(90)
        self.runs_table.setMinimumHeight(260)
        self.runs_table.activated.connect(self._open_details)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 30)
        layout.setSpacing(12)
        layout.addLayout(heading_row)
        layout.addWidget(description)
        layout.addSpacing(5)
        layout.addWidget(filters)
        results_row = QHBoxLayout()
        results_row.addWidget(QLabel("Results"), 0)
        results_row.addStretch(1)
        results_row.addWidget(self.result_count)
        layout.addLayout(results_row)
        layout.addWidget(self.error_state)
        layout.addWidget(self.empty_state)
        layout.addWidget(self.runs_table, 1)

        self.refresh()

    @staticmethod
    def _filter_combo(accessible_name: str) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName(accessible_name.lower().replace(" ", "_"))
        combo.setAccessibleName(accessible_name)
        combo.addItem("All", "")
        return combo

    @property
    def has_loaded(self) -> bool:
        return self._has_loaded

    @property
    def rows(self) -> tuple[RunBrowserRow, ...]:
        return self.table_model.rows()

    def refresh(self) -> None:
        previous_filters = self._filter_values()
        self.refresh_button.setEnabled(False)
        self.refresh_status.setText("Refreshing...")
        self.error_state.setVisible(False)
        try:
            rows = self.provider.load()
            self.table_model.set_rows(rows)
            self._populate_filter_choices(rows, previous_filters)
            self._apply_filters()
            self._has_loaded = True
            self._last_error = None
            self.refresh_status.setText("Updated")
            self.refreshed.emit()
        except Exception as error:
            self.context.logger.exception("Runs refresh failed")
            self._last_error = error
            self.error_state.setText("Runs could not be refreshed. See logs/error.log for details.")
            self.error_state.setVisible(True)
            self.refresh_status.setText("Refresh failed")
        finally:
            self.refresh_button.setEnabled(True)

    def _filter_values(self) -> dict[str, str]:
        return {
            "model": str(self.model_filter.currentData() or ""),
            "benchmark": str(self.benchmark_filter.currentData() or ""),
            "session": str(self.session_filter.currentData() or ""),
            "score": str(self.score_filter.currentData() or ""),
        }

    @staticmethod
    def _choice_values(rows: tuple[RunBrowserRow, ...], attribute: str) -> list[str]:
        values = {str(getattr(row, attribute)) for row in rows if getattr(row, attribute) not in (None, "")}
        return sorted(values, key=lambda value: (value.casefold(), value))

    def _populate_filter_choices(self, rows: tuple[RunBrowserRow, ...], previous: dict[str, str]) -> None:
        choices = {
            "model": self._choice_values(rows, "model"),
            "benchmark": self._choice_values(rows, "benchmark"),
            "session": self._choice_values(rows, "session"),
        }
        for key, combo in (("model", self.model_filter), ("benchmark", self.benchmark_filter), ("session", self.session_filter)):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("All", "")
            for value in choices[key]:
                combo.addItem(value, value)
            target = previous.get(key, "")
            index = combo.findData(target)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)
        score_target = previous.get("score", "")
        score_index = next((index for index, (_, value) in enumerate(SCORE_FILTERS) if value == score_target), 0)
        self.score_filter.blockSignals(True)
        self.score_filter.setCurrentIndex(score_index)
        self.score_filter.blockSignals(False)

    def _apply_filters(self, *_args: object) -> None:
        try:
            values = self._filter_values()
            self.proxy_model.set_filters(
                query=self.search_edit.text(),
                model=values["model"],
                benchmark=values["benchmark"],
                session=values["session"],
                score_state=values["score"],
            )
            total = self.table_model.rowCount()
            visible = self.proxy_model.rowCount()
            self.result_count.setText(f"{visible} visible of {total} runs")
            if total == 0:
                self.empty_state.setText("No benchmark runs have been recorded yet.")
                self.empty_state.setVisible(True)
            elif visible == 0:
                self.empty_state.setText("No runs match the current filters. Clear Filters to see all recorded runs.")
                self.empty_state.setVisible(True)
            else:
                self.empty_state.setVisible(False)
        except Exception as error:
            self.context.logger.exception("Runs filter failed")
            self._last_error = error
            self.error_state.setText("Runs could not be filtered. See logs/error.log for details.")
            self.error_state.setVisible(True)

    def clear_filters(self) -> None:
        self.search_edit.clear()
        for combo in (self.model_filter, self.benchmark_filter, self.session_filter, self.score_filter):
            combo.setCurrentIndex(0)
        self._apply_filters()

    def select_run(self, run_id: int, *, reveal: bool = False) -> bool:
        if reveal and not self._proxy_index_for_run(run_id).isValid():
            self.clear_filters()
        proxy_index = self._proxy_index_for_run(run_id)
        if not proxy_index.isValid():
            return False
        self.runs_table.setCurrentIndex(proxy_index)
        self.runs_table.selectRow(proxy_index.row())
        self.runs_table.scrollTo(proxy_index, QAbstractItemView.ScrollHint.PositionAtCenter)
        return True

    def _proxy_index_for_run(self, run_id: int) -> QModelIndex:
        for row in range(self.proxy_model.rowCount()):
            index = self.proxy_model.index(row, 0)
            if index.data(ROW_ROLE) is not None and index.data(ROW_ROLE).run_id == run_id:
                return index
        return QModelIndex()

    def _open_details(self, index: QModelIndex | QPersistentModelIndex) -> None:
        row = index.data(ROW_ROLE)
        if not isinstance(row, RunBrowserRow) or row.run_id is None:
            self.error_state.setText("This run does not have a persisted ID and cannot be opened.")
            self.error_state.setVisible(True)
            return
        try:
            dialog = RunDetailsDialog(self.context, row.run_id, self)
            dialog.review_saved.connect(lambda _run_id: self.refresh())
            dialog.exec()
        except Exception:
            self.context.logger.exception("Run details failed for run %s", row.run_id)
            self.error_state.setText("Run details could not be opened. See logs/error.log for details.")
            self.error_state.setVisible(True)


__all__ = ("RunFilterProxyModel", "RunsView")
