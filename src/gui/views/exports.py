"""Reports & Exports navigation page."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGroupBox, QLabel, QPushButton, QVBoxLayout, QWidget

from ..context import GuiApplicationContext


class ExportsView(QWidget):
    """Lightweight launcher for the modal standard-export workflow."""

    export_requested = Signal()

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
        standard_layout.addWidget(
            QLabel(
                "Preview the selected scope first. Existing files require an explicit overwrite confirmation, "
                "and missing folders are never created automatically."
            )
        )
        self.export_button = QPushButton("Open Standard Export")
        self.export_button.setObjectName("primaryButton")
        self.export_button.setAccessibleName("Open standard export workflow")
        self.export_button.clicked.connect(self.open_export)
        standard_layout.addWidget(self.export_button)

        unavailable_group = QGroupBox("Unavailable in this phase")
        unavailable_layout = QVBoxLayout(unavailable_group)
        unavailable_layout.addWidget(
            QLabel(
                "JSONL training-data export is not available here. Use the future Dataset Builder GUI for "
                "Dataset Builder JSONL workflows. Comparison and Trend exports are also out of scope."
            )
        )

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("exportPageStatus")
        self.status_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 30)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(standard_group)
        layout.addWidget(unavailable_group)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

    def open_export(self) -> None:
        self.export_requested.emit()

    def show_success(self, path: str) -> None:
        self.status_label.setText(f"Export completed successfully: {path}")


__all__ = ("ExportsView",)
