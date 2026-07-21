"""Modal standard-export workflow for the BenchPup desktop shell."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

try:  # Support both ``python -m src.gui`` and test imports with ``src`` on PATH.
    from ...engine.html_reporting import AnalyticsSourceFamily, HtmlAnalyticsReportOptions
    from ...engine.reporting import BenchmarkReportFilters, ScoreboardReportFilters
    from ...engine.settings import ExportPreferences
    from ...engine.standard_exports import (
        DEFAULT_EXPORT_FILENAMES,
        EXPORT_LABELS,
        StandardExportKind,
        StandardExportPreview,
        StandardExportRequest,
        StandardExportStatus,
    )
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.html_reporting import AnalyticsSourceFamily, HtmlAnalyticsReportOptions  # type: ignore[no-redef]
    from engine.reporting import BenchmarkReportFilters, ScoreboardReportFilters  # type: ignore[no-redef]
    from engine.settings import ExportPreferences  # type: ignore[no-redef]
    from engine.standard_exports import (  # type: ignore[no-redef]
        DEFAULT_EXPORT_FILENAMES,
        EXPORT_LABELS,
        StandardExportKind,
        StandardExportPreview,
        StandardExportRequest,
        StandardExportStatus,
    )
from ..context import GuiApplicationContext


class StandardExportDialog(QDialog):
    """Configure, preview, and explicitly authorize one standard export."""

    export_succeeded = Signal(str)

    def __init__(
        self,
        context: GuiApplicationContext,
        parent: QWidget | None = None,
        *,
        confirm_overwrite: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self._confirm_overwrite = confirm_overwrite
        self._preview: StandardExportPreview | None = None
        self._saving = False
        self._initializing = True
        self._suggested_destination: Path | None = None
        self._initial_directory: Path | None = None
        self._last_success_path: Path | None = None
        self.setObjectName("standardExportDialog")
        self.setAccessibleName("Standard export dialog")
        self.setWindowTitle("Export")
        self.setModal(True)
        self.setMinimumSize(720, 560)
        self.resize(880, 700)

        self._build_ui()
        self._load_preferences()
        self._populate_choices()
        self._initializing = False
        self._refresh_format_controls()
        self._invalidate_preview()

    def _build_ui(self) -> None:
        title = QLabel("Reports & Exports")
        title.setObjectName("dialogTitle")
        description = QLabel(
            "Choose a supported export, review the current records, and confirm the destination before writing."
        )
        description.setObjectName("dialogDescription")
        description.setWordWrap(True)

        self.format_combo = QComboBox()
        self.format_combo.setObjectName("exportFormatCombo")
        self.format_combo.setAccessibleName("Export format")
        for kind in StandardExportKind:
            self.format_combo.addItem(EXPORT_LABELS[kind], kind.value)
        model = self.format_combo.model()
        if isinstance(model, QStandardItemModel):
            unavailable = model.item(self.format_combo.findData(StandardExportKind.JSONL_TRAINING_DATA.value))
            if unavailable is not None:
                unavailable.setEnabled(False)
        self.format_combo.currentIndexChanged.connect(self._format_changed)

        self.run_combo = QComboBox()
        self.run_combo.setObjectName("exportRunCombo")
        self.run_combo.setAccessibleName("Benchmark run scope")
        self.session_combo = QComboBox()
        self.session_combo.setObjectName("exportSessionCombo")
        self.session_combo.setAccessibleName("Session scope")
        self.hardware_combo = QComboBox()
        self.hardware_combo.setObjectName("exportHardwareCombo")
        self.hardware_combo.setAccessibleName("Hardware profile scope")
        self.model_filter_combo = QComboBox()
        self.model_filter_combo.setObjectName("exportModelFilterCombo")
        self.model_filter_combo.setAccessibleName("Benchmark model filter")
        self.batch_filter_combo = QComboBox()
        self.batch_filter_combo.setObjectName("exportBatchFilterCombo")
        self.batch_filter_combo.setAccessibleName("Scoreboard batch filter")
        for combo in (
            self.run_combo,
            self.session_combo,
            self.hardware_combo,
            self.model_filter_combo,
            self.batch_filter_combo,
        ):
            combo.currentIndexChanged.connect(self._controls_changed)

        self.selection_group = QGroupBox("Scope")
        selection_form = QFormLayout(self.selection_group)
        self._selection_form = selection_form
        selection_form.setContentsMargins(12, 10, 12, 10)
        selection_form.setHorizontalSpacing(14)
        selection_form.setVerticalSpacing(8)
        selection_form.addRow("Benchmark run", self.run_combo)
        selection_form.addRow("Session", self.session_combo)
        selection_form.addRow("Hardware profile", self.hardware_combo)
        selection_form.addRow("Model", self.model_filter_combo)
        selection_form.addRow("Scoreboard batch", self.batch_filter_combo)

        self.include_prompt_check = QCheckBox("Include prompt text")
        self.include_prompt_check.setObjectName("includePromptTextCheck")
        self.include_raw_check = QCheckBox("Include raw model output")
        self.include_raw_check.setObjectName("includeRawOutputCheck")
        self.include_attachments_check = QCheckBox("Include attachment metadata")
        self.include_attachments_check.setObjectName("includeAttachmentMetadataCheck")
        self.include_model_details_check = QCheckBox("Include model details")
        self.include_model_details_check.setObjectName("includeModelDetailsCheck")
        self.include_hardware_details_check = QCheckBox("Include hardware details")
        self.include_hardware_details_check.setObjectName("includeHardwareDetailsCheck")
        self.report_options_group = QGroupBox("Report options")
        report_options_layout = QVBoxLayout(self.report_options_group)
        for checkbox in (
            self.include_prompt_check,
            self.include_raw_check,
            self.include_attachments_check,
            self.include_model_details_check,
            self.include_hardware_details_check,
        ):
            checkbox.stateChanged.connect(self._controls_changed)
            report_options_layout.addWidget(checkbox)

        self.analytics_source_combo = QComboBox()
        self.analytics_source_combo.setObjectName("analyticsSourceCombo")
        self.analytics_source_combo.setAccessibleName("HTML Analytics source")
        self.analytics_source_combo.addItem("Benchmark runs", AnalyticsSourceFamily.BENCHMARK_RUNS.value)
        self.analytics_source_combo.addItem("Scoreboard", AnalyticsSourceFamily.SCOREBOARD.value)
        self.analytics_source_combo.addItem("Combined", AnalyticsSourceFamily.COMBINED.value)
        self.analytics_source_combo.currentIndexChanged.connect(self._controls_changed)
        self.analytics_tables_check = QCheckBox("Include detailed tables")
        self.analytics_tables_check.setObjectName("analyticsTablesCheck")
        self.analytics_tables_check.setChecked(True)
        self.analytics_trends_check = QCheckBox("Include trends")
        self.analytics_trends_check.setObjectName("analyticsTrendsCheck")
        self.analytics_trends_check.setChecked(True)
        self.analytics_compact_check = QCheckBox("Use compact layout")
        self.analytics_compact_check.setObjectName("analyticsCompactCheck")
        for checkbox in (
            self.analytics_tables_check,
            self.analytics_trends_check,
            self.analytics_compact_check,
        ):
            checkbox.stateChanged.connect(self._controls_changed)
        self.analytics_group = QGroupBox("HTML Analytics options")
        analytics_form = QFormLayout(self.analytics_group)
        analytics_form.addRow("Source", self.analytics_source_combo)
        analytics_form.addRow(self.analytics_tables_check)
        analytics_form.addRow(self.analytics_trends_check)
        analytics_form.addRow(self.analytics_compact_check)

        self.destination_edit = QLineEdit()
        self.destination_edit.setObjectName("exportDestinationEdit")
        self.destination_edit.setAccessibleName("Export destination")
        self.destination_edit.setPlaceholderText("Choose an existing folder and a filename")
        self.destination_edit.textChanged.connect(self._destination_changed)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.setObjectName("exportBrowseButton")
        self.browse_button.setAccessibleName("Browse for export destination")
        self.browse_button.clicked.connect(self.browse_destination)
        destination_row = QHBoxLayout()
        destination_row.addWidget(self.destination_edit, 1)
        destination_row.addWidget(self.browse_button)
        destination_group = QGroupBox("Destination")
        destination_form = QFormLayout(destination_group)
        destination_form.addRow("File", destination_row)

        self.preview_summary = QLabel("No preview yet.")
        self.preview_summary.setObjectName("exportPreviewSummary")
        self.preview_summary.setAccessibleName("Export preview summary")
        self.preview_summary.setWordWrap(True)
        self.status_label = QLabel("Configure an export, then preview it.")
        self.status_label.setObjectName("exportStatus")
        self.status_label.setWordWrap(True)
        self.result_label = QLabel()
        self.result_label.setObjectName("exportResult")
        self.result_label.setWordWrap(True)
        self.result_label.setVisible(False)

        self.preview_button = QPushButton("Preview")
        self.preview_button.setObjectName("exportPreviewButton")
        self.preview_button.setAccessibleName("Preview export")
        self.preview_button.clicked.connect(self.preview)
        self.export_button = QPushButton("Export")
        self.export_button.setObjectName("exportConfirmButton")
        self.export_button.setAccessibleName("Confirm and write export")
        self.export_button.clicked.connect(self.export)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("exportCancelButton")
        self.cancel_button.setAccessibleName("Cancel export")
        self.cancel_button.clicked.connect(self.reject)
        self.open_file_button = QPushButton("Open File")
        self.open_file_button.setObjectName("exportOpenFileButton")
        self.open_file_button.clicked.connect(self.open_file)
        self.open_folder_button = QPushButton("Open Containing Folder")
        self.open_folder_button.setObjectName("exportOpenFolderButton")
        self.open_folder_button.clicked.connect(self.open_folder)
        self.open_file_button.setVisible(False)
        self.open_folder_button.setVisible(False)

        actions = QHBoxLayout()
        actions.addWidget(self.status_label, 1)
        actions.addWidget(self.open_file_button)
        actions.addWidget(self.open_folder_button)
        actions.addWidget(self.preview_button)
        actions.addWidget(self.export_button)
        actions.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(description)
        format_form = QFormLayout()
        format_form.addRow("Export format", self.format_combo)
        layout.addLayout(format_form)
        layout.addWidget(self.selection_group)
        layout.addWidget(self.report_options_group)
        layout.addWidget(self.analytics_group)
        layout.addWidget(destination_group)
        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.addWidget(self.preview_summary)
        preview_layout.addWidget(self.result_label)
        layout.addWidget(preview_group)
        layout.addLayout(actions)

    def _load_preferences(self) -> None:
        try:
            preferences = self.context.settings.get_export_preferences()
        except Exception as error:
            self.context.logger.exception("Export preferences could not be loaded")
            preferences = ExportPreferences()
        self._initial_directory = self._first_valid_directory(
            preferences.last_export_directory,
            self.context.default_working_directory,
            self.context.paths.project_root,
            self._safe_cwd(),
        )
        kind_index = self.format_combo.findData(preferences.last_export_kind)
        if kind_index < 0:
            kind_index = self.format_combo.findData(StandardExportKind.BENCHMARK_RUNS_CSV.value)
        self.format_combo.blockSignals(True)
        self.format_combo.setCurrentIndex(max(kind_index, 0))
        self.format_combo.blockSignals(False)
        if self._initial_directory is None:
            self.status_label.setText("No valid export folder is available. Choose a valid folder to continue.")
            self.browse_button.setEnabled(False)
            self.destination_edit.clear()
            return
        self._set_suggested_destination(self._initial_directory / self._default_filename())

    @staticmethod
    def _safe_cwd() -> Path | None:
        try:
            return Path.cwd()
        except OSError:
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
            except (OSError, RuntimeError):
                continue
        return None

    def _kind(self) -> StandardExportKind:
        try:
            return StandardExportKind(str(self.format_combo.currentData()))
        except (TypeError, ValueError):
            return StandardExportKind.BENCHMARK_RUNS_CSV

    def _default_filename(self) -> str:
        return DEFAULT_EXPORT_FILENAMES.get(self._kind(), "export")

    def _set_suggested_destination(self, path: Path) -> None:
        self._suggested_destination = path
        self.destination_edit.blockSignals(True)
        self.destination_edit.setText(str(path))
        self.destination_edit.blockSignals(False)

    def _destination_is_suggested(self) -> bool:
        if self._suggested_destination is None:
            return False
        try:
            return Path(self.destination_edit.text()).expanduser().resolve(strict=False) == self._suggested_destination
        except (OSError, ValueError):
            return False

    def _format_changed(self, _index: int) -> None:
        if self._initializing:
            return
        was_suggested = self._destination_is_suggested()
        self._reset_format_options()
        if was_suggested and self._initial_directory is not None:
            self._set_suggested_destination(self._initial_directory / self._default_filename())
        self._refresh_format_controls()
        self._invalidate_preview()

    def _reset_format_options(self) -> None:
        for combo in (
            self.run_combo,
            self.session_combo,
            self.hardware_combo,
            self.model_filter_combo,
            self.batch_filter_combo,
        ):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        for checkbox in (
            self.include_prompt_check,
            self.include_raw_check,
            self.include_attachments_check,
            self.include_model_details_check,
            self.include_hardware_details_check,
        ):
            checkbox.blockSignals(True)
            checkbox.setChecked(False)
            checkbox.blockSignals(False)
        self.analytics_source_combo.blockSignals(True)
        self.analytics_source_combo.setCurrentIndex(0)
        self.analytics_source_combo.blockSignals(False)
        self.analytics_tables_check.blockSignals(True)
        self.analytics_tables_check.setChecked(True)
        self.analytics_tables_check.blockSignals(False)
        self.analytics_trends_check.blockSignals(True)
        self.analytics_trends_check.setChecked(True)
        self.analytics_trends_check.blockSignals(False)
        self.analytics_compact_check.blockSignals(True)
        self.analytics_compact_check.setChecked(False)
        self.analytics_compact_check.blockSignals(False)

    def _refresh_format_controls(self) -> None:
        kind = self._kind()
        run_scope = kind is StandardExportKind.BENCHMARK_RUN_MARKDOWN
        session_scope = kind is StandardExportKind.SESSION_MARKDOWN
        hardware_scope = kind is StandardExportKind.HARDWARE_MARKDOWN
        model_scope = kind in {
            StandardExportKind.BENCHMARK_RUNS_CSV,
            StandardExportKind.COMBINED_MARKDOWN,
            StandardExportKind.BENCHMARK_RUN_MARKDOWN,
            StandardExportKind.MODEL_LEADERBOARD_MARKDOWN,
            StandardExportKind.SESSION_MARKDOWN,
            StandardExportKind.HARDWARE_MARKDOWN,
            StandardExportKind.SCOREBOARD_CSV,
            StandardExportKind.SCOREBOARD_MARKDOWN,
        }
        batch_scope = kind in {
            StandardExportKind.SCOREBOARD_CSV,
            StandardExportKind.COMBINED_MARKDOWN,
            StandardExportKind.SCOREBOARD_MARKDOWN,
        }
        markdown_options = kind in {
            StandardExportKind.BENCHMARK_RUN_MARKDOWN,
            StandardExportKind.SESSION_MARKDOWN,
            StandardExportKind.HARDWARE_MARKDOWN,
        }
        self._set_form_row_visible(self.run_combo, run_scope)
        self._set_form_row_visible(self.session_combo, session_scope)
        self._set_form_row_visible(self.hardware_combo, hardware_scope)
        self._set_form_row_visible(self.model_filter_combo, model_scope)
        self._set_form_row_visible(self.batch_filter_combo, batch_scope)
        self.report_options_group.setVisible(markdown_options or kind is StandardExportKind.MODEL_LEADERBOARD_MARKDOWN)
        for checkbox in (
            self.include_prompt_check,
            self.include_raw_check,
            self.include_attachments_check,
        ):
            checkbox.setVisible(markdown_options)
        self.include_model_details_check.setVisible(kind is StandardExportKind.MODEL_LEADERBOARD_MARKDOWN)
        self.include_hardware_details_check.setVisible(kind is StandardExportKind.HARDWARE_MARKDOWN)
        self.analytics_group.setVisible(kind is StandardExportKind.HTML_ANALYTICS)
        self.selection_group.setVisible(kind is not StandardExportKind.HTML_ANALYTICS)

    def _set_form_row_visible(self, widget: QWidget, visible: bool) -> None:
        label = self._selection_form.labelForField(widget)
        if label is not None:
            label.setVisible(visible)
        widget.setVisible(visible)

    def _controls_changed(self, *_args: object) -> None:
        if not self._initializing:
            self._invalidate_preview()

    def _destination_changed(self, _text: str) -> None:
        if self._initializing:
            return
        if not self._destination_is_suggested():
            self._suggested_destination = None
        self._invalidate_preview()

    def _invalidate_preview(self) -> None:
        self._preview = None
        self.preview_summary.setText("No preview yet.")
        self.result_label.clear()
        self.result_label.setVisible(False)
        self.open_file_button.setVisible(False)
        self.open_folder_button.setVisible(False)
        self.export_button.setEnabled(False)
        if not self._initializing:
            self.status_label.setText("Configuration changed. Preview again before exporting.")

    def _populate_choices(self) -> None:
        aggregates = tuple(self.context.reporting.select_benchmark_runs())
        self._populate_combo(
            self.run_combo,
            [("All active benchmark runs", None)]
            + [
                (
                    f"#{aggregate.run.id}: {aggregate.run.model_snapshot.get('model_name', 'Unknown model')} / "
                    f"{aggregate.run.benchmark_snapshot.get('name', aggregate.run.benchmark_snapshot.get('benchmark_file', 'benchmark'))}",
                    aggregate.run.id,
                )
                for aggregate in aggregates
                if aggregate.run.id is not None
            ],
        )
        scoreboard = tuple(self.context.reporting.select_scoreboard_entries())
        model_values = {
            str(aggregate.run.model_snapshot.get("model_name", "")).strip()
            for aggregate in aggregates
            if str(aggregate.run.model_snapshot.get("model_name", "")).strip()
        }
        model_values.update(
            str(aggregate.entry.model_name).strip()
            for aggregate in scoreboard
            if str(aggregate.entry.model_name).strip()
        )
        self._populate_combo(
            self.model_filter_combo,
            [("All models", "")]
            + [(value, value) for value in sorted(model_values, key=lambda value: (value.casefold(), value))],
        )
        sessions = self.context.catalog.list_sessions()
        self._populate_combo(
            self.session_combo,
            [("Select a session", None)]
            + [(session.title, session.id) for session in sessions if session.id is not None],
        )
        hardware = self.context.catalog.list_hardware_profiles()
        self._populate_combo(
            self.hardware_combo,
            [("Select a hardware profile", None)]
            + [(profile.name, profile.id) for profile in hardware if profile.id is not None],
        )
        batches: dict[int, str] = {}
        for aggregate in scoreboard:
            if aggregate.entry.import_batch_id is not None:
                batches[aggregate.entry.import_batch_id] = aggregate.batch.name if aggregate.batch else f"Batch {aggregate.entry.import_batch_id}"
        self._populate_combo(
            self.batch_filter_combo,
            [("All scoreboard batches", None)]
            + [
                (name, batch_id)
                for batch_id, name in sorted(batches.items(), key=lambda item: (item[1].casefold(), item[1]))
            ],
        )

    @staticmethod
    def _populate_combo(combo: QComboBox, values: Sequence[tuple[str, object]]) -> None:
        combo.blockSignals(True)
        combo.clear()
        for label, data in values:
            combo.addItem(str(label), data)
        combo.blockSignals(False)

    def _request(self) -> StandardExportRequest:
        kind = self._kind()
        selected_model = str(self.model_filter_combo.currentData() or "")
        batch_id = self.batch_filter_combo.currentData()
        benchmark_kinds = {
            StandardExportKind.BENCHMARK_RUNS_CSV,
            StandardExportKind.COMBINED_MARKDOWN,
            StandardExportKind.BENCHMARK_RUN_MARKDOWN,
            StandardExportKind.MODEL_LEADERBOARD_MARKDOWN,
            StandardExportKind.SESSION_MARKDOWN,
            StandardExportKind.HARDWARE_MARKDOWN,
        }
        scoreboard_kinds = {
            StandardExportKind.SCOREBOARD_CSV,
            StandardExportKind.COMBINED_MARKDOWN,
            StandardExportKind.SCOREBOARD_MARKDOWN,
        }
        benchmark_filters = BenchmarkReportFilters(model=selected_model if kind in benchmark_kinds else "")
        scoreboard_filters = ScoreboardReportFilters(
            model=selected_model if kind in scoreboard_kinds else "",
            batch_id=int(batch_id) if isinstance(batch_id, int) else None,
        )
        analytics_options = HtmlAnalyticsReportOptions(
            source_family=str(self.analytics_source_combo.currentData()),
            include_detailed_tables=self.analytics_tables_check.isChecked(),
            include_trends=self.analytics_trends_check.isChecked(),
            compact_layout=self.analytics_compact_check.isChecked(),
        )
        return StandardExportRequest(
            kind=kind,
            destination=self.destination_edit.text().strip(),
            benchmark_filters=benchmark_filters,
            scoreboard_filters=scoreboard_filters,
            run_id=self.run_combo.currentData() if kind is StandardExportKind.BENCHMARK_RUN_MARKDOWN else None,
            session_id=self.session_combo.currentData() if kind is StandardExportKind.SESSION_MARKDOWN else None,
            hardware_profile_id=self.hardware_combo.currentData() if kind is StandardExportKind.HARDWARE_MARKDOWN else None,
            include_prompt_text=self.include_prompt_check.isChecked(),
            include_raw_model_output=self.include_raw_check.isChecked(),
            include_attachment_metadata=self.include_attachments_check.isChecked(),
            include_model_details=self.include_model_details_check.isChecked(),
            include_hardware_details=self.include_hardware_details_check.isChecked(),
            analytics_options=analytics_options,
        )

    def preview(self) -> None:
        if self._saving:
            return
        try:
            request = self._request()
            preview = self.context.standard_exports.preview(request)
        except Exception:
            self.context.logger.exception("Standard export preview failed")
            self._preview = None
            self.export_button.setEnabled(False)
            self.status_label.setText("The export could not be previewed. Correct the configuration and try again.")
            return
        self._preview = preview
        self.preview_summary.setText("No records selected." if preview.empty_selection else (preview.summary or preview.message))
        self.status_label.setText(preview.message)
        self.result_label.clear()
        self.result_label.setVisible(False)
        self.export_button.setEnabled(preview.status is StandardExportStatus.SUCCESS and not preview.empty_selection)

    def browse_destination(self) -> None:
        if self._initial_directory is None:
            self.status_label.setText("No valid export folder is available. Choose a valid folder to continue.")
            return
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Choose export destination",
            str(self._initial_directory / self._default_filename()),
            f"{EXPORT_LABELS[self._kind()]} (*{self.context.standard_exports.expected_extension(self._kind())})",
        )
        if selected:
            self._suggested_destination = None
            self.destination_edit.setText(selected)

    def export(self) -> None:
        if self._saving or self._preview is None or self._preview.status is not StandardExportStatus.SUCCESS:
            return
        self._saving = True
        try:
            try:
                request = self._request()
                result = self.context.standard_exports.write(request, overwrite=False)
            except Exception:
                self.context.logger.exception("Standard export write failed")
                self.export_button.setEnabled(False)
                self.status_label.setText("The export could not be completed. Preview again and retry.")
                return
            if result.status is StandardExportStatus.OVERWRITE_REQUIRED:
                if not self._ask_overwrite(result.destination):
                    self.status_label.setText("Overwrite declined. No file or settings were changed.")
                    return
                result = self.context.standard_exports.write(request, overwrite=True)
            if result.succeeded:
                self._handle_success(request, result.destination)
            else:
                self.export_button.setEnabled(False)
                self.status_label.setText(result.message or "Export could not be completed. Preview again and retry.")
                self.preview_summary.setText(result.details or result.message)
        finally:
            self._saving = False

    def _ask_overwrite(self, path: Path) -> bool:
        if self._confirm_overwrite is not None:
            return bool(self._confirm_overwrite())
        answer = QMessageBox.question(
            self,
            "Confirm overwrite",
            f"The file already exists:\n{path}\n\nReplace it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _handle_success(self, request: StandardExportRequest, path: Path) -> None:
        self._last_success_path = path
        self.export_button.setEnabled(False)
        self.open_file_button.setVisible(True)
        self.open_folder_button.setVisible(True)
        self.result_label.setVisible(True)
        self.result_label.setText(f"Export completed successfully:\n{path}")
        self.status_label.setText("Export complete.")
        try:
            self.context.settings.set_export_preferences(
                ExportPreferences(last_export_directory=path.parent, last_export_kind=self._kind().value)
            )
        except Exception:
            self.context.logger.exception("Export succeeded but preferences could not be saved")
            self.result_label.setText(f"Export completed, but the last export preference could not be saved:\n{path}")
        self.export_succeeded.emit(str(path))

    def open_file(self) -> None:
        if self._last_success_path is None:
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_success_path))):
            QMessageBox.warning(self, "Open file", "The export succeeded, but the file could not be opened.")

    def open_folder(self) -> None:
        if self._last_success_path is None:
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_success_path.parent))):
            QMessageBox.warning(self, "Open containing folder", "The export succeeded, but the containing folder could not be opened.")


__all__ = ("StandardExportDialog",)
