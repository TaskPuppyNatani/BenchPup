"""Review-before-save CSV import workflow over the typed engine importer."""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTableView,
    QVBoxLayout,
    QWizard,
    QWizardPage,
    QWidget,
)

from ..context import GuiApplicationContext
from ..models.import_table_model import ImportTableModel

try:
    from ...engine.importers import (
        AMBIGUOUS_IMPORT,
        BENCHMARK_RUN_IMPORT,
        CSV_IMPORT_TYPES,
        SCOREBOARD_IMPORT,
        UNSUPPORTED_IMPORT,
        CsvImportDetection,
        CsvImportType,
        ImportPreview,
        ImportResult,
        ImportMappingError,
        ImportMappingValidationResult,
        ImportValidationResult,
    )
    from ...engine.settings import ImportPreferences
except ImportError:  # pragma: no cover - exercised by top-level test imports.
    from engine.importers import (  # type: ignore[no-redef]
        AMBIGUOUS_IMPORT,
        BENCHMARK_RUN_IMPORT,
        CSV_IMPORT_TYPES,
        SCOREBOARD_IMPORT,
        UNSUPPORTED_IMPORT,
        CsvImportDetection,
        CsvImportType,
        ImportPreview,
        ImportResult,
        ImportMappingError,
        ImportMappingValidationResult,
        ImportValidationResult,
    )
    from engine.settings import ImportPreferences  # type: ignore[no-redef]


IMPORT_TYPE_LABELS = {
    "auto": "Auto-detect",
    BENCHMARK_RUN_IMPORT: "Benchmark Runs",
    SCOREBOARD_IMPORT: "Scoreboard",
    AMBIGUOUS_IMPORT: "Ambiguous",
    UNSUPPORTED_IMPORT: "Unsupported",
}
DUPLICATE_POLICY_LABELS = {
    "skip": "Skip duplicates",
    "replace": "Replace duplicates",
    "keep": "Keep duplicates",
}
_MAPPING_COMBO_HEIGHT_FLOOR = 32
_MAPPING_CELL_VERTICAL_MARGIN = 2
# The shared theme gives QTableView items 5 px of vertical padding. Cell widgets
# are placed inside that padded content rect, so the row must account for it.
_MAPPING_TABLE_ITEM_VERTICAL_PADDING = 5
_MAPPING_GRID_LINE_ALLOWANCE = 1
_MAPPING_ROW_HEIGHT_FLOOR = (
    _MAPPING_COMBO_HEIGHT_FLOOR
    + (2 * (_MAPPING_CELL_VERTICAL_MARGIN + _MAPPING_TABLE_ITEM_VERTICAL_PADDING))
    + _MAPPING_GRID_LINE_ALLOWANCE
)
_MAPPING_COMBO_MIN_WIDTH = 240
_MAPPING_POPUP_MIN_WIDTH = 280
_MAPPING_COMBO_CHROME_FALLBACK = 24


def _configure_mapping_combo(combo: QComboBox) -> tuple[int, int]:
    """Size one mapping combo for its styled cell and complete item labels."""

    combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    font_metrics = combo.fontMetrics()
    longest_label_width = max(
        (font_metrics.horizontalAdvance(combo.itemText(index)) for index in range(combo.count())),
        default=0,
    )
    current_label_width = font_metrics.horizontalAdvance(combo.currentText())
    styled_chrome_width = max(
        _MAPPING_COMBO_CHROME_FALLBACK,
        combo.sizeHint().width() - current_label_width,
    )
    minimum_width = max(
        _MAPPING_COMBO_MIN_WIDTH,
        longest_label_width + styled_chrome_width,
    )
    combo.setMinimumWidth(minimum_width)
    combo.view().setMinimumWidth(max(_MAPPING_POPUP_MIN_WIDTH, minimum_width))

    minimum_height = max(
        _MAPPING_COMBO_HEIGHT_FLOOR,
        combo.minimumSizeHint().height(),
        combo.sizeHint().height(),
        combo.fontMetrics().height() + 2,
    )
    combo.setFixedHeight(minimum_height)
    return minimum_height, minimum_width


class _SourcePage(QWizardPage):
    def __init__(self, wizard: CsvImportWizard) -> None:
        super().__init__(wizard)
        self._wizard = wizard

    def validatePage(self) -> bool:
        return self._wizard._validate_source_page()


class _MappingPage(QWizardPage):
    def __init__(self, wizard: CsvImportWizard) -> None:
        super().__init__(wizard)
        self._wizard = wizard

    def initializePage(self) -> None:
        self._wizard._prepare_mapping_page()
        super().initializePage()

    def validatePage(self) -> bool:
        return self._wizard._validate_mapping_page()


class _PreviewPage(QWizardPage):
    def __init__(self, wizard: CsvImportWizard) -> None:
        super().__init__(wizard)
        self._wizard = wizard

    def initializePage(self) -> None:
        self._wizard._prepare_preview_page()
        super().initializePage()

    def validatePage(self) -> bool:
        return self._wizard._validate_preview_page()


class _CommitPage(QWizardPage):
    def __init__(self, wizard: CsvImportWizard) -> None:
        super().__init__(wizard)
        self._wizard = wizard

    def initializePage(self) -> None:
        self._wizard._prepare_commit_page()
        super().initializePage()

    def validatePage(self) -> bool:
        return self._wizard._commit()


class CsvImportWizard(QWizard):
    """Four-step CSV import with no database writes before final confirmation."""

    import_completed = Signal(str)

    def __init__(
        self,
        context: GuiApplicationContext,
        parent: QWidget | None = None,
        *,
        confirm_close: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.confirm_close = confirm_close or (lambda: True)
        self.detection: CsvImportDetection | None = None
        self.active_preview: ImportPreview | None = None
        self.validation: ImportValidationResult | None = None
        self.mapping_validation: ImportMappingValidationResult | None = None
        self.import_result: ImportResult | None = None
        self.saved_import_type: CsvImportType | None = None
        self._mapping_by_type: dict[CsvImportType, dict[str, str | None]] = {}
        self._mapping_controls: dict[str, QComboBox] = {}
        self._saving = False
        self._committed = False
        self._source_loaded = ""

        self.setObjectName("csvImportWizard")
        self.setWindowTitle("Import CSV")
        self.setAccessibleName("CSV import wizard")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setMinimumSize(820, 650)
        self.resize(1120, 820)

        self.source_page = _SourcePage(self)
        self._build_source_page()
        self.mapping_page = _MappingPage(self)
        self._build_mapping_page()
        self.preview_page = _PreviewPage(self)
        self._build_preview_page()
        self.commit_page = _CommitPage(self)
        self._build_commit_page()
        self.addPage(self.source_page)
        self.addPage(self.mapping_page)
        self.addPage(self.preview_page)
        self.addPage(self.commit_page)

        preferences = context.settings.get_import_preferences()
        self._apply_preferences(preferences)

    def _build_source_page(self) -> None:
        self.source_page.setTitle("Select CSV")
        self.source_page.setSubTitle("Choose a CSV file. Detection and preview are read-only until you confirm the import.")
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(12)

        source_group = QGroupBox("CSV source")
        source_form = QFormLayout(source_group)
        source_row = QWidget()
        source_layout = QHBoxLayout(source_row)
        source_layout.setContentsMargins(0, 0, 0, 0)
        self.source_path_edit = QLineEdit()
        self.source_path_edit.setObjectName("csvSourcePath")
        self.source_path_edit.setAccessibleName("CSV source file")
        self.source_path_edit.setReadOnly(True)
        self.source_path_edit.setPlaceholderText("No CSV file selected")
        self.browse_source_button = QPushButton("Browse...")
        self.browse_source_button.setObjectName("browseCsvSource")
        self.browse_source_button.setAccessibleName("Choose CSV source file")
        self.browse_source_button.clicked.connect(self.browse_source)
        source_layout.addWidget(self.source_path_edit, 1)
        source_layout.addWidget(self.browse_source_button)
        source_form.addRow("Source file", source_row)

        self.type_combo = QComboBox()
        self.type_combo.setObjectName("csvImportType")
        self.type_combo.setAccessibleName("CSV import type")
        for value in ("auto", *CSV_IMPORT_TYPES):
            self.type_combo.addItem(IMPORT_TYPE_LABELS[value], value)
        self.type_combo.currentIndexChanged.connect(self._type_changed)
        source_form.addRow("Import as", self.type_combo)

        self.detected_type_value = QLabel("Not detected")
        self.detected_type_value.setObjectName("csvDetectedType")
        self.detected_type_value.setAccessibleName("Detected CSV type")
        self.detected_type_value.setTextFormat(Qt.TextFormat.PlainText)
        source_form.addRow("Detected type", self.detected_type_value)

        self.encoding_value = QLabel("Not detected")
        self.encoding_value.setObjectName("csvDetectedEncoding")
        self.encoding_value.setAccessibleName("Detected CSV encoding")
        self.encoding_value.setTextFormat(Qt.TextFormat.PlainText)
        source_form.addRow("Encoding", self.encoding_value)
        layout.addWidget(source_group)

        self.source_status = QLabel("Select a CSV file to begin.")
        self.source_status.setObjectName("csvSourceStatus")
        self.source_status.setWordWrap(True)
        self.source_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.source_status)
        layout.addStretch(1)
        self._set_page_body(self.source_page, body)

    def _build_mapping_page(self) -> None:
        self.mapping_page.setTitle("Review Mapping")
        self.mapping_page.setSubTitle("Confirm or edit how source headings map to engine import fields.")
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(10)

        self.mapping_status = QLabel()
        self.mapping_status.setObjectName("csvMappingStatus")
        self.mapping_status.setWordWrap(True)
        self.mapping_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.mapping_status)

        self.mapping_table = QTableWidget(0, 3)
        self.mapping_table.setObjectName("csvMappingTable")
        self.mapping_table.setAccessibleName("CSV heading mapping")
        self.mapping_table.setHorizontalHeaderLabels(("Source heading", "Mapped field", "Requirement"))
        self.mapping_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mapping_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.mapping_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        vertical_header = self.mapping_table.verticalHeader()
        vertical_header.setVisible(False)
        vertical_header.setMinimumSectionSize(_MAPPING_ROW_HEIGHT_FLOOR)
        vertical_header.setDefaultSectionSize(_MAPPING_ROW_HEIGHT_FLOOR)
        header = self.mapping_table.horizontalHeader()
        header.setMinimumSectionSize(140)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.mapping_table.setColumnWidth(1, _MAPPING_POPUP_MIN_WIDTH)
        layout.addWidget(self.mapping_table, 1)
        self._set_page_body(self.mapping_page, body)

    def _build_preview_page(self) -> None:
        self.preview_page.setTitle("Preview and Validate")
        self.preview_page.setSubTitle("Review source values, mapped values, warnings, validation errors, and duplicates before importing.")
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setSpacing(10)

        self.preview_status = QLabel()
        self.preview_status.setObjectName("csvPreviewStatus")
        self.preview_status.setWordWrap(True)
        self.preview_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.preview_status)

        self.source_preview_table = QTableView()
        self.source_preview_table.setObjectName("csvSourcePreviewTable")
        self.source_preview_table.setAccessibleName("CSV source preview")
        self.source_preview_table.setModel(ImportTableModel(self.source_preview_table))
        self.source_preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.source_preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.source_preview_table.setAlternatingRowColors(True)
        self.source_preview_table.setMinimumHeight(130)
        layout.addWidget(QLabel("Source values (first 20 rows)"))
        layout.addWidget(self.source_preview_table)

        self.mapped_preview_table = QTableView()
        self.mapped_preview_table.setObjectName("csvMappedPreviewTable")
        self.mapped_preview_table.setAccessibleName("CSV mapped preview")
        self.mapped_preview_table.setModel(ImportTableModel(self.mapped_preview_table))
        self.mapped_preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mapped_preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.mapped_preview_table.setAlternatingRowColors(True)
        self.mapped_preview_table.setMinimumHeight(130)
        layout.addWidget(QLabel("Mapped values (first 20 importable rows)"))
        layout.addWidget(self.mapped_preview_table)

        self.duplicate_policy_combo = QComboBox()
        self.duplicate_policy_combo.setObjectName("csvDuplicatePolicy")
        self.duplicate_policy_combo.setAccessibleName("CSV duplicate policy")
        for value, label in DUPLICATE_POLICY_LABELS.items():
            self.duplicate_policy_combo.addItem(label, value)
        self.duplicate_policy_combo.currentIndexChanged.connect(self._duplicate_policy_changed)

        self.batch_name_edit = QLineEdit()
        self.batch_name_edit.setObjectName("scoreboardBatchName")
        self.batch_name_edit.setAccessibleName("Scoreboard batch name")
        self.batch_name_edit.setPlaceholderText("Optional; defaults to the source filename and date")
        self.batch_notes_edit = QPlainTextEdit()
        self.batch_notes_edit.setObjectName("scoreboardBatchNotes")
        self.batch_notes_edit.setAccessibleName("Scoreboard batch notes")
        self.batch_notes_edit.setMaximumHeight(75)

        self.import_options_form = QFormLayout()
        self.import_options_form.addRow("Duplicate policy", self.duplicate_policy_combo)
        self.duplicate_policy_label = self.import_options_form.labelForField(self.duplicate_policy_combo)
        self.import_options_form.addRow("Batch name", self.batch_name_edit)
        self.batch_name_label = self.import_options_form.labelForField(self.batch_name_edit)
        self.import_options_form.addRow("Batch notes", self.batch_notes_edit)
        self.batch_notes_label = self.import_options_form.labelForField(self.batch_notes_edit)
        layout.addLayout(self.import_options_form)

        self.validation_summary = QLabel()
        self.validation_summary.setObjectName("csvValidationSummary")
        self.validation_summary.setWordWrap(True)
        self.validation_summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.validation_summary)
        self._set_page_body(self.preview_page, body)

    def _build_commit_page(self) -> None:
        self.commit_page.setTitle("Confirm Import")
        self.commit_page.setSubTitle("Finish commits the selected first-class record type through the engine transaction.")
        body = QWidget()
        layout = QVBoxLayout(body)
        self.confirmation_summary = QLabel()
        self.confirmation_summary.setObjectName("csvConfirmationSummary")
        self.confirmation_summary.setWordWrap(True)
        self.confirmation_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.confirmation_summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        layout.addWidget(self.confirmation_summary)
        self.commit_status = QLabel()
        self.commit_status.setObjectName("csvCommitStatus")
        self.commit_status.setWordWrap(True)
        self.commit_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.commit_status)
        layout.addStretch(1)
        self._set_page_body(self.commit_page, body)

    @staticmethod
    def _set_page_body(page: QWizardPage, body: QWidget) -> None:
        scroll = QScrollArea()
        scroll.setObjectName("csvImportScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(scroll)

    def _apply_preferences(self, preferences: ImportPreferences) -> None:
        type_index = self.type_combo.findData(preferences.import_type)
        self.type_combo.setCurrentIndex(type_index if type_index >= 0 else 0)
        policy_index = self.duplicate_policy_combo.findData(preferences.duplicate_policy)
        self.duplicate_policy_combo.setCurrentIndex(policy_index if policy_index >= 0 else 0)

    def _clear_workflow_state(self, *, clear_mapping: bool, clear_controls: bool = True) -> None:
        if clear_mapping:
            self._mapping_by_type = {}
        if clear_controls:
            self._mapping_controls = {}
        self.mapping_validation = None
        self.active_preview = None
        self.validation = None
        self.import_result = None
        self.saved_import_type = None
        self._committed = False
        for table in (self.source_preview_table, self.mapped_preview_table):
            model = cast(ImportTableModel, table.model())
            model.set_data((), ())
        self.preview_status.setText("")
        self.validation_summary.setText("")
        self.confirmation_summary.setText("")
        self.commit_status.setText("")

    def _start_directory(self) -> str:
        preferences = self.context.settings.get_import_preferences()
        for candidate in (preferences.source_directory, self.context.default_working_directory, self.context.paths.project_root):
            if candidate is not None:
                try:
                    if candidate.is_dir():
                        return str(candidate)
                except (OSError, ValueError):
                    continue
        return str(self.context.paths.project_root)

    def browse_source(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Select CSV source",
            self._start_directory(),
            "CSV files (*.csv);;All files (*)",
        )
        if not selected:
            return
        self.source_path_edit.setText(selected)
        self.detection = None
        self._clear_workflow_state(clear_mapping=True)
        self._source_loaded = ""
        self.detected_type_value.setText("Not detected")
        self.encoding_value.setText("Not detected")
        self.source_status.setText("CSV selected. Continue to detect its encoding and import type.")

    def _type_changed(self, *_args: object) -> None:
        if self.detection is not None:
            self._clear_workflow_state(clear_mapping=True)

    def _duplicate_policy_changed(self, *_args: object) -> None:
        if self.detection is not None and self.active_preview is not None:
            self._prepare_preview_page()

    def _source_path(self) -> Path | None:
        value = self.source_path_edit.text().strip()
        if not value:
            return None
        try:
            return Path(value).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None

    def _validate_source_page(self) -> bool:
        path = self._source_path()
        if path is None:
            self.source_status.setText("Choose a CSV source file before continuing.")
            return False
        if str(path) != self._source_loaded:
            try:
                self.detection = self.context.csv_importer.detect(path)
                self._source_loaded = str(path)
                self._clear_workflow_state(clear_mapping=True)
            except (OSError, UnicodeError, ValueError, csv.Error) as error:
                self.detection = None
                self._clear_workflow_state(clear_mapping=True)
                self.source_status.setText(f"The CSV could not be read safely: {error}")
                return False
            except Exception as error:
                self.context.logger.exception("CSV detection failed")
                self.detection = None
                self._clear_workflow_state(clear_mapping=True)
                self.source_status.setText("The CSV could not be detected. See logs/error.log for details.")
                return False
        assert self.detection is not None
        self.detected_type_value.setText(IMPORT_TYPE_LABELS[self.detection.status])
        self.encoding_value.setText(self.detection.encoding or "Not recorded")
        self.source_status.setText(self.detection.reason)
        if self.detection.status == UNSUPPORTED_IMPORT:
            return False
        if self.detection.status == AMBIGUOUS_IMPORT and self.type_combo.currentData() == "auto":
            self.source_status.setText("The CSV shape is ambiguous. Choose Benchmark Runs or Scoreboard explicitly before continuing.")
            return False
        return True

    def _effective_import_type(self) -> CsvImportType | None:
        if self.detection is None:
            return None
        selected = self.type_combo.currentData()
        if selected == "auto":
            if self.detection.status == BENCHMARK_RUN_IMPORT:
                return BENCHMARK_RUN_IMPORT
            if self.detection.status == SCOREBOARD_IMPORT:
                return SCOREBOARD_IMPORT
            return None
        if selected in CSV_IMPORT_TYPES:
            if self.detection.status == UNSUPPORTED_IMPORT:
                return None
            return cast(CsvImportType, selected)
        return None

    def _preview_for_type(
        self,
        import_type: CsvImportType,
        mapping: dict[str, str | None] | None = None,
    ) -> ImportPreview:
        path = self._source_path()
        if path is None or self.detection is None:
            raise ValueError("a CSV source must be selected first")
        if self.detection.encoding is None:
            raise ValueError("the CSV encoding is unsupported")
        return self.context.csv_importer.preview(
            path,
            mapping,
            summary=import_type == SCOREBOARD_IMPORT,
            encoding=self.detection.encoding,
        )

    def _prepare_mapping_page(self) -> None:
        if self.detection is None and not self._validate_source_page():
            return
        import_type = self._effective_import_type()
        if import_type is None:
            return
        automatic_preview = self._preview_for_type(import_type)
        self._mapping_by_type.setdefault(import_type, dict(automatic_preview.mapping))
        mapping = self._mapping_by_type[import_type]
        metadata = self.context.csv_importer.mapping_field_metadata(import_type)
        fields = tuple(item.name for item in metadata)
        required = {item.name for item in metadata if item.required}
        warning_by_heading: dict[str, list[str]] = {}
        for warning in automatic_preview.mapping_warnings:
            warning_by_heading.setdefault(warning.source_heading, []).append(warning.message)
        self._mapping_controls = {}
        self.mapping_table.setRowCount(0)
        headings = list(mapping)
        self.mapping_table.setRowCount(len(headings))
        mapped_column_width = _MAPPING_POPUP_MIN_WIDTH
        for row_index, heading in enumerate(headings):
            heading_item = QTableWidgetItem(heading)
            warning_text = "\n".join(warning_by_heading.get(heading, ()))
            heading_item.setToolTip("\n".join(value for value in (heading, warning_text) if value))
            self.mapping_table.setItem(row_index, 0, heading_item)
            combo = QComboBox(self.mapping_table)
            combo.setAccessibleName(f"Mapping for {heading}")
            combo.addItem("Ignore", None)
            for field_name in fields:
                label = f"{field_name} (required)" if field_name in required else field_name
                combo.addItem(label, field_name)
            target = mapping.get(heading)
            target_index = combo.findData(target)
            combo.setCurrentIndex(target_index if target_index >= 0 else 0)
            minimum_height, minimum_width = _configure_mapping_combo(combo)
            mapped_column_width = max(mapped_column_width, minimum_width)
            combo.currentIndexChanged.connect(
                lambda _index, key=heading, kind=import_type: self._mapping_changed(cast(CsvImportType, kind), key)
            )
            if warning_text:
                combo.setToolTip(warning_text)
            cell = QWidget(self.mapping_table)
            cell.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(
                0,
                _MAPPING_CELL_VERTICAL_MARGIN,
                0,
                _MAPPING_CELL_VERTICAL_MARGIN,
            )
            cell_layout.setSpacing(0)
            cell_layout.addWidget(combo)
            self.mapping_table.setCellWidget(row_index, 1, cell)
            self.mapping_table.setRowHeight(
                row_index,
                max(
                    _MAPPING_ROW_HEIGHT_FLOOR,
                    minimum_height
                    + (2 * (_MAPPING_CELL_VERTICAL_MARGIN + _MAPPING_TABLE_ITEM_VERTICAL_PADDING))
                    + _MAPPING_GRID_LINE_ALLOWANCE,
                ),
            )
            self._mapping_controls[heading] = combo
            requirement = "Required target" if target in required else "Optional / ignored"
            self.mapping_table.setItem(row_index, 2, QTableWidgetItem(requirement))
        self.mapping_table.setColumnWidth(1, mapped_column_width)
        status_lines = [
            f"{len(headings)} source headings found. Model Name is the only required engine target; all other validation remains in the engine."
        ]
        if automatic_preview.mapping_warnings:
            status_lines.append(
                "Mapping warnings:\n"
                + "\n".join(warning.message for warning in automatic_preview.mapping_warnings)
            )
        self.mapping_status.setText("\n".join(status_lines))

    def _mapping_combo_for_row(self, row: int) -> QComboBox:
        """Return the real mapping combo regardless of its cell presentation wrapper."""

        cell = self.mapping_table.cellWidget(row, 1)
        if isinstance(cell, QComboBox):
            return cell
        combo = cell.findChild(QComboBox) if cell is not None else None
        if combo is None:
            raise RuntimeError(f"Mapping row {row} does not contain a combo box")
        return combo

    def _mapping_changed(self, import_type: CsvImportType, heading: str) -> None:
        combo = self._mapping_controls.get(heading)
        if combo is not None:
            self._mapping_by_type[import_type][heading] = combo.currentData()
            self._clear_workflow_state(clear_mapping=False, clear_controls=False)
            self.mapping_status.setText("Mapping changed. Continue to revalidate the selected destinations.")

    def _validate_mapping_page(self) -> bool:
        import_type = self._effective_import_type()
        if import_type is None:
            self.mapping_status.setText("Choose a supported CSV import type.")
            return False
        for heading, combo in self._mapping_controls.items():
            self._mapping_by_type[import_type][heading] = combo.currentData()
        validation = self.context.csv_importer.validate_mapping(
            self._mapping_by_type[import_type],
            import_type,
        )
        self.mapping_validation = validation
        if not validation.is_valid:
            self._clear_workflow_state(clear_mapping=False, clear_controls=False)
            self.mapping_validation = validation
            self.mapping_status.setText(
                "Mapping errors:\n" + "\n".join(issue.message for issue in validation.errors)
            )
            return False
        return True

    def _prepare_preview_page(self) -> None:
        if self.detection is None and not self._validate_source_page():
            return
        import_type = self._effective_import_type()
        if import_type is None:
            return
        mapping = self._mapping_by_type.get(import_type, {})
        try:
            mapping_validation = self.context.csv_importer.validate_mapping(mapping, import_type)
            if not mapping_validation.is_valid:
                self._clear_workflow_state(clear_mapping=False, clear_controls=False)
                self.mapping_validation = mapping_validation
                self.mapping_status.setText(
                    "Mapping errors:\n" + "\n".join(issue.message for issue in mapping_validation.errors)
                )
                return
            preview = self._preview_for_type(import_type, mapping)
            path = self._source_path()
            assert path is not None
            validation = self.context.csv_importer.validate_rows(
                preview.rows,
                import_type=import_type,
                row_numbers=preview.row_numbers,
                duplicate_policy=str(self.duplicate_policy_combo.currentData()),
                source_file=path,
            )
        except ImportMappingError as error:
            self._clear_workflow_state(clear_mapping=False, clear_controls=False)
            self.mapping_validation = error.validation
            self.mapping_status.setText(
                "Mapping errors:\n" + "\n".join(issue.message for issue in error.validation.errors)
            )
            return
        except (OSError, UnicodeError, ValueError, csv.Error) as error:
            self.active_preview = None
            self.validation = None
            self.preview_status.setText(f"The preview could not be prepared: {error}")
            return
        except Exception:
            self.context.logger.exception("CSV preview failed")
            self.active_preview = None
            self.validation = None
            self.preview_status.setText("The preview could not be prepared. See logs/error.log for details.")
            return

        self.active_preview = preview
        self.validation = validation
        self._populate_preview_tables(preview)
        self._update_import_options(import_type)
        self.preview_status.setText(
            f"{len(preview.rows)} importable row(s); {len(preview.skipped_rows)} skipped row(s); "
            f"{len(preview.unknown_headings)} ignored heading(s)."
        )
        self.validation_summary.setText(self._validation_text(preview, validation))

    def _populate_preview_tables(self, preview: ImportPreview) -> None:
        source_headers = ("Source row", *preview.headings)
        source_rows = [
            (row_number, *(source_row.get(heading, "") for heading in preview.headings))
            for row_number, source_row in zip(preview.source_row_numbers, preview.source_rows)
        ]
        source_model = cast(ImportTableModel, self.source_preview_table.model())
        source_model.set_data(source_headers, source_rows[:20])
        mapped_fields = list(dict.fromkeys(target for target in preview.mapping.values() if target))
        mapped_headers = ("Source row", *mapped_fields)
        mapped_rows = [
            (row_number, *(row.get(field_name, "") for field_name in mapped_fields))
            for row_number, row in zip(preview.row_numbers, preview.rows)
        ]
        mapped_model = cast(ImportTableModel, self.mapped_preview_table.model())
        mapped_model.set_data(mapped_headers, mapped_rows[:20])
        for table in (self.source_preview_table, self.mapped_preview_table):
            table.resizeColumnsToContents()

    def _update_import_options(self, import_type: CsvImportType) -> None:
        is_run = import_type == BENCHMARK_RUN_IMPORT
        self.duplicate_policy_combo.setVisible(is_run)
        self.batch_name_edit.setVisible(not is_run)
        self.batch_notes_edit.setVisible(not is_run)
        if self.duplicate_policy_label is not None:
            self.duplicate_policy_label.setVisible(is_run)
        if self.batch_name_label is not None:
            self.batch_name_label.setVisible(not is_run)
        if self.batch_notes_label is not None:
            self.batch_notes_label.setVisible(not is_run)

    @staticmethod
    def _validation_text(preview: ImportPreview, validation: ImportValidationResult) -> str:
        lines: list[str] = []
        if preview.mapping_warnings:
            lines.append("Mapping warnings:")
            lines.extend(warning.message for warning in preview.mapping_warnings)
        if preview.unknown_headings:
            lines.append("Ignored headings: " + ", ".join(preview.unknown_headings))
        if preview.skipped_rows:
            lines.append("Skipped rows: " + "; ".join(f"Row {number}: {reason}" for number, reason in preview.skipped_rows))
        if validation.duplicate_count:
            lines.append(f"Duplicates detected: {validation.duplicate_count}")
        if validation.errors:
            lines.append("Validation errors:")
            lines.extend(f"Row {issue.row_number}: {issue.message}" for issue in validation.errors)
        if not lines:
            return "No warnings or validation errors were found."
        return "\n".join(lines)

    def _validate_preview_page(self) -> bool:
        import_type = self._effective_import_type()
        if import_type is None:
            return False
        mapping = self._mapping_by_type.get(import_type, {})
        mapping_validation = self.context.csv_importer.validate_mapping(mapping, import_type)
        if not mapping_validation.is_valid:
            self._clear_workflow_state(clear_mapping=False, clear_controls=False)
            self.mapping_validation = mapping_validation
            self.mapping_status.setText(
                "Mapping errors:\n" + "\n".join(issue.message for issue in mapping_validation.errors)
            )
            return False
        self.mapping_validation = mapping_validation
        if self.active_preview is None or self.validation is None:
            self._prepare_preview_page()
        if self.active_preview is None or self.validation is None:
            return False
        if not self.active_preview.rows:
            self.validation_summary.setText("No importable rows remain. Nothing will be written.")
            return False
        if not self.validation.is_valid:
            return False
        return True

    def _prepare_commit_page(self) -> None:
        import_type = self._effective_import_type()
        preview = self.active_preview
        validation = self.validation
        if import_type is None or preview is None or validation is None:
            self.confirmation_summary.setText("The import preview is not ready.")
            return
        path = self._source_path()
        assert path is not None
        lines = [
            f"Import type: {IMPORT_TYPE_LABELS[import_type]}",
            f"Source: {path}",
            f"Encoding: {self.detection.encoding if self.detection and self.detection.encoding else 'Not recorded'}",
            f"Rows to commit: {len(preview.rows)}",
            f"Rows skipped: {len(preview.skipped_rows)}",
        ]
        if import_type == BENCHMARK_RUN_IMPORT:
            lines.append(f"Duplicate policy: {DUPLICATE_POLICY_LABELS[str(self.duplicate_policy_combo.currentData())]}")
        else:
            batch_name = self.batch_name_edit.text().strip() or "(engine default)"
            lines.append(f"Scoreboard batch: {batch_name}")
        if validation.duplicate_count:
            lines.append(f"Duplicates detected: {validation.duplicate_count}")
        self.confirmation_summary.setText("\n".join(lines))
        self.commit_status.setText("Click Finish to commit. Cancel leaves the database and settings unchanged.")

    def _commit(self) -> bool:
        if self._saving or self._committed:
            return False
        if not self._validate_preview_page():
            self.commit_status.setText("The import has validation errors or no importable rows. Nothing was written.")
            return False
        import_type = self._effective_import_type()
        preview = self.active_preview
        path = self._source_path()
        if import_type is None or preview is None or path is None:
            self.commit_status.setText("The import is not ready. Nothing was written.")
            return False
        self._saving = True
        try:
            if import_type == BENCHMARK_RUN_IMPORT:
                result = self.context.csv_importer.import_rows(
                    preview.rows,
                    str(self.duplicate_policy_combo.currentData()),
                    row_numbers=preview.row_numbers,
                )
            else:
                result = self.context.csv_importer.import_scoreboard_entries(
                    preview.rows,
                    path,
                    self.batch_name_edit.text().strip() or None,
                    self.batch_notes_edit.toPlainText(),
                    row_numbers=preview.row_numbers,
                )
        except (OSError, UnicodeError, ValueError, csv.Error) as error:
            self._clear_workflow_state(clear_mapping=False, clear_controls=False)
            self.commit_status.setText(f"Import failed; no rows were written: {error}")
            return False
        except Exception:
            self.context.logger.exception("CSV import commit failed")
            self._clear_workflow_state(clear_mapping=False, clear_controls=False)
            self.commit_status.setText("Import failed; no rows were written. See logs/error.log for details.")
            return False
        finally:
            self._saving = False

        self.import_result = result
        self.saved_import_type = import_type
        self._committed = True
        try:
            self.context.settings.set_import_preferences(
                ImportPreferences(
                    source_directory=path.parent,
                    import_type=str(self.type_combo.currentData()),
                    duplicate_policy=str(self.duplicate_policy_combo.currentData()),
                )
            )
        except Exception:
            self.context.logger.exception("CSV import preferences could not be saved")
        self.import_completed.emit(import_type)
        self.commit_status.setText(
            f"Import complete: {result.imported} imported, {result.skipped} skipped, "
            f"{result.replaced} replaced, {result.duplicates} duplicate(s)."
        )
        return True

    def reject(self) -> None:
        """Cancel the wizard without persisting preview state or settings."""

        super().reject()


__all__ = (
    "BENCHMARK_RUN_IMPORT",
    "DUPLICATE_POLICY_LABELS",
    "IMPORT_TYPE_LABELS",
    "SCOREBOARD_IMPORT",
    "CsvImportWizard",
)
