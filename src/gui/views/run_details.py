"""Read-only Run Details dialog backed by a complete engine aggregate."""

from __future__ import annotations

import json
import math
from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QMessageBox,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..context import GuiApplicationContext
from ..dialogs.attachment_editor import AttachmentEditorDialog
from ..dialogs.review_editor import ReviewEditorDialog
from ..models.runs import NOT_RECORDED, UNAVAILABLE, snapshot_label

try:
    from ...engine.domain import RunAttachment
    from ...engine.reporting import BenchmarkRunAggregate
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.domain import RunAttachment  # type: ignore[no-redef]
    from engine.reporting import BenchmarkRunAggregate  # type: ignore[no-redef]


def _display(value: Any, *, missing: str = NOT_RECORDED) -> str:
    if value is None or value == "":
        return missing
    return str(value)


def _format_score(value: float | None) -> str:
    return NOT_RECORDED if value is None else f"{value:.2f} / 5"


def _format_speed(value: Any) -> str:
    if value is None or value == "" or isinstance(value, bool):
        return UNAVAILABLE
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return UNAVAILABLE
        return f"{float(value):.1f} tok/s"
    return str(value)


def _format_bool(value: Any) -> str:
    if isinstance(value, bool):
        return "Enabled" if value else "Disabled"
    if isinstance(value, int) and value in (0, 1):
        return "Enabled" if value == 1 else "Disabled"
    return NOT_RECORDED


def _safe_snapshot(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    return {"legacy_value": value}


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return json.dumps({"unavailable": str(value)}, ensure_ascii=False, indent=2)


def _is_file(path_value: str) -> bool:
    if not path_value.strip():
        return False
    try:
        return Path(path_value).is_file()
    except (OSError, ValueError):
        return False


def _is_directory(path_value: str) -> bool:
    if not path_value.strip():
        return False
    try:
        return Path(path_value).is_dir()
    except (OSError, ValueError):
        return False


class RunDetailsDialog(QDialog):
    """Complete run aggregate presented without mutation controls."""

    review_saved = Signal(int)

    def __init__(
        self,
        context: GuiApplicationContext,
        run_id: int,
        parent: QWidget | None = None,
        *,
        aggregate: BenchmarkRunAggregate | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.run_id = run_id
        self.aggregate = aggregate or self.load_aggregate(context, run_id)
        self.setObjectName("runDetailsDialog")
        self.setWindowTitle(f"Run #{run_id} Details")
        self.setModal(True)
        self.setMinimumSize(760, 620)
        self.resize(960, 760)
        self._build_ui()

    @staticmethod
    def load_aggregate(context: GuiApplicationContext, run_id: int) -> BenchmarkRunAggregate:
        """Reload the complete aggregate through the existing service boundary."""

        run, _, _ = context.benchmarks.get_run(run_id)
        if run is None:
            raise LookupError(f"Run #{run_id} was not found.")
        aggregates = context.reporting.select_benchmark_runs([run])
        if not aggregates:
            raise LookupError(f"Run #{run_id} is not available for ordinary browsing.")
        return aggregates[0]

    def _build_ui(self) -> None:
        self.tabs = self._build_tabs()

        actions = QHBoxLayout()
        actions.addWidget(QLabel(f"Run #{self.run_id} evaluation"))
        actions.addStretch(1)
        self.edit_review_button = QPushButton("Edit Review")
        self.edit_review_button.setObjectName("primaryButton")
        self.edit_review_button.setAccessibleName("Edit Review")
        self.edit_review_button.setToolTip("Create or update the ReviewScore without changing run snapshots")
        self.edit_review_button.clicked.connect(self.edit_review)
        actions.addWidget(self.edit_review_button)

        self.close_button = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_button.setAccessibleName("Close run details")
        self.close_button.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addLayout(actions)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.close_button)

    def _build_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setObjectName("runDetailsTabs")
        tabs.setAccessibleName("Run details sections")
        tabs.addTab(self._summary_tab(), "Summary")
        tabs.addTab(self._prompt_output_tab(), "Prompt and Output")
        tabs.addTab(self._evaluation_tab(), "Evaluation")
        tabs.addTab(self._attachments_tab(), "Attachments")
        tabs.addTab(self._snapshots_tab(), "Historical Snapshots")
        tabs.addTab(self._metadata_tab(), "Metadata")
        return tabs

    @staticmethod
    def _scroll(widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        return scroll

    @staticmethod
    def _line_value(value: Any, *, missing: str = NOT_RECORDED) -> QLineEdit:
        field = QLineEdit(_display(value, missing=missing))
        field.setReadOnly(True)
        field.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        field.setAccessibleName("Read-only value")
        return field

    @staticmethod
    def _text_value(value: Any, *, minimum_height: int = 110) -> QPlainTextEdit:
        field = QPlainTextEdit("" if value is None else str(value))
        field.setReadOnly(True)
        field.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        field.setMinimumHeight(minimum_height)
        field.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        field.setAccessibleName("Read-only text")
        return field

    def _form(self) -> QFormLayout:
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        return form

    def _summary_tab(self) -> QWidget:
        run = self.aggregate.run
        model = run.model_snapshot if isinstance(run.model_snapshot, dict) else {}
        benchmark = run.benchmark_snapshot if isinstance(run.benchmark_snapshot, dict) else {}
        hardware = run.hardware_snapshot if isinstance(run.hardware_snapshot, dict) else {}
        score = self.aggregate.score
        summary_group = QGroupBox("Run summary")
        summary_form = self._form()
        summary_values = (
            ("Recorded", run.created_at),
            ("Model", snapshot_label(model, "model_name", "name")),
            ("Benchmark", snapshot_label(benchmark, "name", "file_path", "benchmark_file")),
            ("Session", self.aggregate.session.title if self.aggregate.session else (NOT_RECORDED if run.session_id is None else UNAVAILABLE)),
            ("Prompt name", run.prompt_name or snapshot_label(run.prompt_snapshot, "name", default=NOT_RECORDED)),
            ("Hardware", snapshot_label(hardware, "name", "computer_name", "gpu", default=UNAVAILABLE)),
            ("Overall score", _format_score(score.overall_score if score else None)),
        )
        for label, value in summary_values:
            summary_form.addRow(label, self._line_value(value))
        summary_group.setLayout(summary_form)

        inference_group = QGroupBox("Inference settings")
        inference_form = self._form()
        inference_values = (
            ("Backend", model.get("backend")),
            ("Thinking Mode", _format_bool(model.get("thinking_enabled"))),
            ("Temperature", model.get("temperature")),
            ("Top-P", model.get("top_p")),
            ("Top-K", model.get("top_k")),
            ("Min-P", model.get("min_p")),
            ("Context Length", model.get("context_length")),
            ("Flash Attention", _format_bool(model.get("flash_attention"))),
            ("MoE Experts", model.get("moe_experts")),
            ("Quantization", model.get("quantization")),
            ("Tokens/sec", _format_speed(model.get("tokens_per_second"))),
        )
        for label, value in inference_values:
            inference_form.addRow(label, self._line_value(value))
        inference_group.setLayout(inference_form)

        body = QWidget()
        body.setObjectName("runDetailsSummary")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)
        layout.addWidget(summary_group)
        layout.addWidget(inference_group)
        layout.addStretch(1)
        return self._scroll(body)

    def _prompt_output_tab(self) -> QWidget:
        run = self.aggregate.run
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(QLabel("Prompt name"))
        layout.addWidget(self._line_value(run.prompt_name or snapshot_label(run.prompt_snapshot, "name", default=NOT_RECORDED)))

        prompt_row = QHBoxLayout()
        prompt_row.addWidget(QLabel("Exact prompt text"))
        prompt_row.addStretch(1)
        self.copy_prompt_button = QPushButton("Copy Prompt")
        self.copy_prompt_button.setAccessibleName("Copy exact prompt")
        self.copy_prompt_button.clicked.connect(self.copy_prompt)
        prompt_row.addWidget(self.copy_prompt_button)
        layout.addLayout(prompt_row)
        self.prompt_text = self._text_value(run.prompt_text, minimum_height=150)
        self.prompt_text.setObjectName("exactPromptText")
        layout.addWidget(self.prompt_text)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Raw model output"))
        output_row.addStretch(1)
        self.copy_output_button = QPushButton("Copy Output")
        self.copy_output_button.setAccessibleName("Copy raw model output")
        self.copy_output_button.clicked.connect(self.copy_output)
        output_row.addWidget(self.copy_output_button)
        layout.addLayout(output_row)
        self.raw_output = self._text_value(run.raw_model_output, minimum_height=240)
        self.raw_output.setObjectName("rawModelOutput")
        layout.addWidget(self.raw_output, 1)
        self.copy_status = QLabel()
        self.copy_status.setObjectName("copyStatus")
        layout.addWidget(self.copy_status)
        return self._scroll(body)

    def _evaluation_tab(self) -> QWidget:
        score = self.aggregate.score
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        if score is None:
            label = QLabel("No review score was recorded for this run.")
            label.setObjectName("emptyState")
            label.setWordWrap(True)
            layout.addWidget(label)
            layout.addStretch(1)
            return self._scroll(body)

        form = self._form()
        for label, value in (
            ("Accuracy", _format_score(score.accuracy_score)),
            ("Hallucination", score.hallucination_level),
            ("Reliability", score.reliability_level),
            ("Depth", _format_score(score.depth_score)),
            ("Signal-to-noise", _format_score(score.signal_noise_score)),
            ("Actionability", _format_score(score.actionability_score)),
            ("Seniority", _format_score(score.seniority_score)),
            ("Overall score", _format_score(score.overall_score)),
        ):
            form.addRow(label, self._line_value(value))
        layout.addLayout(form)
        for label, value in (
            ("Strengths", score.strengths),
            ("Weaknesses", score.weaknesses),
            ("Verdict", score.verdict),
            ("Notes", score.notes),
        ):
            layout.addWidget(QLabel(label))
            layout.addWidget(self._text_value(value, minimum_height=75))
        layout.addStretch(1)
        return self._scroll(body)

    def _snapshots_tab(self) -> QWidget:
        run = self.aggregate.run
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        note = QLabel("These values are the historical snapshots saved with the run; current catalog edits do not replace them.")
        note.setObjectName("pageDescription")
        note.setWordWrap(True)
        layout.addWidget(note)
        snapshot_text = self._text_value(
            _json_text(
                {
                    "model": _safe_snapshot(run.model_snapshot),
                    "benchmark": _safe_snapshot(run.benchmark_snapshot),
                    "prompt": _safe_snapshot(run.prompt_snapshot),
                    "hardware": _safe_snapshot(run.hardware_snapshot),
                }
            ),
            minimum_height=420,
        )
        snapshot_text.setObjectName("historicalSnapshots")
        snapshot_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(snapshot_text, 1)
        return self._scroll(body)

    def _attachments_tab(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        heading = QHBoxLayout()
        description = QLabel(
            "Files remain outside the database. Removing an attachment only removes its metadata reference."
        )
        description.setObjectName("pageDescription")
        description.setWordWrap(True)
        heading.addWidget(description, 1)
        add_button = QPushButton("Add Attachment")
        add_button.setObjectName("primaryButton")
        add_button.setAccessibleName("Add Attachment")
        add_button.clicked.connect(self.add_attachment)
        heading.addWidget(add_button)
        layout.addLayout(heading)

        if not self.aggregate.attachments:
            empty = QLabel("No attachments have been recorded for this run.")
            empty.setObjectName("emptyState")
            empty.setWordWrap(True)
            layout.addWidget(empty)
        else:
            for attachment in self.aggregate.attachments:
                layout.addWidget(self._attachment_card(attachment))
        layout.addStretch(1)
        return self._scroll(body)

    def _attachment_card(self, attachment: RunAttachment) -> QGroupBox:
        title = attachment.original_filename or "Attachment"
        card = QGroupBox(title)
        card.setObjectName(f"attachmentCard{attachment.id or 0}")
        form = self._form()
        form.addRow("Attachment type", self._line_value(attachment.attachment_type))
        form.addRow("Filename", self._line_value(attachment.original_filename))
        form.addRow("File path", self._line_value(attachment.file_path))
        form.addRow("Notes", self._text_value(attachment.notes, minimum_height=70))

        status = QLabel("File Available" if _is_file(attachment.file_path) else "File Missing")
        status.setObjectName("attachmentFileStatus")
        status.setAccessibleName("Attachment file status")
        status.setWordWrap(True)
        if not _is_file(attachment.file_path):
            status.setProperty("state", "missing")
        form.addRow("Status", status)

        actions = QHBoxLayout()
        edit_button = QPushButton("Edit Attachment")
        edit_button.setAccessibleName("Edit Attachment")
        edit_button.clicked.connect(partial(self.edit_attachment, attachment))
        remove_button = QPushButton("Remove Attachment")
        remove_button.setAccessibleName("Remove Attachment")
        remove_button.clicked.connect(partial(self.remove_attachment, attachment))
        open_button = QPushButton("Open File")
        open_button.setAccessibleName("Open File")
        open_button.clicked.connect(partial(self.open_attachment_file, attachment))
        folder_button = QPushButton("Open Containing Folder")
        folder_button.setAccessibleName("Open Containing Folder")
        folder_button.clicked.connect(partial(self.open_attachment_folder, attachment))
        for button in (edit_button, remove_button, open_button, folder_button):
            actions.addWidget(button)
        actions.addStretch(1)

        card_layout = QVBoxLayout(card)
        card_layout.addLayout(form)
        card_layout.addLayout(actions)
        return card

    def _metadata_tab(self) -> QWidget:
        run = self.aggregate.run
        form = self._form()
        for label, value in (
            ("Run ID", run.id),
            ("Created", run.created_at),
            ("Updated", run.updated_at),
            ("Fingerprint", run.fingerprint),
            ("Session reference", run.session_id),
            ("Model profile reference", run.model_profile_id),
            ("Benchmark reference", run.benchmark_definition_id),
            ("Prompt template reference", run.prompt_template_id),
            ("Hardware reference", run.hardware_profile_id),
        ):
            form.addRow(label, self._line_value(value))
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(form)
        layout.addStretch(1)
        return self._scroll(body)

    def _reload_tabs(self) -> None:
        previous_index = self.tabs.currentIndex()
        previous_label = self.tabs.tabText(previous_index) if previous_index >= 0 else ""
        self.aggregate = self.load_aggregate(self.context, self.run_id)
        layout = self.layout()
        if not isinstance(layout, QVBoxLayout):
            return
        layout.removeWidget(self.tabs)
        self.tabs.deleteLater()
        self.tabs = self._build_tabs()
        layout.insertWidget(1, self.tabs, 1)

        restored_index = -1
        if previous_label:
            for index in range(self.tabs.count()):
                if self.tabs.tabText(index) == previous_label:
                    restored_index = index
                    break
        if restored_index < 0 and self.tabs.count() > 0:
            restored_index = min(max(previous_index, 0), self.tabs.count() - 1)
        if restored_index >= 0:
            self.tabs.setCurrentIndex(restored_index)

    def _show_attachment_failure(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def add_attachment(self) -> None:
        try:
            editor = AttachmentEditorDialog(self.context, self.run_id, parent=self)
            if editor.exec() != QDialog.DialogCode.Accepted:
                return
            self._reload_tabs()
        except Exception:
            self.context.logger.exception("Add attachment failed for run %s", self.run_id)
            self._show_attachment_failure(
                "Attachment could not be added",
                "The attachment could not be added. Check the selected file and see logs/error.log for details.",
            )

    def edit_attachment(self, attachment: RunAttachment) -> None:
        try:
            editor = AttachmentEditorDialog(self.context, self.run_id, attachment, self)
            if editor.exec() != QDialog.DialogCode.Accepted:
                return
            self._reload_tabs()
        except Exception:
            self.context.logger.exception("Edit attachment failed for run %s", self.run_id)
            self._show_attachment_failure(
                "Attachment could not be edited",
                "The attachment metadata could not be edited. See logs/error.log for details.",
            )

    def remove_attachment(self, attachment: RunAttachment) -> None:
        if attachment.id is None:
            self._show_attachment_failure("Attachment could not be removed", "This attachment has no persisted ID.")
            return
        answer = QMessageBox.question(
            self,
            "Remove attachment?",
            f"Remove the attachment reference for '{attachment.original_filename}'?\n\n"
            "The file on disk will NOT be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.context.benchmarks.delete_attachment(attachment.id)
            self._reload_tabs()
        except Exception:
            self.context.logger.exception("Remove attachment failed for run %s", self.run_id)
            self._show_attachment_failure(
                "Attachment could not be removed",
                "The attachment reference could not be removed. See logs/error.log for details.",
            )

    def open_attachment_file(self, attachment: RunAttachment) -> None:
        if not _is_file(attachment.file_path):
            self._show_attachment_failure(
                "File unavailable",
                f"The attachment file is missing or inaccessible:\n{attachment.file_path}",
            )
            return
        try:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(attachment.file_path))))
        except (OSError, ValueError):
            opened = False
        if not opened:
            self._show_attachment_failure(
                "File could not be opened",
                f"The operating system could not open:\n{attachment.file_path}",
            )

    def open_attachment_folder(self, attachment: RunAttachment) -> None:
        try:
            folder = Path(attachment.file_path).parent
            folder_text = str(folder)
        except (OSError, ValueError):
            folder_text = ""
        if not _is_directory(folder_text):
            self._show_attachment_failure(
                "Folder unavailable",
                f"The containing folder is missing or inaccessible:\n{folder_text or attachment.file_path}",
            )
            return
        try:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(folder_text))
        except (OSError, ValueError):
            opened = False
        if not opened:
            self._show_attachment_failure(
                "Folder could not be opened",
                f"The operating system could not open:\n{folder_text}",
            )

    def edit_review(self) -> None:
        """Open the dedicated review editor and reload only the mutable review."""

        try:
            editor = ReviewEditorDialog(self.context, self.run_id, self)
            if editor.exec() != QDialog.DialogCode.Accepted:
                return
            self._reload_tabs()
            self.review_saved.emit(self.run_id)
        except Exception:
            self.context.logger.exception("Review editor failed for run %s", self.run_id)

    def _copy_text(self, value: str, label: str) -> None:
        if not value:
            self.copy_status.setText(f"No {label.lower()} text to copy.")
            return
        QApplication.clipboard().setText(value)
        self.copy_status.setText(f"{label} copied to the clipboard.")

    def copy_prompt(self) -> None:
        self._copy_text(self.aggregate.run.prompt_text, "Prompt")

    def copy_output(self) -> None:
        self._copy_text(self.aggregate.run.raw_model_output, "Output")


__all__ = ("RunDetailsDialog",)
