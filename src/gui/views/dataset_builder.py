"""Configure, preview, and build curated JSONL datasets."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDate, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

try:  # Support both ``python -m src.gui`` and test imports with ``src`` on PATH.
    from ...engine.datasets import (
        DatasetFileValidation,
        DatasetManifestPairValidation,
        DatasetFilters,
        DatasetPreview,
        DatasetValidationState as EngineDatasetValidationState,
        DatasetWriteResult,
        DatasetWriteStatus,
        ManifestValidation,
        ManifestValidationState as EngineManifestValidationState,
        PairValidationState as EnginePairValidationState,
        RedactionConfig,
        ValidationIssue,
    )
    from ...engine.domain import BENCHMARK_TYPES, LEVELS
    from ...engine.settings import DatasetBuilderPreferences
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.datasets import (  # type: ignore[no-redef]
        DatasetFileValidation,
        DatasetManifestPairValidation,
        DatasetFilters,
        DatasetPreview,
        DatasetValidationState as EngineDatasetValidationState,
        DatasetWriteResult,
        DatasetWriteStatus,
        ManifestValidation,
        ManifestValidationState as EngineManifestValidationState,
        PairValidationState as EnginePairValidationState,
        RedactionConfig,
        ValidationIssue,
    )
    from engine.domain import BENCHMARK_TYPES, LEVELS  # type: ignore[no-redef]
    from engine.settings import DatasetBuilderPreferences  # type: ignore[no-redef]
from ..context import GuiApplicationContext
from ..models.dataset_preview_model import DatasetPreviewTableModel
from ..models.validation_issue_model import ValidationIssueTableModel


class DatasetBuilderState(str, Enum):
    """Explicit build-tab states used to gate preview and write actions."""

    CONFIGURE = "configure"
    PREVIEWING = "previewing"
    PREVIEW_READY = "preview_ready"
    BUILDING = "building"
    SUCCESS = "success"
    RECOVERABLE_FAILURE = "recoverable_failure"
    STALE_PREVIEW = "stale_preview"


class DatasetValidationMode(str, Enum):
    """Standalone validation workflow selected in the validation tab."""

    DATASET = "dataset"
    MANIFEST = "manifest"
    PAIR = "pair"


class DatasetValidationGuiState(str, Enum):
    """GUI state for the non-mutating validation workflow."""

    EMPTY = "empty"
    READY = "ready"
    VALIDATING = "validating"
    VALID = "valid"
    INVALID = "invalid"
    RECOVERABLE_FAILURE = "recoverable_failure"


class DatasetBuilderView(QWidget):
    """Reusable first-class page for DatasetBuilder configuration and output."""

    def __init__(
        self,
        context: GuiApplicationContext,
        parent: QWidget | None = None,
        *,
        confirm_build: Callable[[str], bool] | None = None,
        confirm_overwrite: Callable[[Path], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self._confirm_build = confirm_build
        self._confirm_overwrite = confirm_overwrite
        self._filters = DatasetFilters()
        self._redaction = RedactionConfig()
        self._preview: DatasetPreview | None = None
        self._preview_signature: tuple[DatasetFilters, RedactionConfig] | None = None
        self._write_result: DatasetWriteResult | None = None
        self._initial_directory: Path | None = None
        self._success_jsonl_path: Path | None = None
        self._success_manifest_path: Path | None = None
        self._previewing = False
        self._saving = False
        self._retryable_failure = False
        self._configuration_error = ""
        self._destination_error = ""
        self._state = DatasetBuilderState.CONFIGURE
        self._validation_mode = DatasetValidationMode.DATASET
        self._validation_state = DatasetValidationGuiState.EMPTY
        self._validation_result: (
            DatasetFileValidation | ManifestValidation | DatasetManifestPairValidation | None
        ) = None
        self._validation_busy = False
        self._validation_open_dataset_path: Path | None = None
        self._validation_open_manifest_path: Path | None = None
        self._validation_open_folder_path: Path | None = None
        self._initializing = True

        self.setObjectName("datasetBuilderPage")
        self.setAccessibleName("Dataset Builder page")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._build_ui()
        self._load_preferences()
        self._populate_catalog_choices()
        self._initializing = False
        self._read_and_store_configuration()
        self._invalidate_preview()
        self._update_destination_state()
        self._update_action_state()

    def _build_ui(self) -> None:
        title = QLabel("Dataset Builder")
        title.setObjectName("pageTitle")
        description = QLabel(
            "Configure filters and redaction rules, preview eligible benchmark runs, and build a staged JSONL dataset with its manifest."
        )
        description.setObjectName("pageDescription")
        description.setWordWrap(True)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("datasetBuilderTabs")
        self.tabs.setAccessibleName("Dataset Builder workflows")
        self._build_tab = self._build_dataset_tab()
        self._validation_tab = self._build_validation_tab()
        self.tabs.addTab(self._build_tab, "Build Dataset")
        self.tabs.addTab(self._validation_tab, "Validate Existing Dataset")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 30)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(self.tabs, 1)

    def _build_dataset_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 12, 0, 0)
        tab_layout.setSpacing(10)

        scroll = QScrollArea()
        scroll.setObjectName("datasetBuilderConfigurationScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(2, 2, 10, 2)
        content_layout.setSpacing(12)
        content_layout.addWidget(self._build_filter_group())
        content_layout.addWidget(self._build_redaction_group())
        content_layout.addWidget(self._build_destination_group())
        content_layout.addWidget(self._build_preview_group())
        content_layout.addStretch(1)
        scroll.setWidget(content)
        tab_layout.addWidget(scroll, 1)
        tab_layout.addLayout(self._build_action_row())
        return tab

    def _build_filter_group(self) -> QGroupBox:
        group = QGroupBox("Dataset filters")
        group.setObjectName("datasetFiltersGroup")
        form = QFormLayout(group)
        form.setContentsMargins(12, 10, 12, 10)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)

        self.min_overall_edit = QLineEdit()
        self.min_overall_edit.setObjectName("datasetMinOverallEdit")
        self.min_overall_edit.setPlaceholderText("Blank for no minimum")
        self.min_overall_edit.setClearButtonEnabled(True)
        self.min_overall_edit.textChanged.connect(self._configuration_changed)
        form.addRow("Minimum overall score", self.min_overall_edit)

        self.max_hallucination_combo = self._level_combo("datasetMaxHallucinationCombo")
        self.min_reliability_combo = self._level_combo("datasetMinReliabilityCombo")
        self.max_hallucination_combo.currentIndexChanged.connect(self._configuration_changed)
        self.min_reliability_combo.currentIndexChanged.connect(self._configuration_changed)
        form.addRow("Maximum hallucination", self.max_hallucination_combo)
        form.addRow("Minimum reliability", self.min_reliability_combo)

        self.verdict_edit = QLineEdit()
        self.verdict_edit.setObjectName("datasetVerdictEdit")
        self.verdict_edit.setPlaceholderText("Contains text; blank for any verdict")
        self.verdict_edit.setClearButtonEnabled(True)
        self.verdict_edit.textChanged.connect(self._configuration_changed)
        form.addRow("Verdict", self.verdict_edit)

        self.benchmark_type_combo = QComboBox()
        self.benchmark_type_combo.setObjectName("datasetBenchmarkTypeCombo")
        self.benchmark_type_combo.addItem("Any benchmark type", "")
        for benchmark_type in BENCHMARK_TYPES:
            self.benchmark_type_combo.addItem(benchmark_type.replace("_", " ").title(), benchmark_type)
        self.benchmark_type_combo.currentIndexChanged.connect(self._configuration_changed)
        form.addRow("Benchmark type", self.benchmark_type_combo)

        self.model_combo = QComboBox()
        self.model_combo.setObjectName("datasetModelCombo")
        self.model_combo.currentIndexChanged.connect(self._configuration_changed)
        form.addRow("Model", self.model_combo)

        self.session_combo = QComboBox()
        self.session_combo.setObjectName("datasetSessionCombo")
        self.session_combo.currentIndexChanged.connect(self._configuration_changed)
        form.addRow("Session", self.session_combo)

        self.prompt_template_combo = QComboBox()
        self.prompt_template_combo.setObjectName("datasetPromptTemplateCombo")
        self.prompt_template_combo.currentIndexChanged.connect(self._configuration_changed)
        form.addRow("Prompt template", self.prompt_template_combo)

        self.hardware_combo = QComboBox()
        self.hardware_combo.setObjectName("datasetHardwareCombo")
        self.hardware_combo.currentIndexChanged.connect(self._configuration_changed)
        form.addRow("Hardware profile", self.hardware_combo)

        from_row = QHBoxLayout()
        self.date_from_check = QCheckBox("From")
        self.date_from_check.setObjectName("datasetDateFromCheck")
        self.date_from_edit = self._date_edit("datasetDateFromEdit")
        from_row.addWidget(self.date_from_check)
        from_row.addWidget(self.date_from_edit, 1)
        form.addRow("Date range", from_row)

        to_row = QHBoxLayout()
        self.date_to_check = QCheckBox("To")
        self.date_to_check.setObjectName("datasetDateToCheck")
        self.date_to_edit = self._date_edit("datasetDateToEdit")
        to_row.addWidget(self.date_to_check)
        to_row.addWidget(self.date_to_edit, 1)
        form.addRow("", to_row)
        self.date_from_check.stateChanged.connect(self._date_controls_changed)
        self.date_to_check.stateChanged.connect(self._date_controls_changed)
        self.date_from_edit.dateChanged.connect(self._configuration_changed)
        self.date_to_edit.dateChanged.connect(self._configuration_changed)

        self.include_run_ids_edit = QLineEdit()
        self.include_run_ids_edit.setObjectName("datasetIncludeRunIdsEdit")
        self.include_run_ids_edit.setPlaceholderText("Comma-separated positive IDs; blank for any")
        self.include_run_ids_edit.textChanged.connect(self._configuration_changed)
        form.addRow("Include run IDs", self.include_run_ids_edit)

        self.exclude_run_ids_edit = QLineEdit()
        self.exclude_run_ids_edit.setObjectName("datasetExcludeRunIdsEdit")
        self.exclude_run_ids_edit.setPlaceholderText("Comma-separated positive IDs; blank for none")
        self.exclude_run_ids_edit.textChanged.connect(self._configuration_changed)
        form.addRow("Exclude run IDs", self.exclude_run_ids_edit)

        self.keep_duplicates_check = QCheckBox("Retain exact source duplicates")
        self.keep_duplicates_check.setObjectName("datasetKeepDuplicatesCheck")
        self.keep_duplicates_check.stateChanged.connect(self._configuration_changed)
        form.addRow(self.keep_duplicates_check)

        self.include_provenance_check = QCheckBox("Include source run ID provenance")
        self.include_provenance_check.setObjectName("datasetIncludeProvenanceCheck")
        self.include_provenance_check.setChecked(True)
        self.include_provenance_check.stateChanged.connect(self._configuration_changed)
        form.addRow(self.include_provenance_check)

        self.reset_filters_button = QPushButton("Reset Filters")
        self.reset_filters_button.setObjectName("datasetResetFiltersButton")
        self.reset_filters_button.clicked.connect(self._reset_filters)
        form.addRow(self.reset_filters_button)
        return group

    def _build_redaction_group(self) -> QGroupBox:
        group = QGroupBox("Redaction")
        group.setObjectName("datasetRedactionGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        toggles = QHBoxLayout()
        self.redact_paths_check = self._redaction_check("Paths", "datasetRedactPathsCheck", True)
        self.redact_usernames_check = self._redaction_check("Usernames", "datasetRedactUsernamesCheck", False)
        self.redact_email_check = self._redaction_check("Email addresses", "datasetRedactEmailCheck", True)
        self.redact_hosts_check = self._redaction_check("Hosts/IPs", "datasetRedactHostsCheck", True)
        for checkbox in (
            self.redact_paths_check,
            self.redact_usernames_check,
            self.redact_email_check,
            self.redact_hosts_check,
        ):
            toggles.addWidget(checkbox)
        toggles.addStretch(1)
        layout.addLayout(toggles)

        self.literal_list, literal_row = self._build_rule_editor(
            "Literal terms", "datasetLiteral", "Literal term"
        )
        self.regex_list, regex_row = self._build_rule_editor(
            "Custom regex rules", "datasetRegex", "Regular expression"
        )
        layout.addWidget(QLabel("Literal terms"))
        layout.addWidget(self.literal_list)
        layout.addLayout(literal_row)
        layout.addWidget(QLabel("Custom regex rules"))
        layout.addWidget(self.regex_list)
        layout.addLayout(regex_row)

        self.reset_redaction_button = QPushButton("Reset Redaction")
        self.reset_redaction_button.setObjectName("datasetResetRedactionButton")
        self.reset_redaction_button.clicked.connect(self._reset_redaction)
        layout.addWidget(self.reset_redaction_button, alignment=Qt.AlignmentFlag.AlignLeft)
        return group

    def _build_rule_editor(
        self,
        _title: str,
        prefix: str,
        placeholder: str,
    ) -> tuple[QListWidget, QHBoxLayout]:
        rule_list = QListWidget()
        rule_list.setObjectName(f"{prefix}List")
        rule_list.setAccessibleName(placeholder)
        rule_list.setMinimumHeight(70)
        rule_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)

        editor = QLineEdit()
        editor.setObjectName(f"{prefix}Edit")
        editor.setPlaceholderText(placeholder)
        editor.setClearButtonEnabled(True)
        add_button = QPushButton("Add")
        add_button.setObjectName(f"{prefix}AddButton")
        edit_button = QPushButton("Edit")
        edit_button.setObjectName(f"{prefix}EditButton")
        remove_button = QPushButton("Remove")
        remove_button.setObjectName(f"{prefix}RemoveButton")
        add_button.clicked.connect(lambda _checked=False: self._add_rule(rule_list, editor))
        edit_button.clicked.connect(lambda _checked=False: self._edit_rule(rule_list, editor))
        remove_button.clicked.connect(lambda _checked=False: self._remove_rule(rule_list, editor))
        rule_list.currentRowChanged.connect(lambda _row: self._load_selected_rule(rule_list, editor))
        row = QHBoxLayout()
        row.addWidget(editor, 1)
        row.addWidget(add_button)
        row.addWidget(edit_button)
        row.addWidget(remove_button)
        return rule_list, row

    def _build_destination_group(self) -> QGroupBox:
        group = QGroupBox("Destination")
        group.setObjectName("datasetDestinationGroup")
        form = QFormLayout(group)
        form.setContentsMargins(12, 10, 12, 10)
        row = QHBoxLayout()
        self.destination_edit = QLineEdit()
        self.destination_edit.setObjectName("datasetDestinationEdit")
        self.destination_edit.setAccessibleName("JSONL dataset destination")
        self.destination_edit.setPlaceholderText("Choose a JSONL file in an existing folder")
        self.destination_edit.textChanged.connect(self._destination_changed)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.setObjectName("datasetBrowseButton")
        self.browse_button.clicked.connect(self.browse_destination)
        row.addWidget(self.destination_edit, 1)
        row.addWidget(self.browse_button)
        form.addRow("JSONL file", row)

        self.manifest_path_label = QLabel("Manifest: choose a JSONL destination")
        self.manifest_path_label.setObjectName("datasetManifestPath")
        self.manifest_path_label.setWordWrap(True)
        form.addRow("Manifest", self.manifest_path_label)
        self.destination_error_label = QLabel()
        self.destination_error_label.setObjectName("datasetDestinationError")
        self.destination_error_label.setWordWrap(True)
        form.addRow(self.destination_error_label)
        return group

    def _build_preview_group(self) -> QGroupBox:
        group = QGroupBox("Preview")
        group.setObjectName("datasetPreviewGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.preview_summary = QLabel("No preview yet.")
        self.preview_summary.setObjectName("datasetPreviewSummary")
        self.preview_summary.setWordWrap(True)
        layout.addWidget(self.preview_summary)

        self.exclusion_summary = QLabel("Exclusions: None")
        self.exclusion_summary.setObjectName("datasetExclusionSummary")
        self.exclusion_summary.setWordWrap(True)
        layout.addWidget(self.exclusion_summary)
        self.warning_summary = QLabel("Warnings: None")
        self.warning_summary.setObjectName("datasetWarningSummary")
        self.warning_summary.setWordWrap(True)
        layout.addWidget(self.warning_summary)
        self.redaction_summary = QLabel("Redactions: None")
        self.redaction_summary.setObjectName("datasetRedactionSummary")
        self.redaction_summary.setWordWrap(True)
        layout.addWidget(self.redaction_summary)
        self.filter_summary = QLabel("Active filters: default")
        self.filter_summary.setObjectName("datasetFilterSummary")
        self.filter_summary.setWordWrap(True)
        layout.addWidget(self.filter_summary)
        self.active_redaction_summary = QLabel("Active redaction: default")
        self.active_redaction_summary.setObjectName("datasetActiveRedactionSummary")
        self.active_redaction_summary.setWordWrap(True)
        layout.addWidget(self.active_redaction_summary)

        self.preview_model = DatasetPreviewTableModel(self)
        self.preview_table = QTableView()
        self.preview_table.setObjectName("datasetPreviewTable")
        self.preview_table.setAccessibleName("Eligible dataset records")
        self.preview_table.setModel(self.preview_model)
        self.preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.preview_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.preview_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.preview_table.setWordWrap(False)
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        self.preview_table.setMinimumHeight(180)
        layout.addWidget(self.preview_table)

        self.configuration_error_label = QLabel()
        self.configuration_error_label.setObjectName("datasetConfigurationError")
        self.configuration_error_label.setWordWrap(True)
        layout.addWidget(self.configuration_error_label)
        return group

    def _build_action_row(self) -> QHBoxLayout:
        self.status_label = QLabel("Configure the dataset, then preview it.")
        self.status_label.setObjectName("datasetBuilderStatus")
        self.status_label.setWordWrap(True)
        self.preview_button = QPushButton("Preview")
        self.preview_button.setObjectName("datasetPreviewButton")
        self.preview_button.setAccessibleName("Preview eligible dataset records")
        self.preview_button.clicked.connect(self.preview)
        self.build_button = QPushButton("Build JSONL Dataset")
        self.build_button.setObjectName("datasetBuildButton")
        self.build_button.setAccessibleName("Build JSONL dataset")
        self.build_button.clicked.connect(self.build)
        self.open_jsonl_button = QPushButton("Open JSONL")
        self.open_jsonl_button.setObjectName("datasetOpenJsonlButton")
        self.open_jsonl_button.clicked.connect(self.open_jsonl)
        self.open_manifest_button = QPushButton("Open Manifest")
        self.open_manifest_button.setObjectName("datasetOpenManifestButton")
        self.open_manifest_button.clicked.connect(self.open_manifest)
        self.open_folder_button = QPushButton("Open Containing Folder")
        self.open_folder_button.setObjectName("datasetOpenFolderButton")
        self.open_folder_button.clicked.connect(self.open_folder)
        for button in (self.open_jsonl_button, self.open_manifest_button, self.open_folder_button):
            button.setVisible(False)

        row = QHBoxLayout()
        row.addWidget(self.status_label, 1)
        row.addWidget(self.open_jsonl_button)
        row.addWidget(self.open_manifest_button)
        row.addWidget(self.open_folder_button)
        row.addWidget(self.preview_button)
        row.addWidget(self.build_button)
        return row

    def _build_validation_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 12, 0, 0)
        tab_layout.setSpacing(10)

        scroll = QScrollArea()
        scroll.setObjectName("datasetValidationScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(2, 2, 10, 2)
        content_layout.setSpacing(12)
        content_layout.addWidget(self._build_validation_mode_group())
        content_layout.addWidget(self._build_validation_paths_group())
        content_layout.addWidget(self._build_validation_result_group())
        content_layout.addStretch(1)
        scroll.setWidget(content)
        tab_layout.addWidget(scroll, 1)
        tab_layout.addLayout(self._build_validation_action_row())
        self._update_validation_mode_controls()
        self._clear_validation_result()
        return tab

    def _build_validation_mode_group(self) -> QGroupBox:
        group = QGroupBox("Validation mode")
        group.setObjectName("datasetValidationModeGroup")
        form = QFormLayout(group)
        form.setContentsMargins(12, 10, 12, 10)
        form.setHorizontalSpacing(14)
        self.validation_mode_combo = QComboBox()
        self.validation_mode_combo.setObjectName("datasetValidationModeCombo")
        self.validation_mode_combo.setAccessibleName("Dataset validation mode")
        self.validation_mode_combo.addItem("Validate JSONL Dataset", DatasetValidationMode.DATASET.value)
        self.validation_mode_combo.addItem("Validate Manifest", DatasetValidationMode.MANIFEST.value)
        self.validation_mode_combo.addItem("Verify Dataset + Manifest Pair", DatasetValidationMode.PAIR.value)
        self.validation_mode_combo.currentIndexChanged.connect(self._validation_mode_changed)
        form.addRow("Workflow", self.validation_mode_combo)
        return group

    def _build_validation_paths_group(self) -> QGroupBox:
        group = QGroupBox("Validation inputs")
        group.setObjectName("datasetValidationPathsGroup")
        form = QFormLayout(group)
        form.setContentsMargins(12, 10, 12, 10)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)

        self.validation_dataset_path_label = QLabel("Dataset JSONL path")
        self.validation_dataset_edit = QLineEdit()
        self.validation_dataset_edit.setObjectName("datasetValidationDatasetEdit")
        self.validation_dataset_edit.setAccessibleName("Dataset JSONL path")
        self.validation_dataset_edit.setPlaceholderText("Choose an existing JSONL dataset")
        self.validation_dataset_edit.textChanged.connect(self._validation_path_changed)
        self.validation_dataset_browse_button = QPushButton("Browse…")
        self.validation_dataset_browse_button.setObjectName("datasetValidationDatasetBrowseButton")
        self.validation_dataset_browse_button.clicked.connect(self._browse_validation_dataset)
        dataset_row = QHBoxLayout()
        dataset_row.addWidget(self.validation_dataset_edit, 1)
        dataset_row.addWidget(self.validation_dataset_browse_button)
        form.addRow(self.validation_dataset_path_label, dataset_row)

        self.validation_manifest_path_label = QLabel("Manifest path")
        self.validation_manifest_edit = QLineEdit()
        self.validation_manifest_edit.setObjectName("datasetValidationManifestEdit")
        self.validation_manifest_edit.setAccessibleName("Manifest path")
        self.validation_manifest_edit.setPlaceholderText("Choose an existing manifest JSON file")
        self.validation_manifest_edit.textChanged.connect(self._validation_path_changed)
        self.validation_manifest_browse_button = QPushButton("Browse…")
        self.validation_manifest_browse_button.setObjectName("datasetValidationManifestBrowseButton")
        self.validation_manifest_browse_button.clicked.connect(self._browse_validation_manifest)
        manifest_row = QHBoxLayout()
        manifest_row.addWidget(self.validation_manifest_edit, 1)
        manifest_row.addWidget(self.validation_manifest_browse_button)
        form.addRow(self.validation_manifest_path_label, manifest_row)

        self.use_adjacent_manifest_button = QPushButton("Use Adjacent Manifest")
        self.use_adjacent_manifest_button.setObjectName("datasetValidationAdjacentManifestButton")
        self.use_adjacent_manifest_button.setAccessibleName("Use adjacent dataset manifest")
        self.use_adjacent_manifest_button.clicked.connect(self._use_adjacent_manifest)
        self.validation_pair_helper_label = QLabel("Pair helper")
        form.addRow(self.validation_pair_helper_label, self.use_adjacent_manifest_button)
        return group

    def _build_validation_result_group(self) -> QGroupBox:
        group = QGroupBox("Validation result")
        group.setObjectName("datasetValidationResultGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.validation_status_label = QLabel("Choose the required path(s) to begin validation.")
        self.validation_status_label.setObjectName("datasetValidationStatus")
        self.validation_status_label.setWordWrap(True)
        layout.addWidget(self.validation_status_label)

        self.validation_summary_label = QLabel("No validation result.")
        self.validation_summary_label.setObjectName("datasetValidationSummary")
        self.validation_summary_label.setWordWrap(True)
        self.validation_summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        layout.addWidget(self.validation_summary_label)

        self.validation_metadata_label = QLabel("Metadata: Not reported")
        self.validation_metadata_label.setObjectName("datasetValidationMetadata")
        self.validation_metadata_label.setWordWrap(True)
        self.validation_metadata_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        layout.addWidget(self.validation_metadata_label)

        self.validation_issue_summary_label = QLabel("Issues: None")
        self.validation_issue_summary_label.setObjectName("datasetValidationIssueSummary")
        self.validation_issue_summary_label.setWordWrap(True)
        layout.addWidget(self.validation_issue_summary_label)

        self.validation_issue_model = ValidationIssueTableModel(self)
        self._validation_issue_model = self.validation_issue_model
        self.validation_issue_table = QTableView()
        self.validation_issue_table.setObjectName("datasetValidationIssuesTable")
        self.validation_issue_table.setAccessibleName("Dataset validation issues")
        self.validation_issue_table.setModel(self._validation_issue_model)
        self.validation_issue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.validation_issue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.validation_issue_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.validation_issue_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.validation_issue_table.setWordWrap(True)
        self.validation_issue_table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.validation_issue_table.setAlternatingRowColors(True)
        self.validation_issue_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.validation_issue_table.horizontalHeader().setStretchLastSection(True)
        self.validation_issue_table.verticalHeader().setDefaultSectionSize(34)
        self.validation_issue_table.setMinimumHeight(180)
        layout.addWidget(self.validation_issue_table)
        return group

    def _build_validation_action_row(self) -> QHBoxLayout:
        self.validation_clear_button = QPushButton("Clear")
        self.validation_clear_button.setObjectName("datasetValidationClearButton")
        self.validation_clear_button.clicked.connect(self._clear_validation_inputs)

        self.validation_open_dataset_button = QPushButton("Open Dataset")
        self.validation_open_dataset_button.setObjectName("datasetValidationOpenDatasetButton")
        self.validation_open_dataset_button.clicked.connect(self._open_validation_dataset)
        self.validation_open_manifest_button = QPushButton("Open Manifest")
        self.validation_open_manifest_button.setObjectName("datasetValidationOpenManifestButton")
        self.validation_open_manifest_button.clicked.connect(self._open_validation_manifest)
        self.validation_open_folder_button = QPushButton("Open Containing Folder")
        self.validation_open_folder_button.setObjectName("datasetValidationOpenFolderButton")
        self.validation_open_folder_button.clicked.connect(self._open_validation_folder)

        self.validation_action_button = QPushButton("Validate Dataset")
        self.validation_action_button.setObjectName("datasetValidationActionButton")
        self.validation_action_button.setAccessibleName("Run selected dataset validation")
        self.validation_action_button.clicked.connect(self.validate_current)
        for button in (
            self.validation_open_dataset_button,
            self.validation_open_manifest_button,
            self.validation_open_folder_button,
        ):
            button.setEnabled(False)

        row = QHBoxLayout()
        row.addWidget(self.validation_clear_button)
        row.addStretch(1)
        row.addWidget(self.validation_open_dataset_button)
        row.addWidget(self.validation_open_manifest_button)
        row.addWidget(self.validation_open_folder_button)
        row.addWidget(self.validation_action_button)
        return row

    def _validation_mode_changed(self, _index: int) -> None:
        try:
            self._validation_mode = DatasetValidationMode(
                str(self.validation_mode_combo.currentData())
            )
        except ValueError:
            self._validation_mode = DatasetValidationMode.DATASET
        self._clear_validation_result()
        self._update_validation_mode_controls()

    def _update_validation_mode_controls(self) -> None:
        dataset_visible = self._validation_mode in {
            DatasetValidationMode.DATASET,
            DatasetValidationMode.PAIR,
        }
        manifest_visible = self._validation_mode in {
            DatasetValidationMode.MANIFEST,
            DatasetValidationMode.PAIR,
        }
        pair_visible = self._validation_mode is DatasetValidationMode.PAIR
        for widget in (
            self.validation_dataset_path_label,
            self.validation_dataset_edit,
            self.validation_dataset_browse_button,
        ):
            widget.setVisible(dataset_visible)
        for widget in (
            self.validation_manifest_path_label,
            self.validation_manifest_edit,
            self.validation_manifest_browse_button,
        ):
            widget.setVisible(manifest_visible)
        self.validation_pair_helper_label.setVisible(pair_visible)
        self.use_adjacent_manifest_button.setVisible(pair_visible)
        action_labels = {
            DatasetValidationMode.DATASET: "Validate Dataset",
            DatasetValidationMode.MANIFEST: "Validate Manifest",
            DatasetValidationMode.PAIR: "Verify Pair",
        }
        self.validation_action_button.setText(action_labels[self._validation_mode])
        self.validation_action_button.setAccessibleName(
            f"{action_labels[self._validation_mode]} validation"
        )
        self._update_validation_controls()

    def _validation_paths_complete(self) -> bool:
        dataset_ready = bool(self.validation_dataset_edit.text().strip())
        manifest_ready = bool(self.validation_manifest_edit.text().strip())
        if self._validation_mode is DatasetValidationMode.DATASET:
            return dataset_ready
        if self._validation_mode is DatasetValidationMode.MANIFEST:
            return manifest_ready
        return dataset_ready and manifest_ready

    def _validation_empty_status(self) -> str:
        if self._validation_paths_complete():
            return "Ready to validate. No files or settings have been changed."
        return "Choose the required path(s) to begin validation."

    def _validation_path_changed(self, _text: str) -> None:
        if self._validation_busy:
            return
        self._clear_validation_result()

    def _clear_validation_result(self) -> None:
        self._validation_result = None
        self._validation_issue_model.clear()
        self.validation_summary_label.setText("No validation result.")
        self.validation_metadata_label.setText("Metadata: Not reported")
        self.validation_issue_summary_label.setText("Issues: None")
        self._set_validation_open_actions(None, None)
        if not self._validation_busy:
            self._validation_state = (
                DatasetValidationGuiState.READY
                if self._validation_paths_complete()
                else DatasetValidationGuiState.EMPTY
            )
            self.validation_status_label.setText(self._validation_empty_status())
            self._update_validation_controls()

    def _clear_validation_inputs(self, _checked: bool = False) -> None:
        if self._validation_busy:
            return
        if self._validation_mode in {DatasetValidationMode.DATASET, DatasetValidationMode.PAIR}:
            self.validation_dataset_edit.clear()
        if self._validation_mode in {DatasetValidationMode.MANIFEST, DatasetValidationMode.PAIR}:
            self.validation_manifest_edit.clear()
        self._clear_validation_result()

    def _browse_validation_dataset(self, _checked: bool = False) -> None:
        if self._validation_busy:
            return
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose JSONL dataset",
            str(self._validation_browse_directory()),
            "JSONL datasets (*.jsonl);;All files (*)",
        )
        if selected:
            self.validation_dataset_edit.setText(selected)

    def _browse_validation_manifest(self, _checked: bool = False) -> None:
        if self._validation_busy:
            return
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose dataset manifest",
            str(self._validation_browse_directory(prefer_dataset=True)),
            "JSON manifests (*.json);;All files (*)",
        )
        if selected:
            self.validation_manifest_edit.setText(selected)

    def _validation_browse_directory(self, prefer_dataset: bool = False) -> Path:
        if prefer_dataset:
            raw_dataset = self.validation_dataset_edit.text().strip()
            if raw_dataset:
                try:
                    dataset_parent = Path(raw_dataset).expanduser().parent
                    if dataset_parent.exists() and dataset_parent.is_dir():
                        return dataset_parent
                except (OSError, RuntimeError, ValueError):
                    pass
        if self._initial_directory is not None:
            return self._initial_directory
        return Path()

    def _use_adjacent_manifest(self, _checked: bool = False) -> None:
        if self._validation_busy:
            return
        raw_dataset = self.validation_dataset_edit.text().strip()
        if not raw_dataset:
            self.validation_status_label.setText("Choose a dataset JSONL file first.")
            return
        try:
            dataset_path = Path(raw_dataset)
            manifest_path = dataset_path.with_suffix(dataset_path.suffix + ".manifest.json")
        except (RuntimeError, ValueError):
            self.validation_status_label.setText("The dataset path is not valid.")
            return
        self.validation_manifest_edit.setText(str(manifest_path))

    def validate_current(self, _checked: bool = False) -> None:
        """Dispatch the selected mode to one narrow validation handler."""

        if self._validation_mode is DatasetValidationMode.DATASET:
            self.validate_dataset()
        elif self._validation_mode is DatasetValidationMode.MANIFEST:
            self.validate_manifest()
        else:
            self.verify_pair()

    def validate_dataset(self, _checked: bool = False) -> None:
        self._run_validation(DatasetValidationMode.DATASET)

    def validate_manifest(self, _checked: bool = False) -> None:
        self._run_validation(DatasetValidationMode.MANIFEST)

    def verify_pair(self, _checked: bool = False) -> None:
        self._run_validation(DatasetValidationMode.PAIR)

    def _run_validation(self, mode: DatasetValidationMode) -> None:
        if self._validation_busy:
            return
        if mode is not self._validation_mode:
            return
        dataset_text = self.validation_dataset_edit.text().strip()
        manifest_text = self.validation_manifest_edit.text().strip()
        if not self._validation_paths_complete():
            self._clear_validation_result()
            return

        self._clear_validation_result()
        self._validation_busy = True
        self._validation_state = DatasetValidationGuiState.VALIDATING
        self.validation_status_label.setText("Validating selected input…")
        self._update_validation_controls()
        try:
            if mode is DatasetValidationMode.DATASET:
                result: DatasetFileValidation | ManifestValidation | DatasetManifestPairValidation = (
                    self.context.dataset_builder.validate_dataset(Path(dataset_text))
                )
            elif mode is DatasetValidationMode.MANIFEST:
                result = self.context.dataset_builder.validate_manifest(Path(manifest_text))
            else:
                result = self.context.dataset_builder.verify_dataset_manifest_pair(
                    Path(dataset_text), Path(manifest_text)
                )
            self._validation_result = result
            self._validation_state = self._validation_gui_state(result)
            self._render_validation_result(result)
        except Exception:
            self.context.logger.exception("Dataset validation failed unexpectedly")
            self._validation_result = None
            self._validation_state = DatasetValidationGuiState.RECOVERABLE_FAILURE
            self.validation_status_label.setText(
                "Validation could not be completed. Correct the selected paths and try again."
            )
            self.validation_summary_label.setText(
                f"Mode: {self._validation_mode_label()}\nNo result was returned."
            )
            self.validation_metadata_label.setText("Metadata: Not reported")
            self.validation_issue_summary_label.setText("Issues: None")
            self._set_validation_open_actions(None, None)
        finally:
            self._validation_busy = False
            self._update_validation_controls()

    @staticmethod
    def _validation_gui_state(
        result: DatasetFileValidation | ManifestValidation | DatasetManifestPairValidation,
    ) -> DatasetValidationGuiState:
        if isinstance(result, DatasetFileValidation):
            if result.state is EngineDatasetValidationState.VALID:
                return DatasetValidationGuiState.VALID
            if result.state is EngineDatasetValidationState.UNREADABLE:
                return DatasetValidationGuiState.RECOVERABLE_FAILURE
            return DatasetValidationGuiState.INVALID
        if isinstance(result, ManifestValidation):
            if result.state is EngineManifestValidationState.VALID:
                return DatasetValidationGuiState.VALID
            if result.state in {
                EngineManifestValidationState.MISSING,
                EngineManifestValidationState.UNREADABLE,
            }:
                return DatasetValidationGuiState.RECOVERABLE_FAILURE
            return DatasetValidationGuiState.INVALID
        if result.state is EnginePairValidationState.VALID:
            return DatasetValidationGuiState.VALID
        if result.state is EnginePairValidationState.UNREADABLE:
            return DatasetValidationGuiState.RECOVERABLE_FAILURE
        return DatasetValidationGuiState.INVALID

    def _render_validation_result(
        self,
        result: DatasetFileValidation | ManifestValidation | DatasetManifestPairValidation,
    ) -> None:
        if isinstance(result, DatasetFileValidation):
            self._render_dataset_validation(result)
        elif isinstance(result, ManifestValidation):
            self._render_manifest_validation(result)
        else:
            self._render_pair_validation(result)

        self._validation_issue_model.set_issues(self._validation_issues(result))
        self._set_validation_open_actions(
            self._dataset_path_from_result(result),
            self._manifest_path_from_result(result),
        )

    @staticmethod
    def _validation_issues(
        result: DatasetFileValidation | ManifestValidation | DatasetManifestPairValidation,
    ) -> tuple[ValidationIssue, ...]:
        if isinstance(result, DatasetManifestPairValidation):
            return (
                *result.dataset_result.issues,
                *result.manifest_result.issues,
                *result.issues,
            )
        return result.issues

    def _render_dataset_validation(self, result: DatasetFileValidation) -> None:
        headline = "Valid empty dataset" if result.is_valid and result.record_count == 0 else (
            "Valid dataset" if result.is_valid else
            "Dataset could not be read" if result.state is EngineDatasetValidationState.UNREADABLE else
            "Invalid dataset"
        )
        self.validation_status_label.setText(headline)
        self.validation_summary_label.setText(
            "\n".join(
                (
                    f"Mode: {self._validation_mode_label()}",
                    f"State: {result.state.value}",
                    f"Path: {result.path}",
                    f"Shape: {result.shape_summary}",
                    f"Valid records: {result.record_count}",
                    f"Nonblank physical lines: {result.nonblank_line_count}",
                    f"Blank lines accepted: {result.blank_line_count}",
                )
            )
        )
        self.validation_metadata_label.setText("Metadata: Not reported")
        self.validation_issue_summary_label.setText(self._issue_summary(result.issues, result.issues_truncated))

    def _render_manifest_validation(self, result: ManifestValidation) -> None:
        if result.is_valid:
            headline = "Valid manifest"
        elif result.state is EngineManifestValidationState.MISSING:
            headline = "Manifest not found"
        elif result.state is EngineManifestValidationState.UNREADABLE:
            headline = "Manifest could not be read"
        else:
            headline = "Invalid manifest"
        self.validation_status_label.setText(headline)
        self.validation_summary_label.setText(
            "\n".join(
                (
                    f"Mode: {self._validation_mode_label()}",
                    f"State: {result.state.value}",
                    f"Path: {result.path}",
                    f"Dataset filename: {self._metadata_field(result.metadata, 'dataset_filename')}",
                    f"Declared record count: {self._validation_display_value(result.declared_record_count)}",
                    f"Declared SHA-256: {self._validation_display_value(result.declared_sha256)}",
                    f"Schema version: {self._validation_display_value(result.schema_version)}",
                    f"Format version: {self._validation_display_value(result.format_version)}",
                    f"Created at: {self._validation_display_value(result.created_at)}",
                )
            )
        )
        self.validation_metadata_label.setText(self._metadata_summary(result.metadata))
        self.validation_issue_summary_label.setText(self._issue_summary(result.issues, result.issues_truncated))

    def _render_pair_validation(self, result: DatasetManifestPairValidation) -> None:
        dataset_valid = result.dataset_result.state is EngineDatasetValidationState.VALID
        manifest_valid = result.manifest_result.state is EngineManifestValidationState.VALID
        if result.is_valid:
            headline = "Valid dataset/manifest pair"
        elif not dataset_valid and not manifest_valid:
            headline = "Dataset and manifest are invalid"
        elif not dataset_valid:
            dataset_headline = (
                "Dataset could not be read"
                if result.dataset_result.state is EngineDatasetValidationState.UNREADABLE
                else "Dataset invalid"
            )
            headline = f"{dataset_headline}; manifest valid"
        elif not manifest_valid:
            manifest_headline = (
                "Manifest not found"
                if result.manifest_result.state is EngineManifestValidationState.MISSING
                else "Manifest could not be read"
                if result.manifest_result.state is EngineManifestValidationState.UNREADABLE
                else "Manifest invalid"
            )
            headline = f"Dataset valid; {manifest_headline.lower()}"
        elif result.record_count_matches is False and result.sha256_matches is False:
            headline = "Files do not match: record count and SHA-256 differ"
        elif result.record_count_matches is False:
            headline = "Files do not match: record count differs"
        elif result.sha256_matches is False:
            headline = "Files do not match: SHA-256 differs"
        else:
            headline = "Pair verification did not succeed"
        self.validation_status_label.setText(headline)
        self.validation_summary_label.setText(
            "\n".join(
                (
                    f"Mode: {self._validation_mode_label()}",
                    f"Overall state: {result.state.value}",
                    f"Dataset path: {result.jsonl_path}",
                    f"Manifest path: {result.manifest_path}",
                    f"Dataset state: {result.dataset_result.state.value}",
                    f"Manifest state: {result.manifest_result.state.value}",
                    f"Valid records: {result.dataset_result.record_count}",
                    f"Nonblank physical lines: {result.dataset_result.nonblank_line_count}",
                    f"Pair comparison actual count: {self._validation_display_value(result.actual_record_count)}",
                    f"Manifest declared count: {self._validation_display_value(result.declared_record_count)}",
                    f"Record-count comparison: {self._match_display(result.record_count_matches)}",
                    f"Actual dataset SHA-256: {self._validation_display_value(result.actual_sha256)}",
                    f"Declared manifest SHA-256: {self._validation_display_value(result.declared_sha256)}",
                    f"SHA-256 comparison: {self._match_display(result.sha256_matches)}",
                    f"Schema/version compatibility: {self._compatibility_display(result.schema_version_compatible)}",
                )
            )
        )
        self.validation_metadata_label.setText(self._metadata_summary(result.manifest_result.metadata))
        issue_counts = (
            f"Dataset issues: {len(result.dataset_result.issues)}; "
            f"Manifest issues: {len(result.manifest_result.issues)}; "
            f"Pair issues: {len(result.issues)}"
        )
        notices = []
        if result.dataset_result.issues_truncated:
            notices.append("dataset issues truncated")
        if result.manifest_result.issues_truncated:
            notices.append("manifest issues truncated")
        if result.issues_truncated:
            notices.append("pair issues truncated")
        if notices:
            issue_counts += "; Notices: " + ", ".join(notices)
        self.validation_issue_summary_label.setText(issue_counts)

    def _validation_mode_label(self) -> str:
        return self.validation_mode_combo.currentText()

    @staticmethod
    def _validation_display_value(value: object) -> str:
        if value is None or value == "":
            return "Not reported"
        return str(value)

    @classmethod
    def _metadata_field(cls, metadata: Mapping[str, object] | None, key: str) -> str:
        if metadata is None:
            return "Not reported"
        return cls._validation_display_value(metadata.get(key))

    @classmethod
    def _metadata_value(cls, value: object) -> str:
        if value is None or value == "":
            return "Not reported"
        if isinstance(value, Mapping):
            return "; ".join(f"{key}: {cls._metadata_value(item)}" for key, item in value.items())
        if isinstance(value, (tuple, list)):
            return ", ".join(cls._metadata_value(item) for item in value) or "Not reported"
        return str(value)

    @classmethod
    def _metadata_summary(cls, metadata: Mapping[str, object] | None) -> str:
        if not metadata:
            return "Metadata: Not reported"
        lines = ["Metadata:"]
        lines.extend(f"{key}: {cls._metadata_value(value)}" for key, value in metadata.items())
        return "\n".join(lines)

    @staticmethod
    def _match_display(value: bool | None) -> str:
        if value is True:
            return "Match"
        if value is False:
            return "Mismatch"
        return "Not reported"

    @staticmethod
    def _compatibility_display(value: bool | None) -> str:
        if value is True:
            return "Compatible"
        if value is False:
            return "Not compatible"
        return "Not reported"

    @staticmethod
    def _issue_summary(issues: tuple[ValidationIssue, ...], truncated: bool) -> str:
        summary = f"Issues: {len(issues)}"
        if truncated:
            summary += " (additional issues were truncated by the engine)"
        return summary

    @staticmethod
    def _dataset_path_from_result(
        result: DatasetFileValidation | ManifestValidation | DatasetManifestPairValidation,
    ) -> Path | None:
        if isinstance(result, DatasetFileValidation):
            return result.path
        if isinstance(result, DatasetManifestPairValidation):
            return result.jsonl_path
        return None

    @staticmethod
    def _manifest_path_from_result(
        result: DatasetFileValidation | ManifestValidation | DatasetManifestPairValidation,
    ) -> Path | None:
        if isinstance(result, ManifestValidation):
            return result.path
        if isinstance(result, DatasetManifestPairValidation):
            return result.manifest_path
        return None

    @staticmethod
    def _path_exists(path: Path | None) -> bool:
        if path is None:
            return False
        try:
            return path.exists()
        except (OSError, RuntimeError, ValueError):
            return False

    def _set_validation_open_actions(self, dataset: Path | None, manifest: Path | None) -> None:
        self._validation_open_dataset_path = dataset
        self._validation_open_manifest_path = manifest
        self.validation_open_dataset_button.setEnabled(self._path_exists(dataset))
        self.validation_open_manifest_button.setEnabled(self._path_exists(manifest))
        folder = None
        for path in (dataset, manifest):
            if path is not None:
                try:
                    candidate = path.parent
                    if candidate.exists() and candidate.is_dir():
                        folder = candidate
                        break
                except (OSError, RuntimeError, ValueError):
                    continue
        self._validation_open_folder_path = folder
        self.validation_open_folder_button.setEnabled(folder is not None)

    def _open_validation_path(self, path: Path | None, title: str, noun: str) -> None:
        if path is None or not self._path_exists(path):
            QMessageBox.warning(self, title, f"The {noun} is not available to open.")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, title, f"The {noun} could not be opened.")

    def _open_validation_dataset(self, _checked: bool = False) -> None:
        self._open_validation_path(
            self._validation_open_dataset_path,
            "Open Dataset",
            "dataset file",
        )

    def _open_validation_manifest(self, _checked: bool = False) -> None:
        self._open_validation_path(
            self._validation_open_manifest_path,
            "Open Manifest",
            "manifest file",
        )

    def _open_validation_folder(self, _checked: bool = False) -> None:
        self._open_validation_path(
            self._validation_open_folder_path,
            "Open Containing Folder",
            "containing folder",
        )

    def _update_validation_controls(self) -> None:
        busy = self._validation_busy
        self.validation_mode_combo.setEnabled(not busy)
        self.validation_dataset_browse_button.setEnabled(not busy)
        self.validation_manifest_browse_button.setEnabled(not busy)
        self.use_adjacent_manifest_button.setEnabled(not busy)
        self.validation_clear_button.setEnabled(not busy)
        self.validation_action_button.setEnabled(not busy and self._validation_paths_complete())

    @staticmethod
    def _level_combo(object_name: str) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName(object_name)
        combo.addItem("Any level", "")
        for level in LEVELS:
            combo.addItem(level, level)
        return combo

    @staticmethod
    def _date_edit(object_name: str) -> QDateEdit:
        edit = QDateEdit(QDate.currentDate())
        edit.setObjectName(object_name)
        edit.setCalendarPopup(True)
        edit.setEnabled(False)
        return edit

    def _redaction_check(self, label: str, object_name: str, checked: bool) -> QCheckBox:
        checkbox = QCheckBox(label)
        checkbox.setObjectName(object_name)
        checkbox.setChecked(checked)
        checkbox.stateChanged.connect(self._configuration_changed)
        return checkbox

    def _load_preferences(self) -> None:
        try:
            preferences = self.context.settings.get_dataset_builder_preferences()
        except Exception:
            self.context.logger.exception("Dataset Builder preferences could not be loaded")
            preferences = DatasetBuilderPreferences()
        self._initial_directory = self._first_valid_directory(
            preferences.last_dataset_directory,
            self.context.default_working_directory,
            self.context.paths.project_root,
            self._safe_cwd(),
        )
        if self._initial_directory is None:
            self.status_label.setText(
                "No valid dataset folder is available. Choose a valid destination manually."
            )
            self.browse_button.setEnabled(False)
            return
        self.destination_edit.setText(str(self._initial_directory / "dataset.jsonl"))

    @staticmethod
    def _safe_cwd() -> Path | None:
        try:
            return Path.cwd()
        except (OSError, RuntimeError):
            return None

    @staticmethod
    def _first_valid_directory(*candidates: Path | None) -> Path | None:
        seen: set[Path] = set()
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                path = candidate.expanduser().resolve(strict=False)
                if path in seen:
                    continue
                seen.add(path)
                if path.exists() and path.is_dir():
                    return path
            except (OSError, RuntimeError, ValueError):
                continue
        return None

    def _populate_catalog_choices(self) -> None:
        try:
            self._populate_combo(
                self.model_combo,
                [("Any model", "")]
                + [
                    (
                        f"{profile.name} ({profile.model_name})",
                        profile.model_name,
                    )
                    for profile in self.context.catalog.list_model_profiles()
                ],
                self._filters.model,
            )
            self._populate_combo(
                self.session_combo,
                [("Any session", None)]
                + [
                    (session.title, session.id)
                    for session in self.context.catalog.list_sessions()
                    if session.id is not None
                ],
                self._filters.session_id,
            )
            self._populate_combo(
                self.prompt_template_combo,
                [("Any prompt template", None)]
                + [
                    (f"{template.name} v{template.version}", template.id)
                    for template in self.context.catalog.list_prompt_templates()
                    if template.id is not None
                ],
                self._filters.prompt_template_id,
            )
            self._populate_combo(
                self.hardware_combo,
                [("Any hardware profile", None)]
                + [
                    (profile.name, profile.id)
                    for profile in self.context.catalog.list_hardware_profiles()
                    if profile.id is not None
                ],
                self._filters.hardware_profile_id,
            )
        except Exception:
            self.context.logger.exception("Dataset Builder catalog choices could not be loaded")
            self.status_label.setText(
                "Some catalog choices could not be loaded. Refresh the page after correcting the catalog data."
            )

    @staticmethod
    def _populate_combo(
        combo: QComboBox,
        values: Sequence[tuple[str, object]],
        current: object,
    ) -> None:
        combo.blockSignals(True)
        try:
            combo.clear()
            for label, data in values:
                combo.addItem(str(label), data)
            if current not in (None, "") and combo.findData(current) < 0:
                combo.addItem(f"Unavailable ({current})", current)
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            combo.blockSignals(False)

    def refresh_catalog_choices(self) -> None:
        """Refresh catalog-backed filters without constructing a second page."""

        if self._previewing or self._saving:
            return
        self._populate_catalog_choices()
        self._configuration_changed()

    def _read_and_store_configuration(self) -> bool:
        try:
            self._filters, self._redaction = self._read_configuration()
        except ValueError as error:
            self._configuration_error = str(error)
            self.configuration_error_label.setText(self._configuration_error)
            return False
        self._configuration_error = ""
        self.configuration_error_label.clear()
        return True

    def _read_configuration(self) -> tuple[DatasetFilters, RedactionConfig]:
        minimum_text = self.min_overall_edit.text().strip()
        minimum = None
        if minimum_text:
            try:
                minimum = float(minimum_text)
            except ValueError as error:
                raise ValueError("Minimum overall score must be a number or blank.") from error
            if not math.isfinite(minimum):
                raise ValueError("Minimum overall score must be finite.")

        date_from = self._selected_date(self.date_from_check, self.date_from_edit)
        date_to = self._selected_date(self.date_to_check, self.date_to_edit)
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError("The start date must be on or before the end date.")

        filters = DatasetFilters(
            min_overall=minimum,
            max_hallucination=self._combo_text(self.max_hallucination_combo),
            min_reliability=self._combo_text(self.min_reliability_combo),
            verdict=self.verdict_edit.text().strip(),
            benchmark_type=str(self.benchmark_type_combo.currentData() or ""),
            model=str(self.model_combo.currentData() or ""),
            session_id=self._combo_int(self.session_combo),
            prompt_template_id=self._combo_int(self.prompt_template_combo),
            hardware_profile_id=self._combo_int(self.hardware_combo),
            include_run_ids=self._read_run_ids(self.include_run_ids_edit, "Include run IDs"),
            exclude_run_ids=self._read_run_ids(self.exclude_run_ids_edit, "Exclude run IDs"),
            date_from=date_from,
            date_to=date_to,
            keep_source_duplicates=self.keep_duplicates_check.isChecked(),
            include_provenance=self.include_provenance_check.isChecked(),
        )
        redaction = RedactionConfig(
            literals=tuple(self.literal_list.item(index).text() for index in range(self.literal_list.count())),
            redact_paths=self.redact_paths_check.isChecked(),
            redact_usernames=self.redact_usernames_check.isChecked(),
            redact_email=self.redact_email_check.isChecked(),
            redact_hosts_ips=self.redact_hosts_check.isChecked(),
            regex_patterns=tuple(self.regex_list.item(index).text() for index in range(self.regex_list.count())),
        )
        return filters, redaction

    @staticmethod
    def _selected_date(check: QCheckBox, edit: QDateEdit) -> date | None:
        if not check.isChecked():
            return None
        value = edit.date()
        return date(value.year(), value.month(), value.day())

    @staticmethod
    def _combo_int(combo: QComboBox) -> int | None:
        value = combo.currentData()
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _combo_text(combo: QComboBox) -> str | None:
        value = combo.currentData()
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _read_run_ids(edit: QLineEdit, label: str) -> frozenset[int]:
        raw = edit.text().strip()
        if not raw:
            return frozenset()
        values: set[int] = set()
        for part in raw.split(","):
            value = part.strip()
            try:
                number = int(value)
            except ValueError as error:
                raise ValueError(f"{label} must contain comma-separated positive whole numbers.") from error
            if number <= 0:
                raise ValueError(f"{label} must contain comma-separated positive whole numbers.")
            values.add(number)
        return frozenset(values)

    def _configuration_changed(self, *_args: object) -> None:
        if self._initializing:
            return
        self._read_and_store_configuration()
        self._retryable_failure = False
        self._invalidate_preview()

    def _date_controls_changed(self, *_args: object) -> None:
        self.date_from_edit.setEnabled(self.date_from_check.isChecked())
        self.date_to_edit.setEnabled(self.date_to_check.isChecked())
        self._configuration_changed()

    def _reset_filters(self, _checked: bool = False) -> None:
        self.min_overall_edit.clear()
        self.max_hallucination_combo.setCurrentIndex(0)
        self.min_reliability_combo.setCurrentIndex(0)
        self.verdict_edit.clear()
        self.benchmark_type_combo.setCurrentIndex(0)
        self.model_combo.setCurrentIndex(0)
        self.session_combo.setCurrentIndex(0)
        self.prompt_template_combo.setCurrentIndex(0)
        self.hardware_combo.setCurrentIndex(0)
        self.date_from_check.setChecked(False)
        self.date_to_check.setChecked(False)
        self.include_run_ids_edit.clear()
        self.exclude_run_ids_edit.clear()
        self.keep_duplicates_check.setChecked(False)
        self.include_provenance_check.setChecked(True)

    def _reset_redaction(self, _checked: bool = False) -> None:
        self.redact_paths_check.setChecked(True)
        self.redact_usernames_check.setChecked(False)
        self.redact_email_check.setChecked(True)
        self.redact_hosts_check.setChecked(True)
        self.literal_list.clear()
        self.regex_list.clear()
        self._configuration_changed()

    def _rule_editor_changed(self) -> None:
        self._configuration_changed()

    def _add_rule(self, rule_list: QListWidget, editor: QLineEdit) -> None:
        value = editor.text()
        if not value:
            self.status_label.setText("Enter a rule before adding it.")
            return
        rule_list.addItem(value)
        editor.clear()
        self._rule_editor_changed()

    def _edit_rule(self, rule_list: QListWidget, editor: QLineEdit) -> None:
        item = rule_list.currentItem()
        if item is None:
            self.status_label.setText("Select a rule to edit it.")
            return
        value = editor.text()
        if not value:
            self.status_label.setText("Enter a rule before saving the edit.")
            return
        item.setText(value)
        editor.clear()
        self._rule_editor_changed()

    def _remove_rule(self, rule_list: QListWidget, editor: QLineEdit) -> None:
        row = rule_list.currentRow()
        if row < 0:
            self.status_label.setText("Select a rule to remove it.")
            return
        rule_list.takeItem(row)
        editor.clear()
        self._rule_editor_changed()

    @staticmethod
    def _load_selected_rule(rule_list: QListWidget, editor: QLineEdit) -> None:
        item = rule_list.currentItem()
        editor.setText(item.text() if item is not None else "")

    def _invalidate_preview(self) -> None:
        had_preview = self._preview is not None or self._preview_signature is not None
        self._preview = None
        self._preview_signature = None
        self._write_result = None
        self._success_jsonl_path = None
        self._success_manifest_path = None
        self.preview_model.clear()
        self.preview_summary.setText("No current preview.")
        self.exclusion_summary.setText("Exclusions: None")
        self.warning_summary.setText("Warnings: None")
        self.redaction_summary.setText("Redactions: None")
        self.filter_summary.setText("Active filters: not previewed")
        self.active_redaction_summary.setText("Active redaction: not previewed")
        self._hide_open_actions()
        self._state = DatasetBuilderState.STALE_PREVIEW if had_preview else DatasetBuilderState.CONFIGURE
        if self._configuration_error:
            self.status_label.setText(self._configuration_error)
        elif had_preview:
            self.status_label.setText("Configuration changed. Preview again before building.")
        elif not self._initializing:
            self.status_label.setText("Configure the dataset, then preview it.")
        self._update_action_state()

    def _configuration_signature(self) -> tuple[DatasetFilters, RedactionConfig]:
        return self._filters, self._redaction

    def _preview_is_current(self) -> bool:
        return (
            self._preview is not None
            and self._preview_signature == self._configuration_signature()
            and not self._configuration_error
        )

    def _destination_changed(self, _text: str) -> None:
        if self._initializing:
            return
        self._retryable_failure = False
        self._write_result = None
        self._hide_open_actions()
        self._update_destination_state()
        self._update_action_state()

    def _normalized_destination(self) -> Path | None:
        raw = self.destination_edit.text().strip()
        if not raw:
            return None
        try:
            path = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None
        if path.suffix.casefold() != ".jsonl":
            path = Path(f"{path}.jsonl")
        return path

    def _validate_destination(self) -> tuple[Path | None, str]:
        raw = self.destination_edit.text().strip()
        if not raw:
            return None, "Choose a JSONL destination before building."
        try:
            entered = Path(raw).expanduser().resolve(strict=False)
            if entered.exists() and entered.is_dir():
                return None, "The JSONL destination is an existing directory. Choose a file path."
            path = self._normalized_destination()
            if path is None:
                return None, "The JSONL destination is invalid."
            if path.exists() and path.is_dir():
                return None, "The JSONL destination is an existing directory. Choose a file path."
            if not path.parent.exists():
                return None, "The destination folder must already exist."
            if not path.parent.is_dir():
                return None, "The destination parent is not a directory."
            return path, ""
        except (OSError, RuntimeError, ValueError) as error:
            return None, f"The destination could not be inspected: {error}"

    def _update_destination_state(self) -> None:
        path, error = self._validate_destination()
        self._destination_error = error
        if path is None:
            self.manifest_path_label.setText("Manifest: choose a valid JSONL destination")
            self.destination_error_label.setText(error)
        else:
            manifest = path.with_suffix(path.suffix + ".manifest.json")
            self.manifest_path_label.setText(f"Manifest: {manifest}")
            self.destination_error_label.setText("")

    def browse_destination(self) -> None:
        if self._initial_directory is None:
            self.status_label.setText("No valid dataset folder is available for browsing.")
            return
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Choose JSONL dataset destination",
            str(self._initial_directory / "dataset.jsonl"),
            "JSONL datasets (*.jsonl);;All files (*)",
        )
        if selected:
            self.destination_edit.setText(selected)

    def preview(self) -> None:
        if self._previewing or self._saving:
            return
        if not self._read_and_store_configuration():
            self._invalidate_preview()
            return
        self._previewing = True
        self._state = DatasetBuilderState.PREVIEWING
        self._update_action_state()
        self.status_label.setText("Previewing current benchmark runs…")
        try:
            preview = self.context.dataset_builder.preview(
                filters=self._filters,
                redaction_config=self._redaction,
            )
        except re.error as error:
            self.context.logger.exception("Dataset Builder preview rejected a custom regex")
            self._preview = None
            self._preview_signature = None
            self._state = DatasetBuilderState.RECOVERABLE_FAILURE
            self._configuration_error = f"Invalid custom regex: {error}"
            self.configuration_error_label.setText(self._configuration_error)
            self.status_label.setText(self._configuration_error)
            self._previewing = False
            self._update_action_state()
            return
        except Exception:
            self.context.logger.exception("Dataset Builder preview failed")
            self._preview = None
            self._preview_signature = None
            self._state = DatasetBuilderState.RECOVERABLE_FAILURE
            self.status_label.setText(
                "The dataset preview could not be completed. Correct the configuration and try again."
            )
            self.configuration_error_label.setText(
                "The dataset preview could not be completed. Check the selected filters and source data."
            )
            self._previewing = False
            self._update_action_state()
            return
        finally:
            self._previewing = False

        self._preview = preview
        self._preview_signature = self._configuration_signature()
        self._write_result = None
        self._retryable_failure = False
        self._state = DatasetBuilderState.PREVIEW_READY
        self._configuration_error = ""
        self.configuration_error_label.clear()
        self._display_preview(preview)
        self.status_label.setText(
            "No eligible records are available." if not preview.records else "Preview ready. No files or settings were changed."
        )
        self._update_action_state()

    def _display_preview(self, preview: DatasetPreview) -> None:
        try:
            candidate_count = len(self.context.benchmarks.runs.list(include_deleted=True))
        except Exception:
            self.context.logger.exception("Dataset Builder candidate count failed")
            candidate_count = 0
        excluded_count = sum(preview.excluded.values())
        warning_count = sum(preview.warnings.values())
        self.preview_summary.setText(
            "\n".join(
                (
                    f"Total candidate runs: {candidate_count}",
                    f"Eligible records: {len(preview.records)}",
                    f"Excluded records: {excluded_count}",
                    f"Warnings: {warning_count}",
                    f"Source duplicates: {preview.source_duplicates}",
                    f"Fingerprint duplicates: {preview.fingerprint_duplicates}",
                    f"Near duplicates: {preview.near_duplicates}",
                    f"Post-redaction collisions: {preview.post_redaction_collisions}",
                    f"Total redactions: {preview.redactions}",
                )
            )
        )
        self.exclusion_summary.setText(self._count_summary("Exclusions", preview.excluded))
        self.warning_summary.setText(self._count_summary("Warnings", preview.warnings))
        self.redaction_summary.setText(self._count_summary("Redactions by rule", preview.redaction_counts))
        filter_summary, redaction_summary = self._configuration_summary(self._filters, self._redaction)
        self.filter_summary.setText(filter_summary)
        self.active_redaction_summary.setText(redaction_summary)
        self.preview_model.set_records(preview.records)

    @staticmethod
    def _count_summary(label: str, counts: dict[str, int]) -> str:
        if not counts:
            return f"{label}: None"
        values = "; ".join(f"{key}: {value}" for key, value in sorted(counts.items()))
        return f"{label}: {values}"

    @staticmethod
    def _display_value(value: object) -> str:
        if value in (None, "", frozenset()):
            return "Not set"
        if isinstance(value, frozenset):
            return ", ".join(str(item) for item in sorted(value))
        return str(value)

    def _configuration_summary(
        self,
        filters: DatasetFilters,
        redaction: RedactionConfig,
    ) -> tuple[str, str]:
        filter_values = (
            ("Minimum overall", filters.min_overall),
            ("Maximum hallucination", filters.max_hallucination),
            ("Minimum reliability", filters.min_reliability),
            ("Verdict", filters.verdict),
            ("Benchmark type", filters.benchmark_type),
            ("Model", filters.model),
            ("Session ID", filters.session_id),
            ("Prompt template ID", filters.prompt_template_id),
            ("Hardware profile ID", filters.hardware_profile_id),
            ("Date from", filters.date_from),
            ("Date to", filters.date_to),
            ("Include run IDs", filters.include_run_ids),
            ("Exclude run IDs", filters.exclude_run_ids),
        )
        active = [
            f"{label}: {self._display_value(value)}"
            for label, value in filter_values
            if value not in (None, "", frozenset())
        ]
        active.append("Exact duplicates: retain" if filters.keep_source_duplicates else "Exact duplicates: skip")
        active.append("Provenance: included" if filters.include_provenance else "Provenance: omitted")
        redaction_values: list[str] = []
        if redaction.redact_paths:
            redaction_values.append("paths")
        if redaction.redact_usernames:
            redaction_values.append("usernames")
        if redaction.redact_email:
            redaction_values.append("email addresses")
        if redaction.redact_hosts_ips:
            redaction_values.append("hosts/IPs")
        if redaction.literals:
            redaction_values.append(f"literal terms ({len(redaction.literals)})")
        if redaction.regex_patterns:
            redaction_values.append(f"custom regex ({len(redaction.regex_patterns)})")
        return (
            f"Active filters: {'; '.join(active)}",
            f"Active redaction: {', '.join(redaction_values) if redaction_values else 'None'}",
        )

    def _build_is_eligible(self) -> bool:
        return (
            self._preview_is_current()
            and self._preview is not None
            and bool(self._preview.records)
            and self._destination_error == ""
            and not self._previewing
            and not self._saving
            and self._state not in {DatasetBuilderState.SUCCESS, DatasetBuilderState.BUILDING}
            and (self._state is DatasetBuilderState.PREVIEW_READY or self._retryable_failure)
        )

    def _update_action_state(self) -> None:
        busy = self._previewing or self._saving
        self.preview_button.setEnabled(not busy and not self._configuration_error)
        self.build_button.setEnabled(self._build_is_eligible())
        self.browse_button.setEnabled(not busy and self._initial_directory is not None)
        self.reset_filters_button.setEnabled(not busy)
        self.reset_redaction_button.setEnabled(not busy)

    def _hide_open_actions(self) -> None:
        for button in (self.open_jsonl_button, self.open_manifest_button, self.open_folder_button):
            button.setVisible(False)

    def _show_open_actions(self) -> None:
        for button in (self.open_jsonl_button, self.open_manifest_button, self.open_folder_button):
            button.setVisible(True)

    def build(self) -> None:
        if not self._build_is_eligible():
            return
        destination, error = self._validate_destination()
        if destination is None:
            self._destination_error = error
            self.destination_error_label.setText(error)
            self._update_action_state()
            return
        manifest = destination.with_suffix(destination.suffix + ".manifest.json")
        filter_summary, redaction_summary = self._configuration_summary(self._filters, self._redaction)
        confirmation = "\n".join(
            (
                f"JSONL path: {destination}",
                f"Manifest path: {manifest}",
                f"Preview eligible record count: {len(self._preview.records) if self._preview else 0}",
                filter_summary,
                redaction_summary,
                f"Provenance: {'included' if self._filters.include_provenance else 'omitted'}",
                "\nWrite the staged JSONL dataset and manifest?",
            )
        )
        if not self._confirm_build_action(confirmation):
            self.status_label.setText("Build cancelled. No files or settings were changed.")
            return

        self._saving = True
        self._state = DatasetBuilderState.BUILDING
        self._write_result = None
        self._hide_open_actions()
        self._update_action_state()
        try:
            current_preview = self.context.dataset_builder.preview(
                filters=self._filters,
                redaction_config=self._redaction,
            )
            if current_preview != self._preview:
                self._invalidate_preview()
                self.status_label.setText(
                    "Source data changed since preview. Preview again before building. "
                    "No files or settings were changed."
                )
                return
            result = self.context.dataset_builder.write_dataset(
                destination,
                filters=self._filters,
                redaction_config=self._redaction,
                overwrite=False,
            )
            if result.status is DatasetWriteStatus.OVERWRITE_REQUIRED:
                if not self._confirm_overwrite_action(destination, result):
                    self._saving = False
                    self._state = DatasetBuilderState.PREVIEW_READY
                    self.status_label.setText("Overwrite declined. No files or settings were changed.")
                    self._update_action_state()
                    return
                result = self.context.dataset_builder.write_dataset(
                    destination,
                    filters=self._filters,
                    redaction_config=self._redaction,
                    overwrite=True,
                )
            self._write_result = result
            self._handle_write_result(result)
        except Exception:
            self.context.logger.exception("Dataset Builder build failed")
            self._preview_signature = None
            self._retryable_failure = False
            self._state = DatasetBuilderState.STALE_PREVIEW
            self.status_label.setText(
                "The dataset build failed. Preview the current source data again before retrying."
            )
            self.configuration_error_label.setText(
                "The dataset build could not be completed. Correct the destination or source data and preview again."
            )
        finally:
            self._saving = False
            self._update_action_state()

    def _confirm_build_action(self, details: str) -> bool:
        if self._confirm_build is not None:
            return bool(self._confirm_build(details))
        answer = QMessageBox.question(
            self,
            "Confirm dataset build",
            details,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _confirm_overwrite_action(self, destination: Path, result: DatasetWriteResult) -> bool:
        if self._confirm_overwrite is not None:
            return bool(self._confirm_overwrite(destination))
        detail = result.details or "An existing JSONL dataset or manifest was found."
        answer = QMessageBox.question(
            self,
            "Confirm overwrite",
            f"{detail}\n\nReplace the existing dataset and manifest?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _handle_write_result(self, result: DatasetWriteResult) -> None:
        if result.status is DatasetWriteStatus.SUCCESS:
            expected_count = len(self._preview.records) if self._preview is not None else None
            if expected_count is not None and result.record_count != expected_count:
                self._preview_signature = None
                self._retryable_failure = False
                self._state = DatasetBuilderState.STALE_PREVIEW
                self._success_jsonl_path = None
                self._success_manifest_path = None
                self._hide_open_actions()
                self.status_label.setText(
                    "Source data changed after preview; preview again before accepting this build."
                )
                self.preview_summary.setText(
                    "The source data changed between preview and build. "
                    f"Preview records: {expected_count}; built records: {result.record_count}. "
                    "The output paths are shown for recovery, but no dataset settings were changed.\n"
                    + self._write_result_details(result)
                )
                return
            self._state = DatasetBuilderState.SUCCESS
            self._retryable_failure = False
            self._success_jsonl_path = result.jsonl_path
            self._success_manifest_path = result.manifest_path
            self.status_label.setText("Dataset and manifest finalized successfully.")
            details = [
                f"JSONL: {result.jsonl_path}",
                f"Manifest: {result.manifest_path}",
                f"Record count: {result.record_count}",
            ]
            if result.sha256:
                details.append(f"SHA-256: {result.sha256}")
            filter_summary, redaction_summary = self._configuration_summary(self._filters, self._redaction)
            details.extend((filter_summary, redaction_summary))
            self.preview_summary.setText("\n".join(details))
            try:
                self.context.settings.set_dataset_builder_preferences(
                    DatasetBuilderPreferences(last_dataset_directory=result.jsonl_path.parent)
                )
            except Exception:
                self.context.logger.exception("Dataset succeeded but preferences could not be saved")
                self.status_label.setText(
                    "Dataset build succeeded, but the last dataset folder could not be remembered."
                )
            self._show_open_actions()
            return

        if result.status is DatasetWriteStatus.OVERWRITE_REQUIRED:
            self._state = DatasetBuilderState.PREVIEW_READY
            self._retryable_failure = False
            self.status_label.setText("An existing dataset or manifest requires explicit overwrite confirmation.")
            return

        if result.status in {
            DatasetWriteStatus.TEMP_WRITE_FAILED,
            DatasetWriteStatus.TEMP_CLEANUP_FAILED,
            DatasetWriteStatus.JSONL_FINALIZE_FAILED,
        }:
            self._state = DatasetBuilderState.RECOVERABLE_FAILURE
            self._retryable_failure = True
            self.status_label.setText(self._friendly_write_failure(result))
            self.preview_summary.setText(self._write_result_details(result))
            return

        self._preview_signature = None
        self._retryable_failure = False
        self._state = DatasetBuilderState.STALE_PREVIEW
        self.status_label.setText(self._friendly_write_failure(result))
        self.preview_summary.setText(self._write_result_details(result))

    @staticmethod
    def _friendly_write_failure(result: DatasetWriteResult) -> str:
        messages = {
            DatasetWriteStatus.VALIDATION_FAILED: "The staged dataset output failed validation.",
            DatasetWriteStatus.TEMP_WRITE_FAILED: "The temporary dataset output could not be written.",
            DatasetWriteStatus.TEMP_CLEANUP_FAILED: "The dataset build could not clean up temporary output.",
            DatasetWriteStatus.JSONL_FINALIZE_FAILED: "The JSONL file could not be finalized.",
            DatasetWriteStatus.PARTIAL_FINALIZATION: "The dataset build partially finalized and is not complete.",
        }
        return messages.get(result.status, result.message or "The dataset build did not complete.")

    @staticmethod
    def _write_result_details(result: DatasetWriteResult) -> str:
        lines = [
            DatasetBuilderView._friendly_write_failure(result),
            f"JSONL: {result.jsonl_path}",
            f"Manifest: {result.manifest_path}",
            f"JSONL finalized: {'Yes' if result.jsonl_finalized else 'No'}",
            f"Manifest finalized: {'Yes' if result.manifest_finalized else 'No'}",
        ]
        if result.message:
            lines.append(f"Message: {result.message}")
        if result.details:
            lines.append(f"Details: {result.details}")
        if result.remaining_temp_paths:
            lines.append(
                "Remaining temporary paths: "
                + ", ".join(str(path) for path in result.remaining_temp_paths)
            )
        return "\n".join(lines)

    def _open_path(self, path: Path | None, title: str) -> None:
        if path is None:
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, title, f"The dataset build succeeded, but {path} could not be opened.")

    def open_jsonl(self, _checked: bool = False) -> None:
        self._open_path(self._success_jsonl_path, "Open JSONL")

    def open_manifest(self, _checked: bool = False) -> None:
        self._open_path(self._success_manifest_path, "Open manifest")

    def open_folder(self, _checked: bool = False) -> None:
        folder = self._success_jsonl_path.parent if self._success_jsonl_path is not None else None
        self._open_path(folder, "Open containing folder")


__all__ = (
    "DatasetBuilderState",
    "DatasetBuilderView",
    "DatasetValidationGuiState",
    "DatasetValidationMode",
)
