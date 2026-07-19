"""Explicit Add/Edit dialog for BenchmarkDefinition records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from PySide6.QtWidgets import QFileDialog, QCheckBox, QComboBox, QHBoxLayout, QLineEdit, QPlainTextEdit, QPushButton, QWidget

from ..context import GuiApplicationContext

try:
    from ...engine.domain import BENCHMARK_TYPES, BenchmarkDefinition
except ImportError:  # pragma: no cover - exercised by top-level test imports.
    from engine.domain import BENCHMARK_TYPES, BenchmarkDefinition  # type: ignore[no-redef]

from .base import CatalogEditorDialog


BENCHMARK_TYPE_LABELS = {
    value: value.replace("_", " ").capitalize()
    for value in BENCHMARK_TYPES
}


class BenchmarkEditorDialog(CatalogEditorDialog):
    def __init__(
        self,
        context: GuiApplicationContext,
        definition: BenchmarkDefinition | None = None,
        parent: QWidget | None = None,
        *,
        confirm_close: Callable[[], bool] | None = None,
    ) -> None:
        self.definition = definition
        super().__init__(
            context,
            title="Edit Benchmark Definition" if definition is not None else "Add Benchmark Definition",
            parent=parent,
            confirm_close=confirm_close,
        )
        self.setAccessibleName("Edit Benchmark Definition" if definition is not None else "Add Benchmark Definition")

        self.name_edit = QLineEdit(definition.name if definition else "")
        self.name_edit.setAccessibleName("Benchmark name")
        self.name_edit.setPlaceholderText("Required")
        self.register_field("name", self.name_edit)

        self.file_path_edit = QLineEdit(definition.file_path if definition else "")
        self.file_path_edit.setAccessibleName("Benchmark file path")
        self.file_path_edit.setPlaceholderText("Required; stored exactly as entered")
        self.register_field("file_path", self.file_path_edit)
        browse = QPushButton("Browse")
        browse.setAccessibleName("Browse for benchmark file")
        browse.setToolTip("Optional convenience; manual path entry remains available.")
        browse.clicked.connect(self._browse)
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(8)
        path_layout.addWidget(self.file_path_edit, 1)
        path_layout.addWidget(browse)

        self.type_combo = QComboBox()
        self.type_combo.setAccessibleName("Benchmark type")
        for value in BENCHMARK_TYPES:
            self.type_combo.addItem(BENCHMARK_TYPE_LABELS.get(value, value), value)
        if definition is not None:
            index = self.type_combo.findData(definition.benchmark_type)
            if index >= 0:
                self.type_combo.setCurrentIndex(index)
        self.register_field("benchmark_type", self.type_combo)

        self.default_prompt_edit = QPlainTextEdit(definition.default_prompt if definition else "")
        self.default_prompt_edit.setAccessibleName("Benchmark default prompt")
        self.default_prompt_edit.setMinimumHeight(130)
        self.default_prompt_edit.setToolTip("This is a benchmark default prompt, not a PromptTemplate.")

        self.tags_edit = QLineEdit(definition.tags if definition else "")
        self.tags_edit.setAccessibleName("Benchmark tags")
        self.tags_edit.setPlaceholderText("Optional; stored using the current text format")

        self.active_checkbox = QCheckBox("Active and available to Add Run")
        self.active_checkbox.setAccessibleName("Benchmark definition active")
        self.active_checkbox.setChecked(definition.is_active if definition else True)

        self.form.addRow("Name *", self.name_edit)
        self.form.addRow("File or target *", path_row)
        self.form.addRow("Benchmark type *", self.type_combo)
        self.form.addRow("Default prompt", self.default_prompt_edit)
        self.form.addRow("Tags", self.tags_edit)
        self.form.addRow("Lifecycle", self.active_checkbox)

        for field in (self.name_edit, self.file_path_edit, self.tags_edit):
            field.textChanged.connect(self.mark_dirty)
        self.type_combo.currentIndexChanged.connect(self.mark_dirty)
        self.default_prompt_edit.textChanged.connect(self.mark_dirty)
        self.active_checkbox.toggled.connect(self.mark_dirty)

    def _browse(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "Select benchmark file", self.file_path_edit.text())
        if selected:
            self.file_path_edit.setText(selected)

    def build_draft(self) -> BenchmarkDefinition:
        values: dict[str, Any] = dict(
            name=self.name_edit.text(),
            file_path=self.file_path_edit.text(),
            benchmark_type=str(self.type_combo.currentData() or ""),
            default_prompt=self.default_prompt_edit.toPlainText(),
            tags=self.tags_edit.text(),
            is_active=self.active_checkbox.isChecked(),
        )
        if self.definition is not None:
            return replace(self.definition, **values)
        return BenchmarkDefinition(**values)

    def save_draft(self, draft: BenchmarkDefinition) -> BenchmarkDefinition:
        if draft.id is None:
            return self.context.catalog.create_benchmark_definition(draft)
        return self.context.catalog.update_benchmark_definition(draft)


__all__ = ("BENCHMARK_TYPE_LABELS", "BenchmarkEditorDialog")
