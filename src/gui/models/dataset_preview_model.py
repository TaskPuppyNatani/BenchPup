"""Read-only presentation model for DatasetBuilder preview records."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QObject, Qt


DATASET_PREVIEW_HEADERS = (
    "Run ID",
    "Model",
    "Benchmark",
    "Overall Score",
    "Verdict",
    "Output Length",
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _benchmark_label(value: object) -> str:
    benchmark = _mapping(value)
    for key in ("name", "benchmark_file", "file_path"):
        label = str(benchmark.get(key, "")).strip()
        if label:
            return label
    return "Unknown benchmark"


def _score_label(value: object) -> str:
    if value is None or isinstance(value, bool):
        return "Not recorded"
    if not isinstance(value, (int, float)):
        return str(value)
    try:
        return f"{float(value):.2f}"
    except (ValueError, OverflowError):
        return str(value)


class DatasetPreviewTableModel(QAbstractTableModel):
    """Immutable table adapter over the records returned by ``DatasetBuilder.preview``."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: tuple[Mapping[str, Any], ...] = ()

    def set_records(self, records: Iterable[Mapping[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = tuple(dict(record) for record in records)
        self.endResetModel()

    def clear(self) -> None:
        self.set_records(())

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(DATASET_PREVIEW_HEADERS)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        input_data = _mapping(row.get("input"))
        model = _mapping(input_data.get("model"))
        response = _mapping(row.get("response"))
        metadata = _mapping(row.get("metadata"))
        run_id = metadata.get("source_run_id")
        raw_output = input_data.get("raw_model_output")
        output_length = len(str(raw_output)) if raw_output is not None else 0
        values = (
            str(run_id) if run_id is not None else "Not recorded",
            str(model.get("model_name", "")).strip() or "Unknown model",
            _benchmark_label(input_data.get("benchmark")),
            _score_label(response.get("overall")),
            str(response.get("verdict", "")).strip() or "Not recorded",
            str(output_length),
        )
        if not 0 <= index.column() < len(values):
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return values[index.column()]
        if role == Qt.ItemDataRole.UserRole:
            return row
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (0, 3, 5):
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
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(DATASET_PREVIEW_HEADERS):
            return DATASET_PREVIEW_HEADERS[section]
        if orientation == Qt.Orientation.Vertical:
            return section + 1
        return None


__all__ = ("DATASET_PREVIEW_HEADERS", "DatasetPreviewTableModel")
