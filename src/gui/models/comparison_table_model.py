"""Read-only presentation models for the Comparisons page."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QObject, Qt


SUBJECT_ROW_ROLE = Qt.ItemDataRole.UserRole + 20
TABLE_ROW_ROLE = Qt.ItemDataRole.UserRole + 21


@dataclass(frozen=True)
class ComparisonSubjectRow:
    """One engine-discovered subject prepared for selection display."""

    identity: str
    label: str
    records: int | None
    availability: str
    tooltip: str

    @property
    def values(self) -> tuple[str, ...]:
        return (
            self.label,
            self.identity,
            "Not calculated" if self.records is None else str(self.records),
            self.availability,
        )


@dataclass(frozen=True)
class ComparisonTableRow:
    """One immutable result-table row."""

    values: tuple[str, ...]
    tooltips: tuple[str, ...] = ()


class ComparisonSubjectTableModel(QAbstractTableModel):
    """Non-editable table model for available comparison subjects."""

    headers = ("Subject", "Identity", "Records", "Availability")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: tuple[ComparisonSubjectRow, ...] = ()

    def set_rows(self, rows: tuple[ComparisonSubjectRow, ...]) -> None:
        self.beginResetModel()
        self._rows = tuple(rows)
        self.endResetModel()

    def rows(self) -> tuple[ComparisonSubjectRow, ...]:
        return self._rows

    def row_at(self, row: int) -> ComparisonSubjectRow | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        values = row.values
        if not 0 <= index.column() < len(values):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return row.tooltip
        if role == SUBJECT_ROW_ROLE:
            return row
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() == 2:
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
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.headers):
            return self.headers[section]
        if orientation == Qt.Orientation.Vertical:
            return section + 1
        return None


class ComparisonTableModel(QAbstractTableModel):
    """Small immutable table model used by each result section."""

    def __init__(self, headers: tuple[str, ...], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.headers = headers
        self._rows: tuple[ComparisonTableRow, ...] = ()

    def set_rows(self, rows: tuple[ComparisonTableRow, ...]) -> None:
        self.beginResetModel()
        self._rows = tuple(rows)
        self.endResetModel()

    def rows(self) -> tuple[ComparisonTableRow, ...]:
        return self._rows

    def row_at(self, row: int) -> ComparisonTableRow | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if not 0 <= index.column() < len(row.values):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return row.values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            if index.column() < len(row.tooltips) and row.tooltips[index.column()]:
                return row.tooltips[index.column()]
            return row.values[index.column()]
        if role == TABLE_ROW_ROLE:
            return row
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
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.headers):
            return self.headers[section]
        if orientation == Qt.Orientation.Vertical:
            return section + 1
        return None


__all__ = (
    "SUBJECT_ROW_ROLE",
    "TABLE_ROW_ROLE",
    "ComparisonSubjectRow",
    "ComparisonSubjectTableModel",
    "ComparisonTableModel",
    "ComparisonTableRow",
)
