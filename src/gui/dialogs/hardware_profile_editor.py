"""Explicit Add/Edit dialog for HardwareProfile records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ..context import GuiApplicationContext

try:
    from ...engine.domain import HardwareProfile
except ImportError:  # pragma: no cover - exercised by top-level test imports.
    from engine.domain import HardwareProfile  # type: ignore[no-redef]

from ..models.catalog import NOT_RECORDED, display_timestamp
from .base import CatalogEditorDialog
from .model_editor import OptionalNumericField


class BackendVersionsEditor(QWidget):
    """Small structured editor that keeps backend versions as a string mapping."""

    changed = Signal()

    def __init__(self, values: dict[str, str] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("backendVersionsEditor")
        self.setAccessibleName("Backend versions")

        self.table = QTableWidget(0, 2)
        self.table.setObjectName("backendVersionsTable")
        self.table.setAccessibleName("Backend version entries")
        self.table.setHorizontalHeaderLabels(("Backend", "Version"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(120)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        self.add_button = QPushButton("Add backend version")
        self.add_button.setAccessibleName("Add backend version")
        self.add_button.clicked.connect(self.add_row)
        self.remove_button = QPushButton("Remove selected")
        self.remove_button.setAccessibleName("Remove selected backend version")
        self.remove_button.clicked.connect(self.remove_selected)

        actions = QWidget()
        actions_layout = QVBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(6)
        actions_layout.addWidget(self.add_button)
        actions_layout.addWidget(self.remove_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.table)
        layout.addWidget(actions)

        for key, value in sorted((values or {}).items(), key=lambda item: (item[0].casefold(), item[0])):
            self.add_row(key, value)

    def add_row(self, key: str = "", value: str = "") -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        key_edit = QLineEdit(key)
        key_edit.setAccessibleName(f"Backend name row {row + 1}")
        value_edit = QLineEdit(value)
        value_edit.setAccessibleName(f"Backend version row {row + 1}")
        self.table.setCellWidget(row, 0, key_edit)
        self.table.setCellWidget(row, 1, value_edit)
        key_edit.textChanged.connect(self.changed.emit)
        value_edit.textChanged.connect(self.changed.emit)
        self.table.selectRow(row)

    def remove_selected(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self.changed.emit()

    def mapping(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for row in range(self.table.rowCount()):
            key_widget = self.table.cellWidget(row, 0)
            value_widget = self.table.cellWidget(row, 1)
            if not isinstance(key_widget, QLineEdit) or not isinstance(value_widget, QLineEdit):
                continue
            key = key_widget.text()
            if not key:
                raise ValueError("backend version name is required")
            if key in values:
                raise ValueError("backend version names must be unique")
            values[key] = value_widget.text()
        return values


class HardwareProfileEditorDialog(CatalogEditorDialog):
    """Edit reusable hardware data while preserving imported provenance metadata."""

    def __init__(
        self,
        context: GuiApplicationContext,
        profile: HardwareProfile | None = None,
        parent: QWidget | None = None,
        *,
        confirm_close: Callable[[], bool] | None = None,
    ) -> None:
        self.profile = profile
        super().__init__(
            context,
            title="Edit Hardware Profile" if profile is not None else "Add Hardware Profile",
            parent=parent,
            confirm_close=confirm_close,
        )
        self.setAccessibleName("Edit Hardware Profile" if profile is not None else "Add Hardware Profile")

        self.name_edit = self._line(profile.name if profile else "", "Hardware profile name", "Required")
        self.computer_name_edit = self._line(profile.computer_name if profile else "", "Computer name")
        self.cpu_edit = self._line(profile.cpu if profile else "", "CPU")
        self.gpu_edit = self._line(profile.gpu if profile else "", "GPU")
        self.operating_system_edit = self._line(profile.operating_system if profile else "", "Operating system")
        self.vram_field = OptionalNumericField(
            "VRAM (GB)", integer=False, minimum=0.0, maximum=1_000_000_000.0,
            decimals=6, value=profile.vram_gb if profile else None,
        )
        self.ram_field = OptionalNumericField(
            "RAM (GB)", integer=False, minimum=0.0, maximum=1_000_000_000.0,
            decimals=6, value=profile.ram_gb if profile else None,
        )
        self.backend_versions_edit = BackendVersionsEditor(profile.backend_versions if profile else {})
        self.notes_edit = QPlainTextEdit(profile.notes if profile else "")
        self.notes_edit.setAccessibleName("Hardware profile notes")
        self.notes_edit.setMinimumHeight(96)

        import_source = profile.import_source if profile is not None else ""
        self.import_source_value = QLabel(import_source or "Manual profile")
        self.import_source_value.setAccessibleName("Hardware profile import source")
        self.import_source_value.setTextFormat(Qt.TextFormat.PlainText)
        self.import_source_value.setWordWrap(True)
        imported_at = display_timestamp(profile.imported_at) if profile and profile.imported_at else NOT_RECORDED
        self.imported_at_value = QLabel(imported_at)
        self.imported_at_value.setAccessibleName("Hardware profile imported timestamp")
        self.imported_at_value.setTextFormat(Qt.TextFormat.PlainText)

        fields = (
            ("Name *", self.name_edit, "name"),
            ("Computer name", self.computer_name_edit, "computer_name"),
            ("CPU", self.cpu_edit, "cpu"),
            ("GPU", self.gpu_edit, "gpu"),
            ("VRAM (GB)", self.vram_field, "vram_gb"),
            ("RAM (GB)", self.ram_field, "ram_gb"),
            ("Operating system", self.operating_system_edit, "operating_system"),
            ("Backend versions", self.backend_versions_edit, "backend_versions"),
            ("Notes", self.notes_edit, "notes"),
        )
        for label, widget, field_name in fields:
            self.form.addRow(label, widget)
            self.register_field(field_name, widget)
        self.form.addRow("Import source", self.import_source_value)
        self.form.addRow("Imported at", self.imported_at_value)

        for field in (self.name_edit, self.computer_name_edit, self.cpu_edit, self.gpu_edit, self.operating_system_edit):
            field.textChanged.connect(self.mark_dirty)
        for field in (self.vram_field, self.ram_field):
            field.record_checkbox.toggled.connect(self.mark_dirty)
            field.spin_box.valueChanged.connect(self.mark_dirty)
        self.notes_edit.textChanged.connect(self.mark_dirty)
        self.backend_versions_edit.changed.connect(self.mark_dirty)

    @staticmethod
    def _line(value: str, accessible_name: str, placeholder: str = "") -> QLineEdit:
        field = QLineEdit(value)
        field.setAccessibleName(accessible_name)
        if placeholder:
            field.setPlaceholderText(placeholder)
        return field

    def build_draft(self) -> HardwareProfile:
        values: dict[str, Any] = dict(
            name=self.name_edit.text(),
            computer_name=self.computer_name_edit.text(),
            cpu=self.cpu_edit.text(),
            gpu=self.gpu_edit.text(),
            vram_gb=self.vram_field.value(),
            ram_gb=self.ram_field.value(),
            operating_system=self.operating_system_edit.text(),
            backend_versions=self.backend_versions_edit.mapping(),
            notes=self.notes_edit.toPlainText(),
        )
        if self.profile is not None:
            return replace(self.profile, **values)
        return HardwareProfile(**values)

    def save_draft(self, draft: HardwareProfile) -> HardwareProfile:
        if draft.id is None:
            return self.context.catalog.create_hardware_profile(draft)
        return self.context.catalog.update_hardware_profile(draft)


__all__ = ("BackendVersionsEditor", "HardwareProfileEditorDialog")
