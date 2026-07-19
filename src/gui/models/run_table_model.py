"""Read-only Qt table model for the Runs browser."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QObject, Qt

from .runs import NOT_RECORDED, RunBrowserRow, UNAVAILABLE


TABLE_HEADERS = (
    "Run",
    "Recorded",
    "Model",
    "Benchmark",
    "Session",
    "Overall score",
    "Accuracy",
    "Hallucination",
    "Reliability",
    "Tokens / second",
)
ROW_ROLE = Qt.ItemDataRole.UserRole + 1
RUN_ID_ROLE = Qt.ItemDataRole.UserRole + 2


def format_recorded_at(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return value or UNAVAILABLE


def format_score(value: float | None) -> str:
    return NOT_RECORDED if value is None else f"{value:.2f} / 5"


def format_speed(value: Any) -> str:
    if value is None or value == "" or isinstance(value, bool):
        return UNAVAILABLE
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return UNAVAILABLE
        return f"{float(value):.1f} tok/s"
    return str(value)


class RunTableModel(QAbstractTableModel):
    """Immutable-source model with no editing or persistence actions."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: tuple[RunBrowserRow, ...] = ()

    def set_rows(self, rows: tuple[RunBrowserRow, ...]) -> None:
        self.beginResetModel()
        self._rows = tuple(rows)
        self.endResetModel()

    def rows(self) -> tuple[RunBrowserRow, ...]:
        return self._rows

    def row_at(self, row: int) -> RunBrowserRow | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

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
            f"#{row.run_id}" if row.run_id is not None else UNAVAILABLE,
            format_recorded_at(row.recorded_at),
            row.model,
            row.benchmark,
            row.session,
            format_score(row.overall_score),
            format_score(row.accuracy_score),
            row.hallucination_level or NOT_RECORDED,
            row.reliability_level or NOT_RECORDED,
            format_speed(row.tokens_per_second),
        )
        if not 0 <= index.column() < len(values):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            raw_values = (
                str(row.run_id) if row.run_id is not None else UNAVAILABLE,
                row.recorded_at,
                row.model,
                row.benchmark,
                row.session,
                format_score(row.overall_score),
                format_score(row.accuracy_score),
                row.hallucination_level or NOT_RECORDED,
                row.reliability_level or NOT_RECORDED,
                format_speed(row.tokens_per_second),
            )
            return raw_values[index.column()]
        if role == ROW_ROLE:
            return row
        if role == RUN_ID_ROLE:
            return row.run_id
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (0, 5, 6, 9):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(TABLE_HEADERS):
            return TABLE_HEADERS[section]
        if orientation == Qt.Orientation.Vertical:
            return section + 1
        return None


__all__ = (
    "ROW_ROLE",
    "RUN_ID_ROLE",
    "TABLE_HEADERS",
    "RunTableModel",
    "format_recorded_at",
    "format_score",
    "format_speed",
)
