"""Read-only table primitives shared by the GUI catalog pages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QObject, Qt


T = TypeVar("T")
CATALOG_ROW_ROLE = Qt.ItemDataRole.UserRole + 10
CATALOG_RECORD_ROLE = Qt.ItemDataRole.UserRole + 11


@dataclass(frozen=True)
class CatalogTableRow(Generic[T]):
    """One immutable presentation row and its source domain record."""

    record: T
    values: tuple[str, ...]
    tooltips: tuple[str, ...]
    sort_values: tuple[Any, ...]
    search_text: str


class CatalogTableModel(QAbstractTableModel, Generic[T]):
    """A sortable, selectable, deliberately non-editable catalog table model."""

    def __init__(self, headers: tuple[str, ...], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.headers = headers
        self._rows: tuple[CatalogTableRow[T], ...] = ()
        self._sort_column: int | None = None
        self._sort_order = Qt.SortOrder.AscendingOrder

    def set_rows(self, rows: tuple[CatalogTableRow[T], ...]) -> None:
        self.beginResetModel()
        self._rows = tuple(rows)
        self._sort_rows()
        self.endResetModel()

    def rows(self) -> tuple[CatalogTableRow[T], ...]:
        return self._rows

    def record_at(self, row: int) -> T | None:
        if not 0 <= row < len(self._rows):
            return None
        return self._rows[row].record

    def row_at(self, row: int) -> CatalogTableRow[T] | None:
        if not 0 <= row < len(self._rows):
            return None
        return self._rows[row]

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
            return row.tooltips[index.column()]
        if role == CATALOG_ROW_ROLE:
            return row
        if role == CATALOG_RECORD_ROLE:
            return row.record
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

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        if not 0 <= column < len(self.headers):
            return
        self.layoutAboutToBeChanged.emit()
        self._sort_column = column
        self._sort_order = order
        self._sort_rows()
        self.layoutChanged.emit()

    def _sort_rows(self) -> None:
        if self._sort_column is None:
            return
        column = self._sort_column
        try:
            ordered = sorted(self._rows, key=lambda row: row.sort_values[column])
        except (IndexError, TypeError):
            ordered = sorted(self._rows, key=lambda row: str(row.values[column]).casefold())
        if self._sort_order == Qt.SortOrder.DescendingOrder:
            ordered.reverse()
        self._rows = tuple(ordered)


__all__ = ("CATALOG_RECORD_ROLE", "CATALOG_ROW_ROLE", "CatalogTableModel", "CatalogTableRow")
