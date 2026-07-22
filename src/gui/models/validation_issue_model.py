"""Read-only presentation model for structured dataset validation issues."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QObject, Qt

try:  # Support both ``python -m src.gui`` and test imports with ``src`` on PATH.
    from ...engine.datasets import ValidationIssue
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.datasets import ValidationIssue  # type: ignore[no-redef]


VALIDATION_ISSUE_HEADERS = (
    "Source",
    "Code",
    "Line",
    "Field",
    "Expected",
    "Actual",
    "Message",
)


def _display_value(value: object) -> str:
    """Render an optional engine value without exposing Python ``None``."""

    if value is None or value == "":
        return "Not recorded"
    return str(value)


class ValidationIssueTableModel(QAbstractTableModel):
    """Immutable table adapter over engine-owned ``ValidationIssue`` rows."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._issues: tuple[ValidationIssue, ...] = ()

    def set_issues(self, issues: Iterable[ValidationIssue]) -> None:
        self.beginResetModel()
        self._issues = tuple(issues)
        self.endResetModel()

    def clear(self) -> None:
        self.set_issues(())

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._issues)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(VALIDATION_ISSUE_HEADERS)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._issues):
            return None
        issue = self._issues[index.row()]
        values = (
            issue.source.value,
            issue.code.value,
            _display_value(issue.line_number),
            _display_value(issue.field),
            _display_value(issue.expected),
            _display_value(issue.actual),
            issue.message,
        )
        if not 0 <= index.column() < len(values):
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return values[index.column()]
        if role == Qt.ItemDataRole.UserRole:
            return issue
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
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(VALIDATION_ISSUE_HEADERS):
            return VALIDATION_ISSUE_HEADERS[section]
        if orientation == Qt.Orientation.Vertical:
            return section + 1
        return None


__all__ = ("VALIDATION_ISSUE_HEADERS", "ValidationIssueTableModel")
