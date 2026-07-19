"""Dedicated editor for the ReviewScore attached to one benchmark run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from PySide6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QLabel, QLineEdit, QPlainTextEdit, QWidget

from ..context import GuiApplicationContext
from ..views.add_run import OptionalScoreField

try:
    from ...engine.domain import LEVELS, BenchmarkRun, ReviewScore
except ImportError:  # pragma: no cover - exercised by top-level test imports.
    from engine.domain import LEVELS, BenchmarkRun, ReviewScore  # type: ignore[no-redef]

from .base import CatalogEditorDialog


class ReviewEditorDialog(CatalogEditorDialog):
    """Edit or create only the review attached to an existing run."""

    def __init__(
        self,
        context: GuiApplicationContext,
        run_id: int,
        parent: QWidget | None = None,
        *,
        confirm_close: Callable[[], bool] | None = None,
    ) -> None:
        run, score, _ = context.benchmarks.get_run(run_id)
        if run is None:
            raise LookupError(f"Run #{run_id} was not found.")

        self.run_id = run_id
        self.run: BenchmarkRun = run
        self.score = score
        super().__init__(
            context,
            title=f"Edit Review - Run #{run_id}",
            parent=parent,
            confirm_close=confirm_close,
        )
        self.setObjectName("reviewEditorDialog")
        self.setAccessibleName("Edit Review")
        if self.save_button is not None:
            self.save_button.setAccessibleName("Save review")
        if self.cancel_button is not None:
            self.cancel_button.setAccessibleName("Cancel review editor")

        model_snapshot = self.run.model_snapshot if isinstance(self.run.model_snapshot, dict) else {}
        context_label = QLabel(
            f"Run #{run_id}  |  Model: {model_snapshot.get('model_name') or model_snapshot.get('name') or 'Not recorded'}"
        )
        context_label.setObjectName("reviewEditorContext")
        context_label.setWordWrap(True)
        self.form.addRow(context_label)

        self.score_fields: dict[str, OptionalScoreField] = {}
        scores = QGroupBox("Scores")
        score_form = QFormLayout(scores)
        for key, label in (
            ("accuracy_score", "Accuracy (0-5)"),
            ("depth_score", "Depth (0-5)"),
            ("signal_noise_score", "Signal-to-noise (0-5)"),
            ("actionability_score", "Actionability (0-5)"),
            ("seniority_score", "Seniority (0-5)"),
            ("overall_score", "Overall (0-5)"),
        ):
            field = OptionalScoreField(label)
            self._load_score_field(field, getattr(score, key, None) if score else None)
            self.score_fields[key] = field
            score_form.addRow(label, field)
            field.record_checkbox.toggled.connect(self.mark_dirty)
            field.spin_box.valueChanged.connect(self.mark_dirty)
        self.form.addRow(scores)

        levels = QGroupBox("Review levels")
        level_form = QFormLayout(levels)
        self.hallucination_combo = self._level_combo(
            "Hallucination level",
            score.hallucination_level if score else "Medium",
        )
        self.reliability_combo = self._level_combo(
            "Reliability level",
            score.reliability_level if score else "Medium",
        )
        level_form.addRow("Hallucination", self.hallucination_combo)
        level_form.addRow("Reliability", self.reliability_combo)
        level_note = QLabel("The engine validates both categorical levels when the review is saved.")
        level_note.setObjectName("fieldHint")
        level_note.setWordWrap(True)
        level_form.addRow("", level_note)
        self.form.addRow(levels)
        self.hallucination_combo.currentIndexChanged.connect(self.mark_dirty)
        self.reliability_combo.currentIndexChanged.connect(self.mark_dirty)

        text_group = QGroupBox("Review text")
        text_form = QFormLayout(text_group)
        self.strengths_edit = self._text_edit("Strengths", score.strengths if score else "", 78)
        self.weaknesses_edit = self._text_edit("Weaknesses", score.weaknesses if score else "", 78)
        self.verdict_edit = QLineEdit(score.verdict if score else "")
        self.verdict_edit.setAccessibleName("Verdict")
        self.verdict_edit.setPlaceholderText("Optional")
        self.notes_edit = self._text_edit("Notes", score.notes if score else "", 105)
        text_form.addRow("Strengths", self.strengths_edit)
        text_form.addRow("Weaknesses", self.weaknesses_edit)
        text_form.addRow("Verdict", self.verdict_edit)
        text_form.addRow("Notes", self.notes_edit)
        self.form.addRow(text_group)
        for field in (self.strengths_edit, self.weaknesses_edit, self.notes_edit):
            field.textChanged.connect(self.mark_dirty)
        self.verdict_edit.textChanged.connect(self.mark_dirty)

    @staticmethod
    def _load_score_field(field: OptionalScoreField, value: float | None) -> None:
        if value is None:
            return
        field.record_checkbox.setChecked(True)
        field.spin_box.setValue(float(value))

    @staticmethod
    def _level_combo(accessible_name: str, value: str) -> QComboBox:
        combo = QComboBox()
        combo.setAccessibleName(accessible_name)
        combo.addItems(list(LEVELS))
        combo.setCurrentText(value if value in LEVELS else "Medium")
        return combo

    @staticmethod
    def _text_edit(accessible_name: str, value: str, minimum_height: int) -> QPlainTextEdit:
        field = QPlainTextEdit(value)
        field.setAccessibleName(accessible_name)
        field.setMinimumHeight(minimum_height)
        return field

    def build_draft(self) -> ReviewScore:
        values: dict[str, Any] = {
            "accuracy_score": self.score_fields["accuracy_score"].value(),
            "hallucination_level": self.hallucination_combo.currentText(),
            "reliability_level": self.reliability_combo.currentText(),
            "depth_score": self.score_fields["depth_score"].value(),
            "signal_noise_score": self.score_fields["signal_noise_score"].value(),
            "actionability_score": self.score_fields["actionability_score"].value(),
            "seniority_score": self.score_fields["seniority_score"].value(),
            "overall_score": self.score_fields["overall_score"].value(),
            "strengths": self.strengths_edit.toPlainText(),
            "weaknesses": self.weaknesses_edit.toPlainText(),
            "verdict": self.verdict_edit.text(),
            "notes": self.notes_edit.toPlainText(),
        }
        if self.score is not None:
            return replace(self.score, **values)
        return ReviewScore(run_id=self.run_id, **values)

    def save_draft(self, draft: ReviewScore) -> ReviewScore:
        if draft.id is None:
            return self.context.benchmarks.create_score(draft)
        return self.context.benchmarks.update_score(draft)


__all__ = ("ReviewEditorDialog",)
