"""Presentation-only Comparisons page for the Phase 5G2A GUI workflow."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import Enum
import math
from typing import Any

from PySide6.QtCore import QDate, QSortFilterProxyModel, Qt, QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..context import GuiApplicationContext
from ..models.comparison_table_model import (
    ComparisonSubjectRow,
    ComparisonSubjectTableModel,
    ComparisonTableModel,
    ComparisonTableRow,
)

try:
    from ...engine.domain import BENCHMARK_TYPES, LEVELS
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.domain import BENCHMARK_TYPES, LEVELS  # type: ignore[no-redef]

try:
    from ...engine.comparisons import (
        BenchmarkComparisonRequest,
        BenchmarkModelComparisonRequest,
        ComparisonResultState,
        ComparisonSubject,
        MetricComparison,
        ModelComparisonResult,
        ScoreboardModelComparisonRequest,
        ScoreboardModelComparisonResult,
        SessionComparisonResult,
    )
    from ...engine.domain import BenchmarkSession
    from ...engine.statistics import BenchmarkStatisticsFilters, ScoreboardStatisticsFilters
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.comparisons import (  # type: ignore[no-redef]
        BenchmarkComparisonRequest,
        BenchmarkModelComparisonRequest,
        ComparisonResultState,
        ComparisonSubject,
        MetricComparison,
        ModelComparisonResult,
        ScoreboardModelComparisonRequest,
        ScoreboardModelComparisonResult,
        SessionComparisonResult,
    )
    from engine.domain import BenchmarkSession  # type: ignore[no-redef]
    from engine.statistics import BenchmarkStatisticsFilters, ScoreboardStatisticsFilters  # type: ignore[no-redef]


class ComparisonSource(str, Enum):
    BENCHMARK_RUNS = "benchmark_runs"
    SCOREBOARDS = "scoreboards"


class ComparisonDimension(str, Enum):
    MODELS = "models"
    SESSIONS = "sessions"
    BENCHMARKS = "benchmarks"


class _FilterValidationError(ValueError):
    def __init__(self, message: str, widget: QWidget) -> None:
        super().__init__(message)
        self.widget = widget


_COMPARISON_CONTROL_MIN_HEIGHT = 30
_COMPARISON_SELECTION_PANEL_MIN_HEIGHT = 220


def _configure_comparison_control(widget: QWidget, *, expand_horizontally: bool = True) -> None:
    """Keep styled controls readable while preserving responsive width behavior."""

    minimum_height = max(
        _COMPARISON_CONTROL_MIN_HEIGHT,
        widget.sizeHint().height(),
        widget.minimumSizeHint().height(),
        widget.fontMetrics().height() + 12,
    )
    widget.setMinimumHeight(minimum_height)
    widget.setSizePolicy(
        QSizePolicy.Policy.Expanding if expand_horizontally else QSizePolicy.Policy.Preferred,
        QSizePolicy.Policy.Fixed,
    )


class _OptionalDateRange(QWidget):
    """Comparison-local optional date controls matching the shared GUI convention."""

    changed = Signal()

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

        _configure_comparison_control(self.from_check, expand_horizontally=False)
        _configure_comparison_control(self.from_date)
        _configure_comparison_control(self.to_check, expand_horizontally=False)
        _configure_comparison_control(self.to_date)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.from_check)
        layout.addWidget(self.from_date)
        layout.addSpacing(8)
        layout.addWidget(self.to_check)
        layout.addWidget(self.to_date)
        self.from_check.toggled.connect(self.from_date.setEnabled)
        self.to_check.toggled.connect(self.to_date.setEnabled)
        self.from_check.toggled.connect(self.changed)
        self.to_check.toggled.connect(self.changed)
        self.from_date.dateChanged.connect(self.changed)
        self.to_date.dateChanged.connect(self.changed)

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
        return editor

    def values(self) -> tuple[date | None, date | None]:
        start = (
            date.fromisoformat(self.from_date.date().toString("yyyy-MM-dd"))
            if self.from_check.isChecked()
            else None
        )
        end = (
            date.fromisoformat(self.to_date.date().toString("yyyy-MM-dd"))
            if self.to_check.isChecked()
            else None
        )
        return start, end

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


def _add_any_combo_items(combo: QComboBox, values: tuple[str, ...]) -> None:
    combo.addItem("Any", "")
    for value in values:
        combo.addItem(value, value)


def _optional_float(
    text: str,
    widget: QWidget,
    label: str,
    *,
    minimum: float | None = 0.0,
    maximum: float | None = None,
) -> float | None:
    value = text.strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError as error:
        raise _FilterValidationError(f"{label} must be a number.", widget) from error
    if not math.isfinite(parsed) or (minimum is not None and parsed < minimum) or (
        maximum is not None and parsed > maximum
    ):
        if maximum is None:
            message = f"{label} must be at least {minimum:g}."
        else:
            message = f"{label} must be between {minimum:g} and {maximum:g}."
        raise _FilterValidationError(message, widget)
    return parsed


def _optional_positive_int(text: str, widget: QWidget, label: str) -> int | None:
    value = text.strip()
    if not value:
        return None
    if not value.isdigit() or int(value) <= 0:
        raise _FilterValidationError(f"{label} must be a positive integer.", widget)
    return int(value)


def _integer_set(text: str, widget: QWidget, label: str) -> frozenset[int]:
    values: set[int] = set()
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        if not item.isdigit() or int(item) <= 0:
            raise _FilterValidationError(
                f"{label} must contain only positive integers separated by commas.",
                widget,
            )
        values.add(int(item))
    return frozenset(values)


def _format_score(value: float | int | None) -> str:
    return "Not recorded" if value is None else f"{float(value):.2f} / 5"


def _format_speed(value: float | int | None) -> str:
    return "Unavailable" if value is None else f"{float(value):.1f} tok/s"


def _format_number(value: float | int | None) -> str:
    return "Not recorded" if value is None else f"{float(value):.2f}"


def _format_percent(value: float | int | None) -> str:
    return "Unavailable" if value is None else f"{float(value):.1f}%"


def _format_distribution(distribution: Any) -> str:
    if getattr(distribution, "observed_count", 0) <= 0:
        return "Not recorded"
    return ", ".join(
        f"{category} ({_format_percent(distribution.percentages.get(category))})"
        for category in distribution.counts
    )


def _format_date(value: object) -> str:
    return "Unavailable" if value in (None, "") else str(value)


def _format_date_range(start: object, end: object) -> str:
    first = _format_date(start)
    last = _format_date(end)
    if first == "Unavailable":
        return last
    if last == "Unavailable" or first == last:
        return first
    return f"{first} to {last}"


def _format_summary_value(summary: Any, *, kind: str = "number") -> str:
    value = getattr(summary, "mean", None)
    if kind == "score":
        return _format_score(value)
    if kind == "speed":
        return _format_speed(value)
    return _format_number(value)


def _format_direction(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw).replace("_", " ")


class _BenchmarkFilterPanel(QGroupBox):
    filters_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Benchmark Run filters", parent)
        self.setObjectName("comparisonBenchmarkFilters")
        self.benchmark_edit = self._line_edit("comparisonBenchmarkFilter", "Any benchmark")
        self.benchmark_type = QComboBox()
        self.benchmark_type.setObjectName("comparisonBenchmarkTypeFilter")
        self.benchmark_type.setAccessibleName("Benchmark type filter")
        _add_any_combo_items(self.benchmark_type, BENCHMARK_TYPES)
        self.session_edit = self._line_edit("comparisonSessionFilter", "Any session text")
        self.session_id = QComboBox()
        self.session_id.setObjectName("comparisonSessionIdFilter")
        self.session_id.setAccessibleName("Benchmark session ID filter")
        self.hardware_edit = self._line_edit("comparisonHardwareFilter", "Any hardware")
        self.hardware_profile_id = QComboBox()
        self.hardware_profile_id.setObjectName("comparisonHardwareProfileFilter")
        self.hardware_profile_id.setAccessibleName("Hardware profile filter")
        self.date_range = _OptionalDateRange("comparisonBenchmark", self)
        self.minimum_score = self._line_edit("comparisonMinimumScoreFilter", "No minimum score")
        self.maximum_score = self._line_edit("comparisonMaximumScoreFilter", "No maximum score")
        self.hallucination = QComboBox()
        self.hallucination.setObjectName("comparisonHallucinationFilter")
        self.hallucination.setAccessibleName("Hallucination level filter")
        _add_any_combo_items(self.hallucination, LEVELS)
        self.reliability = QComboBox()
        self.reliability.setObjectName("comparisonReliabilityFilter")
        self.reliability.setAccessibleName("Reliability level filter")
        _add_any_combo_items(self.reliability, LEVELS)
        self.include_run_ids = self._line_edit("comparisonIncludeRunIds", "Include run IDs, comma separated")
        self.exclude_run_ids = self._line_edit("comparisonExcludeRunIds", "Exclude run IDs, comma separated")
        self.include_deleted = QCheckBox("Include deleted records")
        self.include_deleted.setObjectName("comparisonBenchmarkIncludeDeleted")
        self.include_deleted.setAccessibleName("Include deleted Benchmark Run records")
        self.session_label = QLabel("Session")
        self.session_id_label = QLabel("Session ID")

        layout = QGridLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)
        layout.addWidget(QLabel("Benchmark"), 0, 0)
        layout.addWidget(self.benchmark_edit, 0, 1)
        layout.addWidget(QLabel("Type"), 0, 2)
        layout.addWidget(self.benchmark_type, 0, 3)
        layout.addWidget(self.session_label, 1, 0)
        layout.addWidget(self.session_edit, 1, 1)
        layout.addWidget(self.session_id_label, 1, 2)
        layout.addWidget(self.session_id, 1, 3)
        layout.addWidget(QLabel("Hardware"), 2, 0)
        layout.addWidget(self.hardware_edit, 2, 1)
        layout.addWidget(QLabel("Hardware profile"), 2, 2)
        layout.addWidget(self.hardware_profile_id, 2, 3)
        layout.addWidget(QLabel("Created date"), 3, 0)
        layout.addWidget(self.date_range, 3, 1, 1, 3)
        layout.addWidget(QLabel("Score range"), 4, 0)
        layout.addWidget(self.minimum_score, 4, 1)
        layout.addWidget(self.maximum_score, 4, 3)
        layout.addWidget(QLabel("Hallucination"), 5, 0)
        layout.addWidget(self.hallucination, 5, 1)
        layout.addWidget(QLabel("Reliability"), 5, 2)
        layout.addWidget(self.reliability, 5, 3)
        layout.addWidget(QLabel("Include run IDs"), 6, 0)
        layout.addWidget(self.include_run_ids, 6, 1)
        layout.addWidget(QLabel("Exclude run IDs"), 6, 2)
        layout.addWidget(self.exclude_run_ids, 6, 3)
        layout.addWidget(self.include_deleted, 7, 0, 1, 4)
        for column in (1, 3):
            layout.setColumnStretch(column, 1)

        for control in (
            self.benchmark_edit,
            self.benchmark_type,
            self.session_edit,
            self.session_id,
            self.hardware_edit,
            self.hardware_profile_id,
            self.minimum_score,
            self.maximum_score,
            self.hallucination,
            self.reliability,
            self.include_run_ids,
            self.exclude_run_ids,
            self.include_deleted,
        ):
            _configure_comparison_control(
                control,
                expand_horizontally=not isinstance(control, QCheckBox),
            )
            if isinstance(control, QLineEdit):
                control.textChanged.connect(self.filters_changed)
            elif isinstance(control, QComboBox):
                control.currentIndexChanged.connect(self.filters_changed)
            else:
                control.toggled.connect(self.filters_changed)
        self.date_range.changed.connect(self.filters_changed)

    @staticmethod
    def _line_edit(object_name: str, placeholder: str) -> QLineEdit:
        editor = QLineEdit()
        editor.setObjectName(object_name)
        editor.setAccessibleName(placeholder)
        editor.setPlaceholderText(placeholder)
        editor.setClearButtonEnabled(True)
        _configure_comparison_control(editor)
        return editor

    def set_sessions(self, sessions: list[BenchmarkSession]) -> None:
        blocker = QSignalBlocker(self.session_id)
        self.session_id.clear()
        self.session_id.addItem("Any session", None)
        for session in sessions:
            label = session.title.strip() or "Unknown session"
            if session.id is not None:
                label = f"{label} (#{session.id})"
            if session.is_deleted:
                label = f"{label} [deleted]"
            self.session_id.addItem(label, session.id)
        del blocker

    def set_hardware_profiles(self, profiles: list[Any]) -> None:
        blocker = QSignalBlocker(self.hardware_profile_id)
        self.hardware_profile_id.clear()
        self.hardware_profile_id.addItem("Any hardware profile", None)
        for profile in profiles:
            self.hardware_profile_id.addItem(str(profile.name), profile.id)
        del blocker

    def set_session_mode(self, enabled: bool) -> None:
        self.session_label.setVisible(not enabled)
        self.session_edit.setVisible(not enabled)
        self.session_id_label.setVisible(not enabled)
        self.session_id.setVisible(not enabled)

    def clear(self) -> None:
        blockers = (
            QSignalBlocker(self.benchmark_edit),
            QSignalBlocker(self.benchmark_type),
            QSignalBlocker(self.session_edit),
            QSignalBlocker(self.session_id),
            QSignalBlocker(self.hardware_edit),
            QSignalBlocker(self.hardware_profile_id),
            QSignalBlocker(self.minimum_score),
            QSignalBlocker(self.maximum_score),
            QSignalBlocker(self.hallucination),
            QSignalBlocker(self.reliability),
            QSignalBlocker(self.include_run_ids),
            QSignalBlocker(self.exclude_run_ids),
            QSignalBlocker(self.include_deleted),
        )
        self.benchmark_edit.clear()
        self.benchmark_type.setCurrentIndex(0)
        self.session_edit.clear()
        self.session_id.setCurrentIndex(0)
        self.hardware_edit.clear()
        self.hardware_profile_id.setCurrentIndex(0)
        self.minimum_score.clear()
        self.maximum_score.clear()
        self.hallucination.setCurrentIndex(0)
        self.reliability.setCurrentIndex(0)
        self.include_run_ids.clear()
        self.exclude_run_ids.clear()
        self.include_deleted.setChecked(False)
        self.date_range.clear()
        del blockers

    def filters(self, *, session_mode: bool) -> BenchmarkStatisticsFilters:
        start, end = self.date_range.values()
        if start is not None and end is not None and start > end:
            raise _FilterValidationError("The created date range is reversed.", self.date_range)
        minimum = _optional_float(
            self.minimum_score.text(),
            self.minimum_score,
            "Minimum score",
            maximum=5.0,
        )
        maximum = _optional_float(
            self.maximum_score.text(),
            self.maximum_score,
            "Maximum score",
            maximum=5.0,
        )
        if minimum is not None and maximum is not None and minimum > maximum:
            raise _FilterValidationError("Minimum score cannot exceed maximum score.", self.minimum_score)
        return BenchmarkStatisticsFilters(
            benchmark=self.benchmark_edit.text().strip(),
            benchmark_type=str(self.benchmark_type.currentData() or ""),
            session="" if session_mode else self.session_edit.text().strip(),
            session_id=None if session_mode else self.session_id.currentData(),
            hardware=self.hardware_edit.text().strip(),
            hardware_profile_id=None if self.hardware_profile_id.currentData() is None else int(self.hardware_profile_id.currentData()),
            date_from=start,
            date_to=end,
            min_score=minimum,
            max_score=maximum,
            hallucination=str(self.hallucination.currentData() or ""),
            reliability=str(self.reliability.currentData() or ""),
            include_run_ids=_integer_set(self.include_run_ids.text(), self.include_run_ids, "Include run IDs"),
            exclude_run_ids=_integer_set(self.exclude_run_ids.text(), self.exclude_run_ids, "Exclude run IDs"),
            include_deleted=self.include_deleted.isChecked(),
        )


class _ScoreboardFilterPanel(QGroupBox):
    filters_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Scoreboard filters", parent)
        self.setObjectName("comparisonScoreboardFilters")
        self.batch_id = self._line_edit("comparisonBatchIdFilter", "Any import batch ID")
        self.date_range = _OptionalDateRange("comparisonScoreboard", self)
        self.minimum_score = self._line_edit("comparisonScoreboardMinimumScoreFilter", "No minimum score")
        self.maximum_score = self._line_edit("comparisonScoreboardMaximumScoreFilter", "No maximum score")
        self.hallucination = self._line_edit("comparisonScoreboardHallucinationFilter", "Any hallucination text")
        self.consistency = self._line_edit("comparisonScoreboardConsistencyFilter", "Any consistency text")
        self.reliability = self._line_edit("comparisonScoreboardReliabilityFilter", "Any reliability text")
        self.include_deleted = QCheckBox("Include deleted entries")
        self.include_deleted.setObjectName("comparisonScoreboardIncludeDeleted")
        self.include_deleted.setAccessibleName("Include deleted Scoreboard entries")

        layout = QGridLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)
        layout.addWidget(QLabel("Batch ID"), 0, 0)
        layout.addWidget(self.batch_id, 0, 1)
        layout.addWidget(QLabel("Imported date"), 0, 2)
        layout.addWidget(self.date_range, 0, 3)
        layout.addWidget(QLabel("Score range"), 1, 0)
        layout.addWidget(self.minimum_score, 1, 1)
        layout.addWidget(self.maximum_score, 1, 3)
        layout.addWidget(QLabel("Hallucination"), 2, 0)
        layout.addWidget(self.hallucination, 2, 1)
        layout.addWidget(QLabel("Consistency"), 2, 2)
        layout.addWidget(self.consistency, 2, 3)
        layout.addWidget(QLabel("Reliability"), 3, 0)
        layout.addWidget(self.reliability, 3, 1)
        layout.addWidget(self.include_deleted, 4, 0, 1, 4)
        for column in (1, 3):
            layout.setColumnStretch(column, 1)

        for control in (
            self.batch_id,
            self.minimum_score,
            self.maximum_score,
            self.hallucination,
            self.consistency,
            self.reliability,
        ):
            _configure_comparison_control(control)
            control.textChanged.connect(self.filters_changed)
        _configure_comparison_control(self.include_deleted, expand_horizontally=False)
        self.include_deleted.toggled.connect(self.filters_changed)
        self.date_range.changed.connect(self.filters_changed)

    @staticmethod
    def _line_edit(object_name: str, placeholder: str) -> QLineEdit:
        editor = QLineEdit()
        editor.setObjectName(object_name)
        editor.setAccessibleName(placeholder)
        editor.setPlaceholderText(placeholder)
        editor.setClearButtonEnabled(True)
        _configure_comparison_control(editor)
        return editor

    def clear(self) -> None:
        blockers = (
            QSignalBlocker(self.batch_id),
            QSignalBlocker(self.minimum_score),
            QSignalBlocker(self.maximum_score),
            QSignalBlocker(self.hallucination),
            QSignalBlocker(self.consistency),
            QSignalBlocker(self.reliability),
            QSignalBlocker(self.include_deleted),
        )
        for editor in (
            self.batch_id,
            self.minimum_score,
            self.maximum_score,
            self.hallucination,
            self.consistency,
            self.reliability,
        ):
            editor.clear()
        self.include_deleted.setChecked(False)
        self.date_range.clear()
        del blockers

    def filters(self) -> ScoreboardStatisticsFilters:
        start, end = self.date_range.values()
        if start is not None and end is not None and start > end:
            raise _FilterValidationError("The imported date range is reversed.", self.date_range)
        minimum = _optional_float(self.minimum_score.text(), self.minimum_score, "Minimum score")
        maximum = _optional_float(self.maximum_score.text(), self.maximum_score, "Maximum score")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise _FilterValidationError("Minimum score cannot exceed maximum score.", self.minimum_score)
        return ScoreboardStatisticsFilters(
            batch_id=_optional_positive_int(self.batch_id.text(), self.batch_id, "Batch ID"),
            date_from=start,
            date_to=end,
            min_score=minimum,
            max_score=maximum,
            hallucination=self.hallucination.text().strip(),
            consistency=self.consistency.text().strip(),
            reliability=self.reliability.text().strip(),
            include_deleted=self.include_deleted.isChecked(),
        )


@dataclass(frozen=True)
class _Selection:
    key: str
    label: str
    value: str | BenchmarkSession
    tooltip: str
    records: int | None = None
    availability: str = "Available"
    selectable: bool = True
    status: str = ""


STATE_COPY = {
    ComparisonResultState.READY: (
        "Comparison ready",
        "The selected subjects were compared using the active filters.",
    ),
    ComparisonResultState.READY_WITH_MISSING_VALUES: (
        "Comparison ready with missing values",
        "Some requested metrics have no values for one or more subjects.",
    ),
    ComparisonResultState.NO_SOURCE_DATA: (
        "No source data",
        "No eligible records are available from the selected source.",
    ),
    ComparisonResultState.INSUFFICIENT_SUBJECTS: (
        "Select at least two subjects",
        "A comparison requires at least two distinct subjects.",
    ),
    ComparisonResultState.SELECTED_SUBJECTS_UNAVAILABLE: (
        "Selected subjects unavailable",
        "One or more selected subjects have no eligible records under the active filters.",
    ),
    ComparisonResultState.FILTERS_NO_RECORDS: (
        "No records match filters",
        "The active filters produced no eligible records.",
    ),
}


class ComparisonsView(QWidget):
    """One reusable, read-only comparison workflow over typed engine services."""

    status_message = Signal(str)

    def __init__(self, context: GuiApplicationContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.setObjectName("comparisonsPage")
        self.setAccessibleName("Comparisons page")
        self._has_loaded = False
        self._source = ComparisonSource.BENCHMARK_RUNS
        self._dimension = ComparisonDimension.MODELS
        self._available: dict[str, _Selection] = {}
        self._selected: list[_Selection] = []
        self._discovery_state: ComparisonResultState | None = None
        self._discovery_warnings: tuple[Any, ...] = ()
        self._result: Any | None = None
        self.pairwise_direction_label: QLabel | None = None
        self._busy = False
        self._build_ui()

    @property
    def has_loaded(self) -> bool:
        return self._has_loaded

    @property
    def current_result(self) -> Any | None:
        return self._result

    def _build_ui(self) -> None:
        title = QLabel("Comparisons")
        title.setObjectName("pageTitle")
        title.setAccessibleName("Comparisons page title")
        description = QLabel(
            "Compare historical Benchmark Runs or Scoreboards using the committed comparison engine."
        )
        description.setObjectName("pageDescription")
        description.setWordWrap(True)

        self.refresh_subjects_button = QPushButton("Refresh Subjects")
        self.refresh_subjects_button.setObjectName("refreshComparisonSubjects")
        self.refresh_subjects_button.setAccessibleName("Refresh comparison subjects")
        _configure_comparison_control(self.refresh_subjects_button, expand_horizontally=False)
        self.refresh_subjects_button.clicked.connect(self.refresh)
        self.refresh_status = QLabel("Ready")
        self.refresh_status.setObjectName("comparisonRefreshStatus")
        self.refresh_status.setAccessibleName("Comparison refresh status")
        header_actions = QHBoxLayout()
        header_actions.addWidget(self.refresh_status)
        header_actions.addStretch(1)
        header_actions.addWidget(self.refresh_subjects_button)

        self.source_selector = QComboBox()
        self.source_selector.setObjectName("comparisonSourceSelector")
        self.source_selector.setAccessibleName("Comparison source")
        self.source_selector.addItem("Benchmark Runs", ComparisonSource.BENCHMARK_RUNS.value)
        self.source_selector.addItem("Scoreboards", ComparisonSource.SCOREBOARDS.value)
        _configure_comparison_control(self.source_selector)
        self.dimension_selector = QComboBox()
        self.dimension_selector.setObjectName("comparisonDimensionSelector")
        self.dimension_selector.setAccessibleName("Comparison dimension")
        self.dimension_selector.addItem("Models", ComparisonDimension.MODELS.value)
        self.dimension_selector.addItem("Sessions", ComparisonDimension.SESSIONS.value)
        self.dimension_selector.addItem("Benchmarks", ComparisonDimension.BENCHMARKS.value)
        _configure_comparison_control(self.dimension_selector)
        self.source_selector.currentIndexChanged.connect(self._on_source_changed)
        self.dimension_selector.currentIndexChanged.connect(self._on_dimension_changed)

        self.benchmark_filters = _BenchmarkFilterPanel(self)
        self.scoreboard_filters = _ScoreboardFilterPanel(self)
        self.benchmark_filters.filters_changed.connect(self._on_filter_changed)
        self.scoreboard_filters.filters_changed.connect(self._on_filter_changed)
        self._populate_filter_catalogs()
        self.benchmark_filters.set_session_mode(False)
        self.filter_stack = QStackedWidget()
        self.filter_stack.setObjectName("comparisonFilterStack")
        self.filter_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.filter_stack.addWidget(self.benchmark_filters)
        self.filter_stack.addWidget(self.scoreboard_filters)
        configuration = QGroupBox("Comparison configuration")
        configuration.setObjectName("comparisonConfiguration")
        configuration.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        configuration_layout = QVBoxLayout(configuration)
        configuration_layout.setSpacing(10)
        selectors = QHBoxLayout()
        selectors.setSpacing(10)
        selectors.addWidget(QLabel("Source"))
        selectors.addWidget(self.source_selector, 1)
        selectors.addWidget(QLabel("Dimension"))
        selectors.addWidget(self.dimension_selector, 1)
        configuration_layout.addLayout(selectors)
        configuration_layout.addWidget(self.filter_stack)
        self.filter_error = QLabel()
        self.filter_error.setObjectName("comparisonFilterError")
        self.filter_error.setAccessibleName("Comparison filter error")
        self.filter_error.setWordWrap(True)
        self.filter_error.setVisible(False)
        configuration_layout.addWidget(self.filter_error)
        self.apply_filters_button = QPushButton("Apply Filters / Refresh Subjects")
        self.apply_filters_button.setObjectName("applyComparisonFilters")
        self.apply_filters_button.setAccessibleName("Apply comparison filters and refresh subjects")
        self.apply_filters_button.setEnabled(False)
        _configure_comparison_control(self.apply_filters_button, expand_horizontally=False)
        self.apply_filters_button.clicked.connect(self._apply_filters)
        self.clear_filters_button = QPushButton("Clear Filters")
        self.clear_filters_button.setObjectName("clearComparisonFilters")
        self.clear_filters_button.setAccessibleName("Clear comparison filters")
        self.clear_filters_button.setEnabled(False)
        _configure_comparison_control(self.clear_filters_button, expand_horizontally=False)
        self.clear_filters_button.clicked.connect(self._clear_filters)
        filter_actions = QHBoxLayout()
        filter_actions.setSpacing(8)
        filter_actions.addStretch(1)
        filter_actions.addWidget(self.apply_filters_button)
        filter_actions.addWidget(self.clear_filters_button)
        configuration_layout.addLayout(filter_actions)

        self.available_search = QLineEdit()
        self.available_search.setObjectName("comparisonAvailableSearch")
        self.available_search.setAccessibleName("Search available comparison subjects")
        self.available_search.setPlaceholderText("Search available subjects")
        _configure_comparison_control(self.available_search)
        self.available_model = ComparisonSubjectTableModel(self)
        self.available_proxy = QSortFilterProxyModel(self)
        self.available_proxy.setSourceModel(self.available_model)
        self.available_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.available_proxy.setFilterKeyColumn(-1)
        self.available_table = QTableView()
        self.available_table.setObjectName("comparisonAvailableTable")
        self.available_table.setAccessibleName("Available comparison subjects")
        self.available_table.setModel(self.available_proxy)
        self.available_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.available_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.available_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.available_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.available_table.setSortingEnabled(False)
        self.available_table.setWordWrap(False)
        self.available_table.horizontalHeader().setStretchLastSection(True)
        self.available_table.verticalHeader().setVisible(False)
        self.available_search.textChanged.connect(self.available_proxy.setFilterFixedString)
        self.available_table.doubleClicked.connect(self._add_selected_available)
        self.available_table.selectionModel().selectionChanged.connect(
            lambda *_args: self._update_selection_actions()
        )

        available_panel = QGroupBox("Available subjects")
        available_panel.setObjectName("comparisonAvailablePanel")
        available_panel.setMinimumHeight(_COMPARISON_SELECTION_PANEL_MIN_HEIGHT)
        available_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        available_layout = QVBoxLayout(available_panel)
        available_layout.setSpacing(8)
        available_layout.addWidget(self.available_search)
        available_layout.addWidget(self.available_table, 1)

        self.add_subject_button = QPushButton("Add →")
        self.add_subject_button.setObjectName("addComparisonSubject")
        self.add_subject_button.setAccessibleName("Add selected comparison subject")
        _configure_comparison_control(self.add_subject_button, expand_horizontally=False)
        self.add_subject_button.clicked.connect(self._add_selected_available)
        self.remove_subject_button = QPushButton("← Remove")
        self.remove_subject_button.setObjectName("removeComparisonSubject")
        self.remove_subject_button.setAccessibleName("Remove selected comparison subject")
        _configure_comparison_control(self.remove_subject_button, expand_horizontally=False)
        self.remove_subject_button.clicked.connect(self._remove_selected_subject)
        self.move_up_button = QPushButton("Move Up")
        self.move_up_button.setObjectName("moveComparisonSubjectUp")
        self.move_up_button.setAccessibleName("Move selected comparison subject up")
        _configure_comparison_control(self.move_up_button, expand_horizontally=False)
        self.move_up_button.clicked.connect(lambda _checked=False: self._move_selected(-1))
        self.move_down_button = QPushButton("Move Down")
        self.move_down_button.setObjectName("moveComparisonSubjectDown")
        self.move_down_button.setAccessibleName("Move selected comparison subject down")
        _configure_comparison_control(self.move_down_button, expand_horizontally=False)
        self.move_down_button.clicked.connect(lambda _checked=False: self._move_selected(1))
        self.clear_selection_button = QPushButton("Clear")
        self.clear_selection_button.setObjectName("clearComparisonSelection")
        self.clear_selection_button.setAccessibleName("Clear selected comparison subjects")
        _configure_comparison_control(self.clear_selection_button, expand_horizontally=False)
        self.clear_selection_button.clicked.connect(self._clear_selection)
        selection_actions = QVBoxLayout()
        selection_actions.setSpacing(6)
        selection_actions.addStretch(1)
        selection_actions.addWidget(self.add_subject_button)
        selection_actions.addWidget(self.remove_subject_button)
        selection_actions.addWidget(self.move_up_button)
        selection_actions.addWidget(self.move_down_button)
        selection_actions.addWidget(self.clear_selection_button)
        selection_actions.addStretch(1)
        selection_actions_widget = QWidget()
        selection_actions_widget.setLayout(selection_actions)

        self.selected_list = QListWidget()
        self.selected_list.setObjectName("comparisonSelectedList")
        self.selected_list.setAccessibleName("Ordered selected comparison subjects")
        self.selected_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.selected_list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.selected_list.currentRowChanged.connect(lambda _row: self._update_selection_actions())
        selected_panel = QGroupBox("Selected subjects")
        selected_panel.setObjectName("comparisonSelectedPanel")
        selected_panel.setMinimumHeight(_COMPARISON_SELECTION_PANEL_MIN_HEIGHT)
        selected_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        selected_layout = QVBoxLayout(selected_panel)
        selected_layout.setSpacing(8)
        self.selected_count = QLabel("0 selected")
        self.selected_count.setObjectName("comparisonSelectedCount")
        self.selected_count.setAccessibleName("Selected comparison subject count")
        self.baseline_label = QLabel("Baseline: none")
        self.baseline_label.setObjectName("comparisonBaseline")
        self.baseline_label.setAccessibleName("Comparison baseline")
        selected_layout.addWidget(self.selected_count)
        selected_layout.addWidget(self.baseline_label)
        selected_layout.addWidget(self.selected_list, 1)

        self.selection_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.selection_splitter.setObjectName("comparisonSelectionSplitter")
        self.selection_splitter.setMinimumHeight(_COMPARISON_SELECTION_PANEL_MIN_HEIGHT)
        self.selection_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.selection_splitter.addWidget(available_panel)
        self.selection_splitter.addWidget(selection_actions_widget)
        self.selection_splitter.addWidget(selected_panel)
        self.selection_splitter.setStretchFactor(0, 3)
        self.selection_splitter.setStretchFactor(1, 0)
        self.selection_splitter.setStretchFactor(2, 2)

        self.compare_button = QPushButton("Compare")
        self.compare_button.setObjectName("compareSubjects")
        self.compare_button.setAccessibleName("Compare selected subjects")
        self.compare_button.setEnabled(False)
        _configure_comparison_control(self.compare_button, expand_horizontally=False)
        self.compare_button.clicked.connect(self._compare)
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        action_row.addStretch(1)
        action_row.addWidget(self.compare_button)

        self.state_banner = QLabel("Select a source and refresh subjects.")
        self.state_banner.setObjectName("comparisonStateBanner")
        self.state_banner.setAccessibleName("Comparison state")
        self.state_banner.setWordWrap(True)
        self.active_filter_summary = QLabel("No active filters")
        self.active_filter_summary.setObjectName("comparisonActiveFilters")
        self.active_filter_summary.setAccessibleName("Active comparison filters")
        self.active_filter_summary.setWordWrap(True)
        self.warning_list = QListWidget()
        self.warning_list.setObjectName("comparisonWarnings")
        self.warning_list.setAccessibleName("Comparison warnings")
        self.warning_list.setMaximumHeight(110)
        self.warning_list.setVisible(False)
        self.results_tabs = QTabWidget()
        self.results_tabs.setObjectName("comparisonResultsTabs")
        self.results_tabs.setAccessibleName("Comparison results")
        self.result_tables: dict[str, ComparisonTableModel] = {}
        self.results_placeholder = QLabel("Select at least two subjects, then choose Compare.")
        self.results_placeholder.setObjectName("comparisonResultsPlaceholder")
        self.results_placeholder.setWordWrap(True)
        self.results_tabs.addTab(self.results_placeholder, "Results")

        content = QWidget()
        content.setObjectName("comparisonPageContent")
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(30, 28, 30, 30)
        content_layout.setSpacing(12)
        content_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        content_layout.addLayout(header_actions)
        content_layout.addWidget(title)
        content_layout.addWidget(description)
        content_layout.addWidget(configuration)
        content_layout.addWidget(self.selection_splitter, 1)
        content_layout.addLayout(action_row)
        content_layout.addWidget(self.state_banner)
        content_layout.addWidget(self.active_filter_summary)
        content_layout.addWidget(self.warning_list)
        content_layout.addWidget(self.results_tabs, 2)
        content_layout.addStretch(1)

        self.content_scroll = QScrollArea()
        self.content_scroll.setObjectName("comparisonContentScroll")
        self.content_scroll.setAccessibleName("Scrollable Comparisons content")
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.content_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.content_scroll.setWidget(content)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.content_scroll, 1)
        self._update_selection_actions()

    def _populate_filter_catalogs(self) -> None:
        try:
            self.benchmark_filters.set_sessions(self.context.catalog.list_sessions(include_deleted=True))
            self.benchmark_filters.set_hardware_profiles(self.context.catalog.list_hardware_profiles())
        except Exception as error:
            self.context.logger.error(
                "Comparison filter catalog refresh failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    def _on_filter_changed(self, *_args: object) -> None:
        self._clear_result_for_configuration_change()
        self._discovery_state = None
        self.apply_filters_button.setEnabled(True)
        self.clear_filters_button.setEnabled(True)

    def _clear_filter_error(self) -> None:
        error_label = getattr(self, "filter_error", None)
        if error_label is not None:
            error_label.setVisible(False)
            error_label.clear()

    def _show_filter_error(self, error: _FilterValidationError) -> None:
        self.filter_error.setText(str(error))
        self.filter_error.setVisible(True)
        error.widget.setFocus()

    def _current_filters(self) -> BenchmarkStatisticsFilters | ScoreboardStatisticsFilters:
        if self._source is ComparisonSource.SCOREBOARDS:
            return self.scoreboard_filters.filters()
        return self.benchmark_filters.filters(session_mode=self._dimension is ComparisonDimension.SESSIONS)

    def _apply_filters(self, *_args: object) -> None:
        self._clear_filter_error()
        try:
            filters = self._current_filters()
        except _FilterValidationError as error:
            self._show_filter_error(error)
            return
        self._invalidate_result_state()
        self._discover(filters)
        self.apply_filters_button.setEnabled(False)
        self.clear_filters_button.setEnabled(True)

    def _clear_filters(self, *_args: object) -> None:
        if self._source is ComparisonSource.SCOREBOARDS:
            self.scoreboard_filters.clear()
        else:
            self.benchmark_filters.clear()
        self._clear_filter_error()
        self._invalidate_result_state()
        self._discover()
        self.apply_filters_button.setEnabled(False)
        self.clear_filters_button.setEnabled(False)

    def _on_source_changed(self, _index: int) -> None:
        value = self.source_selector.currentData()
        self._source = ComparisonSource(value)
        if self._source is ComparisonSource.SCOREBOARDS:
            self._set_dimension_options(include_benchmarks=False)
            self.dimension_selector.setEnabled(False)
            self.filter_stack.setCurrentIndex(1)
        else:
            self._set_dimension_options(include_benchmarks=True)
            self.dimension_selector.setEnabled(True)
            self.filter_stack.setCurrentIndex(0)
        self._reset_mode_state()
        self._discover()

    def _on_dimension_changed(self, _index: int) -> None:
        value = self.dimension_selector.currentData()
        self._dimension = ComparisonDimension(value)
        self._reset_mode_state()
        self._discover()

    def _set_dimension_options(self, *, include_benchmarks: bool) -> None:
        """Keep the source/dimension matrix explicit and source-safe."""

        current_value = self.dimension_selector.currentData()
        options = [("Models", ComparisonDimension.MODELS.value)]
        if include_benchmarks:
            options.extend(
                (
                    ("Sessions", ComparisonDimension.SESSIONS.value),
                    ("Benchmarks", ComparisonDimension.BENCHMARKS.value),
                )
            )
        with QSignalBlocker(self.dimension_selector):
            self.dimension_selector.clear()
            for label, option_value in options:
                self.dimension_selector.addItem(label, option_value)
            values = [option_value for _label, option_value in options]
            target = current_value if current_value in values else ComparisonDimension.MODELS.value
            self.dimension_selector.setCurrentIndex(values.index(target))
        self._dimension = ComparisonDimension(self.dimension_selector.currentData())

    def _reset_mode_state(self) -> None:
        self._available.clear()
        self.available_model.set_rows(())
        self._selected.clear()
        self._discovery_state = None
        self._discovery_warnings = ()
        self._invalidate_result_state(message="Refreshing subjects...")
        self._render_selection()
        self.benchmark_filters.clear()
        self.scoreboard_filters.clear()
        self.benchmark_filters.set_session_mode(self._dimension is ComparisonDimension.SESSIONS)
        self.apply_filters_button.setEnabled(False)
        self.clear_filters_button.setEnabled(False)

    def _discover(
        self,
        filters: BenchmarkStatisticsFilters | ScoreboardStatisticsFilters | None = None,
    ) -> None:
        self.refresh_subjects_button.setEnabled(False)
        self.refresh_status.setText("Refreshing...")
        try:
            active_filters = filters if filters is not None else self._current_filters()
            if self._source is ComparisonSource.BENCHMARK_RUNS and self._dimension is ComparisonDimension.MODELS:
                discovery = self.context.comparisons.discover_benchmark_model_subjects(
                    filters=active_filters  # type: ignore[arg-type]
                )
                selections = tuple(self._subject_selection(subject) for subject in discovery.subjects)
                self._discovery_state = discovery.state
                self._discovery_warnings = discovery.warnings
            elif (
                self._source is ComparisonSource.BENCHMARK_RUNS
                and self._dimension is ComparisonDimension.BENCHMARKS
            ):
                discovery = self.context.comparisons.discover_benchmark_subjects(
                    filters=active_filters  # type: ignore[arg-type]
                )
                selections = tuple(self._subject_selection(subject) for subject in discovery.subjects)
                self._discovery_state = discovery.state
                self._discovery_warnings = discovery.warnings
            elif self._source is ComparisonSource.SCOREBOARDS:
                discovery = self.context.comparisons.discover_scoreboard_model_subjects(
                    filters=active_filters  # type: ignore[arg-type]
                )
                selections = tuple(self._subject_selection(subject) for subject in discovery.subjects)
                self._discovery_state = discovery.state
                self._discovery_warnings = discovery.warnings
            else:
                if not isinstance(active_filters, BenchmarkStatisticsFilters):
                    raise TypeError("Benchmark Run session mode requires BenchmarkStatisticsFilters")
                sessions = self.context.catalog.list_sessions(include_deleted=active_filters.include_deleted)
                selections = tuple(self._session_selection(session) for session in sessions)
                self._discovery_state = None
                self._discovery_warnings = ()
            self._set_available(selections)
            self._render_warnings(self._discovery_warnings)
            if self._discovery_state is not None:
                self._render_state(self._discovery_state)
            elif selections:
                self.state_banner.setText("Sessions ready. Select at least two subjects.")
            else:
                self.state_banner.setText("No sessions are available.")
            self._has_loaded = True
            self.refresh_status.setText("Updated")
        except _FilterValidationError as error:
            self._show_filter_error(error)
            self._has_loaded = False
            self.state_banner.setText("Fix the highlighted filter before refreshing subjects.")
            self.refresh_status.setText("Invalid filters")
        except Exception as error:
            self.context.logger.error(
                "Comparison subject refresh failed",
                exc_info=(type(error), error, error.__traceback__),
            )
            self._has_loaded = False
            self._available.clear()
            self.available_model.set_rows(())
            self._render_selection()
            self._render_warnings(())
            self.state_banner.setText("Subjects could not be refreshed. Please try again.")
            self.refresh_status.setText("Refresh failed")
        finally:
            self.refresh_subjects_button.setEnabled(True)
            self._update_selection_actions()

    def refresh(self) -> None:
        """Refresh discovery without writing or automatically selecting subjects."""

        self._invalidate_result_state(message="Refreshing subjects...")
        self._discover()

    @staticmethod
    def _session_selection(session: BenchmarkSession) -> _Selection:
        key = (
            f"session:{session.id}"
            if session.id is not None
            else f"session-title:{session.title.strip().casefold()}"
        )
        label = session.title.strip() or "Unknown session"
        if session.id is not None:
            label = f"{label} (#{session.id})"
        if session.is_deleted:
            label = f"{label} [deleted]"
        return _Selection(key, label, session, label)

    @classmethod
    def _subject_selection(cls, subject: ComparisonSubject) -> _Selection:
        tooltip = subject.label
        if subject.status:
            tooltip = f"{tooltip}\nStatus: {subject.status}"
        return _Selection(
            key=subject.identity,
            label=subject.label,
            value=subject.identity,
            tooltip=tooltip,
            records=subject.eligible_record_count,
            availability=cls._subject_availability(subject),
            selectable=subject.selectable,
            status=subject.status,
        )

    @staticmethod
    def _subject_availability(subject: ComparisonSubject) -> str:
        available = []
        if subject.score_available:
            available.append("score")
        if subject.throughput_available:
            available.append("throughput")
        if subject.review_available:
            available.append("review")
        return ", ".join(available) if available else "No metrics recorded"

    def _set_available(self, selections: tuple[_Selection, ...]) -> None:
        previous_selected = tuple(self._selected)
        self._available = {selection.key: selection for selection in selections}
        self._selected = [
            self._available.get(selection.key, replace(selection, selectable=False))
            for selection in previous_selected
        ]
        rows = tuple(
            ComparisonSubjectRow(
                identity=selection.key,
                label=selection.label,
                records=selection.records,
                availability=selection.availability,
                tooltip=selection.tooltip,
                selectable=selection.selectable,
                status=selection.status,
            )
            for selection in selections
        )
        self.available_model.set_rows(rows)
        self._render_selection()

    def _render_selection(self) -> None:
        self.selected_list.clear()
        for index, selection in enumerate(self._selected):
            item = QListWidgetItem(selection.label)
            item.setData(Qt.ItemDataRole.UserRole, selection.key)
            item.setToolTip(selection.tooltip)
            if selection.key not in self._available or not selection.selectable:
                item.setText(f"{selection.label} — unavailable under current filters")
            if index == 0:
                item.setText(f"Baseline — {item.text()}")
            self.selected_list.addItem(item)
        self.selected_count.setText(f"{len(self._selected)} selected")
        self.baseline_label.setText(
            f"Baseline: {self._selected[0].label}" if self._selected else "Baseline: none"
        )
        self._update_selection_actions()

    def _add_selected_available(self, *_args: object) -> None:
        indexes = self.available_table.selectionModel().selectedRows()
        if not indexes:
            return
        source_index = self.available_proxy.mapToSource(indexes[0])
        row = self.available_model.row_at(source_index.row())
        if row is None or not row.selectable or row.identity in {item.key for item in self._selected}:
            return
        selection = self._available.get(row.identity)
        if selection is None or not selection.selectable:
            return
        self._selected.append(selection)
        self._clear_result_for_configuration_change()
        self._render_selection()

    def _remove_selected_subject(self, *_args: object) -> None:
        row = self.selected_list.currentRow()
        if not 0 <= row < len(self._selected):
            return
        self._selected.pop(row)
        self._clear_result_for_configuration_change()
        self._render_selection()

    def _move_selected(self, delta: int) -> None:
        row = self.selected_list.currentRow()
        target = row + delta
        if not 0 <= row < len(self._selected) or not 0 <= target < len(self._selected):
            return
        self._selected[row], self._selected[target] = self._selected[target], self._selected[row]
        self._clear_result_for_configuration_change()
        self._render_selection()
        self.selected_list.setCurrentRow(target)

    def _clear_selection(self, *_args: object) -> None:
        if not self._selected:
            return
        self._selected.clear()
        self._clear_result_for_configuration_change()
        self._render_selection()

    def _invalidate_result_state(
        self,
        *,
        message: str = "Configuration changed. Compare again to update results.",
        placeholder_message: str | None = None,
    ) -> None:
        self._result = None
        self._reset_results(placeholder_message=placeholder_message)
        self._render_warnings(())
        self._render_active_filters({})
        self._clear_filter_error()
        self.state_banner.setToolTip("")
        self.state_banner.setText(message)

    def _clear_result_for_configuration_change(self) -> None:
        self._invalidate_result_state()

    def _compare(self, *_args: object) -> None:
        if self._busy:
            return
        if len(self._selected) < 2:
            self.state_banner.setText("Select at least two subjects before comparing.")
            return
        self._clear_filter_error()
        try:
            filters = self._current_filters()
        except _FilterValidationError as error:
            self._show_filter_error(error)
            return

        self._busy = True
        self.compare_button.setEnabled(False)
        self.refresh_subjects_button.setEnabled(False)
        self.refresh_status.setText("Comparing...")
        try:
            if self._source is ComparisonSource.BENCHMARK_RUNS and self._dimension is ComparisonDimension.MODELS:
                selected_models = tuple(
                    str(selection.value)
                    for selection in self._selected
                    if isinstance(selection.value, str)
                )
                result = self.context.comparisons.compare_benchmark_models(
                    BenchmarkModelComparisonRequest(selected_models=selected_models, filters=filters),  # type: ignore[arg-type]
                )
            elif (
                self._source is ComparisonSource.BENCHMARK_RUNS
                and self._dimension is ComparisonDimension.BENCHMARKS
            ):
                if any(
                    not selection.selectable or selection.key not in self._available
                    for selection in self._selected
                ):
                    self.state_banner.setText(
                        "One or more selected benchmarks are unavailable under the active filters."
                    )
                    return
                result = self.context.comparisons.compare_benchmarks(
                    BenchmarkComparisonRequest(
                        selected_benchmarks=tuple(selection.key for selection in self._selected),
                        filters=filters,  # type: ignore[arg-type]
                    ),
                )
            elif self._source is ComparisonSource.BENCHMARK_RUNS:
                selected_sessions = tuple(
                    selection.value
                    for selection in self._selected
                    if isinstance(selection.value, BenchmarkSession)
                )
                result = self.context.comparisons.compare_sessions(
                    selected_sessions,
                    filters=filters,  # type: ignore[arg-type]
                )
            else:
                selected_models = tuple(
                    str(selection.value)
                    for selection in self._selected
                    if isinstance(selection.value, str)
                )
                result = self.context.comparisons.compare_scoreboard_models(
                    ScoreboardModelComparisonRequest(selected_models=selected_models, filters=filters),  # type: ignore[arg-type]
                )
            self._result = result
            self._render_result(result)
            self.refresh_status.setText("Updated")
            self.status_message.emit("Comparison completed successfully.")
        except Exception as error:
            self.context.logger.error(
                "Comparison failed",
                exc_info=(type(error), error, error.__traceback__),
            )
            self._invalidate_result_state(
                message="Comparison could not be completed. Please review the inputs and try again.",
                placeholder_message="Comparison could not be completed. Review the inputs and try again.",
            )
            self.refresh_status.setText("Comparison failed")
        finally:
            self._busy = False
            self.refresh_subjects_button.setEnabled(True)
            self._update_selection_actions()

    def _render_result(self, result: Any) -> None:
        self._render_state(result.state)
        self._render_warnings(tuple(result.warnings))
        self._render_active_filters(result.metadata.active_filters)
        self._reset_results()
        if not result.entities:
            self.results_placeholder.setText("No comparison entities are available for the current selection.")
            return

        if isinstance(result, ScoreboardModelComparisonResult):
            self._render_scoreboard_summary(result)
        else:
            self._render_benchmark_summary(result)
            self._render_review(result)

        if result.categorical_comparisons:
            self._render_categories(result)
        if isinstance(result, ModelComparisonResult):
            if result.ranking or result.speed_ranking:
                self._render_rankings(result)
            if self._alignment_has_content(result.alignment):
                self._render_model_alignment(result)
        elif isinstance(result, SessionComparisonResult):
            if self._alignment_has_content(result.alignment):
                self._render_session_alignment(result)
        self._render_pairwise(result)
        self._activate_primary_result_tab()

    def _render_active_filters(self, filters: Any) -> None:
        if not filters:
            self.active_filter_summary.setText("Active filters: none")
            return
        parts = [
            f"{str(name).replace('_', ' ').title()}: {value}"
            for name, value in filters.items()
        ]
        self.active_filter_summary.setText("Active filters: " + "; ".join(parts))

    def _add_table_tab(
        self,
        title: str,
        headers: tuple[str, ...],
        rows: tuple[Any, ...],
        *,
        introduction: QLabel | None = None,
        empty_message: str | None = None,
    ) -> ComparisonTableModel:
        container = QWidget()
        model = ComparisonTableModel(headers, container)
        model.set_rows(tuple(rows))
        table = QTableView()
        table.setObjectName(f"comparison{title.replace(' ', '')}Table")
        table.setAccessibleName(f"Comparison {title.lower()} table")
        table.setModel(model)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setMinimumSectionSize(100)
        table.verticalHeader().setVisible(False)
        table.setMinimumHeight(180)
        table.resizeColumnsToContents()
        container_layout = QVBoxLayout(container)
        if introduction is not None:
            container_layout.addWidget(introduction)
        if empty_message and not rows:
            empty = QLabel(empty_message)
            empty.setObjectName(f"comparison{title.replace(' ', '')}Empty")
            empty.setWordWrap(True)
            container_layout.addWidget(empty)
        container_layout.addWidget(table, 1)
        self.result_tables[title] = model
        self.results_tabs.addTab(container, title)
        return model

    @staticmethod
    def _baseline_labels(entities: tuple[Any, ...]) -> dict[str, str]:
        return {
            entity.identity: ("Baseline" if index == 0 else "Compared" if index == 1 else "")
            for index, entity in enumerate(entities)
        }

    def _render_benchmark_summary(self, result: ModelComparisonResult | SessionComparisonResult) -> None:
        headers = (
            "Order",
            "Subject",
            "Records",
            "Scored",
            "Unscored",
            "Mean score",
            "Median score",
            "Mean tokens / second",
            "Reviews",
            "Availability",
        )
        order_labels = self._baseline_labels(result.entities)
        rows = []
        for entity in result.entities:
            summary = entity.summary
            rows.append(
                self._row(
                    (
                        order_labels.get(entity.identity, ""),
                        entity.label,
                        str(summary.total_eligible_runs),
                        str(summary.scored_runs),
                        str(summary.unscored_runs),
                        _format_summary_value(summary.overall_score, kind="score"),
                        _format_score(summary.overall_score.median),
                        _format_summary_value(summary.tokens_per_second, kind="speed"),
                        str(entity.review.total_reviews),
                        "Available" if entity.record_count else "Unavailable",
                    ),
                    tooltip=(entity.identity, entity.label),
                )
            )
        self._add_table_tab("Summary", headers, tuple(rows))

    def _render_scoreboard_summary(self, result: ScoreboardModelComparisonResult) -> None:
        headers = (
            "Order",
            "Model",
            "Entries",
            "Scored",
            "Unscored",
            "Mean score",
            "Median score",
            "Mean tokens / second",
            "Import batches",
            "Imported range",
            "Availability",
        )
        order_labels = self._baseline_labels(result.entities)
        rows = []
        for entity in result.entities:
            summary = entity.summary
            batch_labels = ", ".join(entity.represented_import_batches)
            batches = (
                f"{summary.unique_import_batch_count}: {batch_labels}"
                if batch_labels
                else str(summary.unique_import_batch_count)
            )
            rows.append(
                self._row(
                    (
                        order_labels.get(entity.identity, ""),
                        entity.label,
                        str(summary.total_eligible_entries),
                        str(summary.scored_entries),
                        str(summary.unscored_entries),
                        _format_summary_value(summary.score),
                        _format_number(summary.score.median),
                        _format_summary_value(summary.tokens_per_second, kind="speed"),
                        batches,
                        _format_date_range(summary.imported_at_min, summary.imported_at_max),
                        "Available" if entity.record_count else "Unavailable",
                    ),
                    tooltip=(entity.identity, entity.label),
                )
            )
        self._add_table_tab("Summary", headers, tuple(rows))

    @staticmethod
    def _row(values: tuple[str, ...], tooltip: tuple[str, ...] = ()) -> ComparisonTableRow:
        return ComparisonTableRow(values, tooltip)

    def _render_review(self, result: ModelComparisonResult | SessionComparisonResult) -> None:
        headers = ("Metric", "Subject", "Mean", "Median", "Available", "Missing", "Distribution")
        metric_fields = (
            ("Accuracy", "accuracy_score"),
            ("Depth", "depth_score"),
            ("Signal-to-noise", "signal_noise_score"),
            ("Actionability", "actionability_score"),
            ("Seniority", "seniority_score"),
            ("Overall", "overall_score"),
        )
        rows = []
        for entity in result.entities:
            for label, field_name in metric_fields:
                summary = getattr(entity.review, field_name)
                rows.append(
                    self._row(
                        (
                            label,
                            entity.label,
                            _format_score(summary.mean),
                            _format_score(summary.median),
                            str(summary.available_count),
                            str(summary.missing_count),
                            "",
                        ),
                        tooltip=(entity.identity, field_name),
                    )
                )
            for label, field_name in (
                ("Hallucination", "hallucination"),
                ("Reliability", "reliability"),
            ):
                distribution = getattr(entity.review, field_name)
                rows.append(
                    self._row(
                        (
                            label,
                            entity.label,
                            "",
                            "",
                            str(distribution.observed_count),
                            str(distribution.missing_count),
                            _format_distribution(distribution),
                        ),
                        tooltip=(entity.identity, field_name),
                    )
                )
        self._add_table_tab(
            "Review",
            headers,
            tuple(rows),
            empty_message="No BenchmarkRun review values are recorded for the selected subjects.",
        )

    def _render_categories(self, result: Any) -> None:
        identities = tuple(entity.identity for entity in result.entities)
        labels = {entity.identity: entity.label for entity in result.entities}
        headers = ("Category",) + tuple(
            value
            for identity in identities
            for value in (
                f"{labels[identity]} count",
                f"{labels[identity]} %",
                f"{labels[identity]} missing",
            )
        )
        rows = []
        for comparison in result.categorical_comparisons:
            values = [comparison.category]
            for identity in identities:
                values.extend(
                    (
                        str(comparison.counts.get(identity, 0)),
                        _format_percent(comparison.percentages.get(identity)),
                        str(comparison.missing_counts.get(identity, 0)),
                    )
                )
            rows.append(self._row(tuple(values), tooltip=(comparison.category,)))
        self._add_table_tab("Categories", headers, tuple(rows), empty_message="No categorical values are available.")

    def _render_rankings(self, result: ModelComparisonResult) -> None:
        headers = (
            "Ranking",
            "Subject",
            "Rank",
            "Mean score",
            "Median score",
            "Scored",
            "Mean tokens / second",
        )
        rows = []
        for title, ranking in (("Overall", result.ranking), ("Speed", result.speed_ranking)):
            for entry in ranking:
                rows.append(
                    self._row(
                        (
                            title,
                            entry.label,
                            "Unranked" if entry.rank is None else str(entry.rank),
                            _format_score(entry.mean_overall_score),
                            _format_score(entry.median_overall_score),
                            str(entry.scored_count),
                            _format_speed(entry.mean_tokens_per_second),
                        ),
                        tooltip=(entry.entity, entry.label),
                    )
                )
        self._add_table_tab("Rankings", headers, tuple(rows), empty_message="No ranking values are available.")

    @staticmethod
    def _alignment_has_content(alignment: Any) -> bool:
        for name in (
            "shared_benchmark_count",
            "shared_model_count",
            "shared_model_benchmark_pair_count",
            "excluded_benchmark_count",
        ):
            if getattr(alignment, name, 0):
                return True
        return any(
            bool(getattr(alignment, name, ()))
            for name in (
                "shared_benchmarks",
                "shared_models",
                "shared_model_benchmark_pairs",
                "aligned_benchmarks",
                "aligned_models",
                "aligned_model_benchmarks",
                "non_overlapping_benchmarks",
                "non_overlapping_models",
                "non_overlapping_model_benchmark_pairs",
            )
        )

    def _render_model_alignment(self, result: ModelComparisonResult) -> None:
        headers = (
            "Alignment",
            "Subject",
            "Value",
            "Count",
            "Records",
            "Scored",
            "Mean score",
            "Mean tokens / second",
        )
        rows: list[ComparisonTableRow] = []
        labels = {entity.identity: entity.label for entity in result.entities}
        alignment = result.alignment
        if alignment.shared_benchmarks or alignment.shared_benchmark_count:
            rows.append(
                self._row(
                    (
                        "Shared benchmarks",
                        "All selected subjects",
                        ", ".join(alignment.shared_benchmarks),
                        str(alignment.shared_benchmark_count),
                        "",
                        "",
                        "",
                        "",
                    ),
                    tooltip=alignment.shared_benchmarks,
                )
            )
        for aligned in result.alignment.aligned_benchmarks:
            for identity, values in aligned.entities.items():
                rows.append(
                    self._row(
                        (
                            "Aligned benchmark",
                            labels.get(identity, identity),
                            aligned.label,
                            "",
                            str(values.record_count),
                            str(values.scored_count),
                            _format_summary_value(values.overall_score, kind="score"),
                            _format_summary_value(values.tokens_per_second, kind="speed"),
                        ),
                        tooltip=(aligned.key, identity),
                    )
                )
        for identity, benchmarks in alignment.non_overlapping_benchmarks.items():
            for benchmark in benchmarks:
                rows.append(
                    self._row(
                        (
                            "Non-overlapping benchmark",
                            labels.get(identity, identity),
                            benchmark,
                            "",
                            "",
                            "",
                            "",
                            "",
                        ),
                        tooltip=(identity, benchmark),
                    )
                )
        if alignment.excluded_benchmark_count:
            rows.append(
                self._row(
                    (
                        "Excluded benchmark count",
                        "All selected subjects",
                        "",
                        str(alignment.excluded_benchmark_count),
                        "",
                        "",
                        "",
                        "",
                    )
                )
            )
        self._add_table_tab(
            "Alignment",
            headers,
            tuple(rows),
            empty_message="No BenchmarkRun alignment details are available.",
        )

    def _render_session_alignment(self, result: SessionComparisonResult) -> None:
        headers = (
            "Alignment",
            "Subject",
            "Value",
            "Count",
            "Records",
            "Scored",
            "Mean score",
            "Mean tokens / second",
        )
        alignment = result.alignment
        labels = {entity.identity: entity.label for entity in result.entities}
        rows: list[ComparisonTableRow] = []
        if alignment.shared_models or alignment.shared_model_count:
            rows.append(
                self._row(
                    (
                        "Shared models",
                        "All selected sessions",
                        ", ".join(alignment.shared_models),
                        str(alignment.shared_model_count),
                        "",
                        "",
                        "",
                        "",
                    ),
                    tooltip=alignment.shared_models,
                )
            )
        if alignment.shared_benchmarks or alignment.shared_benchmark_count:
            rows.append(
                self._row(
                    (
                        "Shared benchmarks",
                        "All selected sessions",
                        ", ".join(alignment.shared_benchmarks),
                        str(alignment.shared_benchmark_count),
                        "",
                        "",
                        "",
                        "",
                    ),
                    tooltip=alignment.shared_benchmarks,
                )
            )
        if alignment.shared_model_benchmark_pairs or alignment.shared_model_benchmark_pair_count:
            rows.append(
                self._row(
                    (
                        "Shared model / benchmark pairs",
                        "All selected sessions",
                        "; ".join(
                            f"{model} / {benchmark}"
                            for model, benchmark in alignment.shared_model_benchmark_pairs
                        ),
                        str(alignment.shared_model_benchmark_pair_count),
                        "",
                        "",
                        "",
                        "",
                    ),
                    tooltip=tuple(
                        f"{model} / {benchmark}"
                        for model, benchmark in alignment.shared_model_benchmark_pairs
                    ),
                )
            )

        for title, aligned_items in (
            ("Aligned model", alignment.aligned_models),
            ("Aligned benchmark", alignment.aligned_benchmarks),
        ):
            for aligned in aligned_items:
                for identity, values in aligned.entities.items():
                    rows.append(
                        self._row(
                            (
                                title,
                                labels.get(identity, identity),
                                aligned.label,
                                "",
                                str(values.record_count),
                                str(values.scored_count),
                                _format_summary_value(values.overall_score, kind="score"),
                                _format_summary_value(values.tokens_per_second, kind="speed"),
                            ),
                            tooltip=(aligned.key, identity),
                        )
                    )
        for aligned in alignment.aligned_model_benchmarks:
            for identity, values in aligned.entities.items():
                rows.append(
                    self._row(
                        (
                            "Aligned model / benchmark pair",
                            labels.get(identity, identity),
                            f"{aligned.model_label} / {aligned.benchmark_label}",
                            "",
                            str(values.record_count),
                            str(values.scored_count),
                            _format_summary_value(values.overall_score, kind="score"),
                            _format_summary_value(values.tokens_per_second, kind="speed"),
                        ),
                        tooltip=(aligned.model_key, aligned.benchmark_key, identity),
                    )
                )

        for title, values_by_subject in (
            ("Non-overlapping model", alignment.non_overlapping_models),
            ("Non-overlapping benchmark", alignment.non_overlapping_benchmarks),
        ):
            for identity, values in values_by_subject.items():
                for value in values:
                    rows.append(
                        self._row(
                            (title, labels.get(identity, identity), value, "", "", "", "", ""),
                            tooltip=(identity, value),
                        )
                    )
        for identity, pairs in alignment.non_overlapping_model_benchmark_pairs.items():
            for model, benchmark in pairs:
                rows.append(
                    self._row(
                        (
                            "Non-overlapping model / benchmark pair",
                            labels.get(identity, identity),
                            f"{model} / {benchmark}",
                            "",
                            "",
                            "",
                            "",
                            "",
                        ),
                        tooltip=(identity, model, benchmark),
                    )
                )
        self._add_table_tab(
            "Alignment",
            headers,
            tuple(rows),
            empty_message="No session alignment details are available.",
        )

    @staticmethod
    def _metric_kind(name: str, *, score_scale: bool = True) -> str:
        if "percentage" in name:
            return "percent"
        if "score" in name:
            return "score" if score_scale else "number"
        if "tokens_per_second" in name:
            return "speed"
        return "count"

    @staticmethod
    def _format_metric(value: float | int | None, kind: str) -> str:
        if kind == "score":
            return _format_score(value)
        if kind == "speed":
            return _format_speed(value)
        if kind == "percent":
            return _format_percent(value)
        if kind == "number":
            return _format_number(value)
        return "Unavailable" if value is None else str(int(value))

    def _render_pairwise(self, result: Any) -> None:
        pairwise = result.pairwise
        if pairwise is None and len(self._selected) != 2:
            if len(self._selected) > 2:
                self._add_table_tab(
                    "Pairwise",
                    ("Pairwise comparison",),
                    (),
                    empty_message="Pairwise comparison is available only for two selected subjects.",
                )
            return
        if pairwise is None:
            self._add_table_tab(
                "Pairwise",
                ("Pairwise comparison",),
                (),
                empty_message="No pairwise values were provided for the selected subjects.",
            )
            return

        labels = {entity.identity: entity.label for entity in result.entities}
        direction_label = QLabel(
            f"Delta direction: {pairwise.delta_direction.replace('_', ' ')}"
        )
        direction_label.setObjectName("comparisonPairwiseDirection")
        direction_label.setAccessibleName("Pairwise delta direction")
        direction_label.setWordWrap(True)
        self.pairwise_direction_label = direction_label
        headers = (
            "Metric",
            "Baseline",
            "Compared",
            "Delta",
            "Percent delta",
            "Direction",
            "Status",
        )
        rows = []
        for metric in pairwise.metrics:
            kind = self._metric_kind(
                metric.metric_name,
                score_scale=not isinstance(result, ScoreboardModelComparisonResult),
            )
            baseline = metric.baseline_entity or pairwise.baseline_entity
            compared = metric.comparison_entity or pairwise.comparison_entity
            rows.append(
                self._row(
                    (
                        metric.metric_name,
                        self._format_metric(metric.values.get(baseline), kind),
                        self._format_metric(metric.values.get(compared), kind),
                        self._format_metric(metric.absolute_delta, kind),
                        _format_percent(metric.percentage_delta),
                        _format_direction(metric.direction),
                        metric.unavailable_reason or "Available",
                    ),
                    tooltip=(
                        labels.get(baseline, baseline),
                        labels.get(compared, compared),
                        metric.unavailable_reason or "",
                    ),
                )
            )
        self._add_table_tab("Pairwise", headers, tuple(rows), introduction=direction_label)

    def _activate_primary_result_tab(self) -> None:
        """Replace the empty page and activate the first meaningful result."""

        placeholder_index = self.results_tabs.indexOf(self.results_placeholder)
        if placeholder_index >= 0:
            self.results_tabs.removeTab(placeholder_index)

        if self.results_tabs.count() == 0:
            self.results_tabs.addTab(self.results_placeholder, "Results")
            self.results_tabs.setCurrentWidget(self.results_placeholder)
            return

        summary_index = next(
            (
                index
                for index in range(self.results_tabs.count())
                if self.results_tabs.tabText(index) == "Summary"
            ),
            -1,
        )
        active_index = summary_index if summary_index >= 0 else 0
        self.results_tabs.setCurrentIndex(active_index)

    def _update_selection_actions(self) -> None:
        selected_row = self.selected_list.currentRow()
        available_selected = False
        if self.available_table.selectionModel():
            indexes = self.available_table.selectionModel().selectedRows()
            if indexes:
                source_index = self.available_proxy.mapToSource(indexes[0])
                row = self.available_model.row_at(source_index.row())
                available_selected = row is not None and row.selectable
        self.add_subject_button.setEnabled(available_selected)
        self.remove_subject_button.setEnabled(0 <= selected_row < len(self._selected))
        self.move_up_button.setEnabled(0 < selected_row < len(self._selected))
        self.move_down_button.setEnabled(0 <= selected_row < len(self._selected) - 1)
        self.clear_selection_button.setEnabled(bool(self._selected))
        can_compare = len(self._selected) >= 2 and not self._busy
        if (
            self._source is ComparisonSource.BENCHMARK_RUNS
            and self._dimension is ComparisonDimension.BENCHMARKS
        ):
            can_compare = can_compare and all(
                selection.selectable and selection.key in self._available
                for selection in self._selected
            )
        self.compare_button.setEnabled(can_compare)

    def _render_state(self, state: ComparisonResultState) -> None:
        presentation = STATE_COPY.get(state)
        if presentation is None:
            title = "Comparison status"
            explanation = "The comparison returned an unrecognized status. Refresh subjects and try again."
            state_value = getattr(state, "value", state)
            self.state_banner.setToolTip(f"Unrecognized comparison state: {state_value}")
        else:
            title, explanation = presentation
            self.state_banner.setToolTip("")
        self.state_banner.setText(f"{title}: {explanation}")

    def _render_warnings(self, warnings: tuple[Any, ...]) -> None:
        self.warning_list.clear()
        seen: set[tuple[Any, ...]] = set()
        for warning in warnings:
            key = (
                warning.code,
                warning.source_family,
                warning.subject_identity,
                warning.metric,
                warning.category,
                warning.message,
            )
            if key in seen:
                continue
            seen.add(key)
            source = str(warning.source_family.value).replace("_", " ").title()
            context = " — ".join(
                value
                for value in (source, warning.subject_identity, warning.metric, warning.category)
                if value
            )
            item = QListWidgetItem(f"{context}: {warning.message or warning.code.value}")
            item.setToolTip(warning.message or warning.code.value)
            self.warning_list.addItem(item)
        self.warning_list.setVisible(self.warning_list.count() > 0)

    def _reset_results(self, *, placeholder_message: str | None = None) -> None:
        self.pairwise_direction_label = None
        while self.results_tabs.count():
            page = self.results_tabs.widget(0)
            self.results_tabs.removeTab(0)
            if page is not None and page is not self.results_placeholder:
                page.deleteLater()
        self.result_tables.clear()
        self.results_placeholder.setText(
            placeholder_message or "Select at least two subjects, then choose Compare."
        )
        self.results_tabs.addTab(self.results_placeholder, "Results")
        self.results_tabs.setCurrentWidget(self.results_placeholder)


__all__ = ("ComparisonsView", "ComparisonDimension", "ComparisonSource")
