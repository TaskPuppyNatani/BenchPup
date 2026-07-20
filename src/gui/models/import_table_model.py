"""Read-only table model used by the CSV import preview pages."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QObject, Qt


class ImportTableModel(QAbstractTableModel):
    """Small immutable presentation model for source or mapped CSV rows."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._headers: tuple[str, ...] = ()
        self._rows: tuple[tuple[str, ...], ...] = ()

    def set_data(self, headers: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
        self.beginResetModel()
        self._headers = tuple(str(header) for header in headers)
        self._rows = tuple(
            tuple("" if value is None else str(value) for value in row)
            for row in rows
        )
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._headers)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        if not 0 <= index.column() < len(row):
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return row[index.column()]
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
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self._headers):
            return self._headers[section]
        if orientation == Qt.Orientation.Vertical:
            return section + 1
        return None


__all__ = ("ImportTableModel",)
