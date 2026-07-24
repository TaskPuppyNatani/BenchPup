"""Reports & Exports navigation page."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGroupBox, QLabel, QPushButton, QVBoxLayout, QWidget

from ..context import GuiApplicationContext


class ExportsView(QWidget):
    """Lightweight launcher for the modal standard-export workflow."""

    export_requested = Signal()
    dataset_builder_requested = Signal()
    backup_restore_requested = Signal()

    def __init__(self, context: GuiApplicationContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.setObjectName("exportsPage")
        self.setAccessibleName("Reports and Exports page")

        title = QLabel("Reports & Exports")
        title.setObjectName("pageTitle")
        description = QLabel(
            "Create standard CSV, Markdown, Scoreboard HTML, and HTML Analytics files from current engine records."
        )
        description.setObjectName("pageDescription")
        description.setWordWrap(True)

        standard_group = QGroupBox("Standard exports")
        standard_layout = QVBoxLayout(standard_group)
        standard_description = QLabel(
            "Preview the selected scope first. Existing files require an explicit overwrite confirmation, "
            "and missing folders are never created automatically."
        )
        standard_description.setWordWrap(True)
        standard_layout.addWidget(standard_description)
        self.export_button = QPushButton("Open Standard Export")
        self.export_button.setObjectName("primaryButton")
        self.export_button.setAccessibleName("Open standard export workflow")
        self.export_button.clicked.connect(self.open_export)
        standard_layout.addWidget(self.export_button)

        dataset_group = QGroupBox("Dataset Builder")
        dataset_layout = QVBoxLayout(dataset_group)
        dataset_description = QLabel(
            "Configure filters and redaction rules, preview eligible runs, and build JSONL training data "
            "with its manifest in the dedicated Dataset Builder workflow."
        )
        dataset_description.setWordWrap(True)
        dataset_layout.addWidget(dataset_description)
        self.dataset_builder_button = QPushButton("Open Dataset Builder")
        self.dataset_builder_button.setObjectName("openDatasetBuilderButton")
        self.dataset_builder_button.setAccessibleName("Open Dataset Builder workflow")
        self.dataset_builder_button.clicked.connect(self.open_dataset_builder)
        dataset_layout.addWidget(self.dataset_builder_button)

        backup_restore_group = QGroupBox("Backup & Restore")
        backup_restore_layout = QVBoxLayout(backup_restore_group)
        backup_restore_description = QLabel(
            "Create an atomic archive or review, merge, and replace from a validated BenchPup archive."
        )
        backup_restore_description.setWordWrap(True)
        backup_restore_layout.addWidget(backup_restore_description)
        self.backup_restore_button = QPushButton("Open Backup & Restore")
        self.backup_restore_button.setObjectName("openBackupRestoreButton")
        self.backup_restore_button.setAccessibleName("Open Backup and Restore workflow")
        self.backup_restore_button.clicked.connect(self.open_backup_restore)
        backup_restore_layout.addWidget(self.backup_restore_button)

        out_of_scope_label = QLabel(
            "Comparison and Trend exports are out of scope for this phase."
        )
        out_of_scope_label.setWordWrap(True)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("exportPageStatus")
        self.status_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 30)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(standard_group)
        layout.addWidget(dataset_group)
        layout.addWidget(backup_restore_group)
        layout.addWidget(out_of_scope_label)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

    def open_export(self) -> None:
        self.export_requested.emit()

    def open_dataset_builder(self, _checked: bool = False) -> None:
        self.dataset_builder_requested.emit()

    def open_backup_restore(self, _checked: bool = False) -> None:
        self.backup_restore_requested.emit()

    def show_success(self, path: str) -> None:
        self.status_label.setText(f"Export completed successfully: {path}")

    def show_backup_success(self, path: str) -> None:
        self.status_label.setText(f"Backup completed successfully: {path}")


__all__ = ("ExportsView",)
