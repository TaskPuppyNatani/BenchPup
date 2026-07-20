"""CSV import entry page for the BenchPup desktop shell."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget, QDialog

from ..context import GuiApplicationContext
from ..dialogs.csv_import import CsvImportWizard
from ..dialogs.hardware_import import HardwareImportDialog


class ImportsView(QWidget):
    """Small launcher page that keeps detailed import workflows modal."""

    import_completed = Signal(str)
    hardware_import_completed = Signal(int)

    def __init__(self, context: GuiApplicationContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.setObjectName("importsPage")
        self.setAccessibleName("Imports page")

        title = QLabel("Imports")
        title.setObjectName("pageTitle")
        description = QLabel(
            "Import Benchmark Runs or historical Scoreboard entries through the existing engine transaction workflow."
        )
        description.setObjectName("pageDescription")
        description.setWordWrap(True)

        csv_group = QGroupBox("CSV imports")
        csv_layout = QVBoxLayout(csv_group)
        csv_layout.addWidget(QLabel("Review detection, mappings, source rows, validation, duplicates, and the final commit before anything is written."))
        self.import_csv_button = QPushButton("Import CSV")
        self.import_csv_button.setObjectName("primaryButton")
        self.import_csv_button.setAccessibleName("Import CSV")
        self.import_csv_button.clicked.connect(self.open_csv_import)
        csv_layout.addWidget(self.import_csv_button, 0)

        hardware_note = QGroupBox("Hardware profile imports")
        hardware_layout = QVBoxLayout(hardware_note)
        hardware_layout.addWidget(QLabel("Import MSInfo32, DXDiag, or lshw --short reports into reusable hardware profiles."))
        self.import_hardware_button = QPushButton("Import Hardware Profile")
        self.import_hardware_button.setObjectName("primaryButton")
        self.import_hardware_button.setAccessibleName("Import Hardware Profile")
        self.import_hardware_button.clicked.connect(self.open_hardware_import)
        hardware_layout.addWidget(self.import_hardware_button, 0)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("importStatus")
        self.status_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 30)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(csv_group)
        layout.addWidget(hardware_note)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

    def open_csv_import(self) -> None:
        dialog = CsvImportWizard(self.context, self)
        dialog.import_completed.connect(self.import_completed)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.saved_import_type is not None:
            self.status_label.setText("CSV import completed successfully.")
        dialog.deleteLater()

    def open_hardware_import(self) -> None:
        dialog = HardwareImportDialog(self.context, self)
        dialog.import_completed.connect(self.hardware_import_completed)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.saved_profile is not None:
            self.status_label.setText("Hardware profile imported successfully.")
        dialog.deleteLater()


__all__ = ("ImportsView",)
