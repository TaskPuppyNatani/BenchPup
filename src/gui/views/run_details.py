"""Read-only Run Details dialog backed by a complete engine aggregate."""

from __future__ import annotations

import json
import math
from typing import Any

from PySide6.QtCore import Qt, Signal
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
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..context import GuiApplicationContext
from ..dialogs.review_editor import ReviewEditorDialog
from ..models.runs import NOT_RECORDED, UNAVAILABLE, snapshot_label

try:
    from ...engine.reporting import BenchmarkRunAggregate
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
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
        if self.aggregate.attachments:
            layout.addWidget(QLabel("Attachments (read-only metadata)"))
            attachment_lines = [
                f"{item.attachment_type}: {item.original_filename} — {item.file_path}"
                for item in self.aggregate.attachments
            ]
            layout.addWidget(self._text_value("\n".join(attachment_lines), minimum_height=90))
        else:
            empty = QLabel("No attachment metadata was recorded for this run.")
            empty.setObjectName("emptyState")
            layout.addWidget(empty)
        layout.addStretch(1)
        return self._scroll(body)

    def edit_review(self) -> None:
        """Open the dedicated review editor and reload only the mutable review."""

        try:
            editor = ReviewEditorDialog(self.context, self.run_id, self)
            if editor.exec() != QDialog.DialogCode.Accepted:
                return
            self.aggregate = self.load_aggregate(self.context, self.run_id)
            layout = self.layout()
            if not isinstance(layout, QVBoxLayout):
                return
            layout.removeWidget(self.tabs)
            self.tabs.deleteLater()
            self.tabs = self._build_tabs()
            layout.insertWidget(1, self.tabs, 1)
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
