"""Review-before-save Add Run wizard over the existing engine services."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWizard,
    QWizardPage,
    QWidget,
)

from ..context import GuiApplicationContext

try:
    from ...engine.domain import LEVELS, BenchmarkRun, ReviewScore
    from ...engine.services import is_database_integrity_error
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.domain import LEVELS, BenchmarkRun, ReviewScore  # type: ignore[no-redef]
    from engine.services import is_database_integrity_error  # type: ignore[no-redef]


NOT_SELECTED = "Not selected"


class OptionalScoreField(QWidget):
    """A numeric review field with a real, explicit unrecorded state."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(f"optional{label.replace(' ', '')}Field")
        self.record_checkbox = QCheckBox("Record")
        self.record_checkbox.setAccessibleName(f"Record {label}")
        self.spin_box = QDoubleSpinBox()
        self.spin_box.setAccessibleName(label)
        self.spin_box.setRange(0.0, 5.0)
        self.spin_box.setDecimals(2)
        self.spin_box.setSingleStep(0.5)
        self.spin_box.setValue(0.0)
        self.spin_box.setEnabled(False)
        self.record_checkbox.toggled.connect(self.spin_box.setEnabled)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.record_checkbox)
        layout.addWidget(self.spin_box)
        layout.addStretch(1)

    def value(self) -> float | None:
        return float(self.spin_box.value()) if self.record_checkbox.isChecked() else None


class _ReviewPage(QWizardPage):
    def __init__(
        self,
        update_callback: Callable[[], None],
        save_callback: Callable[[], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._update_callback = update_callback
        self._save_callback = save_callback

    def initializePage(self) -> None:
        self._update_callback()
        super().initializePage()

    def validatePage(self) -> bool:
        return self._save_callback()


class AddRunWizard(QWizard):
    """Four-page Add Run workflow with no writes before final confirmation."""

    run_created = Signal(int)

    def __init__(
        self,
        context: GuiApplicationContext,
        parent: QWidget | None = None,
        *,
        confirm_close: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.confirm_close = confirm_close or self._ask_confirm_close
        self.saved_run: BenchmarkRun | None = None
        self._dirty = False
        self._saving = False
        self._saved = False
        self.setObjectName("addRunWizard")
        self.setWindowTitle("Add Benchmark Run")
        self.setAccessibleName("Add benchmark run wizard")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setMinimumSize(720, 620)
        self.resize(900, 760)

        self.context_page = self._build_context_page()
        self.prompt_page = self._build_prompt_page()
        self.evaluation_page = self._build_evaluation_page()
        self.review_page = _ReviewPage(self._update_review_summary, self._save, self)
        self._build_review_page(self.review_page)
        self.addPage(self.context_page)
        self.addPage(self.prompt_page)
        self.addPage(self.evaluation_page)
        self.addPage(self.review_page)
        self._load_catalog_choices()
        self._connect_dirty_tracking()
        self._dirty = False

    def _build_context_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Context")
        page.setSubTitle("Choose existing catalog records when available. Every relationship may remain unselected for a manual run.")
        layout = QVBoxLayout(page)
        self.catalog_status = QLabel()
        self.catalog_status.setObjectName("catalogStatus")
        self.catalog_status.setWordWrap(True)
        layout.addWidget(self.catalog_status)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(10)
        self.session_combo = self._selector("Session")
        self.model_combo = self._selector("Model profile")
        self.benchmark_combo = self._selector("Benchmark definition")
        self.prompt_template_combo = self._selector("Prompt template")
        self.hardware_combo = self._selector("Hardware profile")
        form.addRow("Session", self.session_combo)
        form.addRow("Model profile", self.model_combo)
        form.addRow("Benchmark definition", self.benchmark_combo)
        form.addRow("Prompt template", self.prompt_template_combo)
        form.addRow("Hardware profile", self.hardware_combo)
        layout.addLayout(form)
        layout.addStretch(1)

        self.prompt_template_combo.currentIndexChanged.connect(self._prefill_from_template)
        self.benchmark_combo.currentIndexChanged.connect(self._prefill_from_benchmark)
        return page

    def _build_prompt_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Prompt and Result")
        page.setSubTitle("Preserve the exact prompt and raw model output. The engine creates snapshots and fingerprints when you save.")
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(8)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.prompt_name_edit = QLineEdit()
        self.prompt_name_edit.setAccessibleName("Prompt name")
        self.prompt_name_edit.setPlaceholderText("Optional manual prompt name")
        form.addRow("Prompt name", self.prompt_name_edit)
        layout.addLayout(form)

        layout.addWidget(QLabel("Exact prompt text"))
        self.prompt_text_edit = QPlainTextEdit()
        self.prompt_text_edit.setObjectName("promptTextInput")
        self.prompt_text_edit.setAccessibleName("Exact prompt text")
        self.prompt_text_edit.setPlaceholderText("Paste or type the exact prompt, or leave blank when a selected template supplies it.")
        self.prompt_text_edit.setMinimumHeight(150)
        layout.addWidget(self.prompt_text_edit)

        layout.addWidget(QLabel("Raw model output"))
        self.raw_output_edit = QPlainTextEdit()
        self.raw_output_edit.setObjectName("rawOutputInput")
        self.raw_output_edit.setAccessibleName("Raw model output")
        self.raw_output_edit.setPlaceholderText("Paste the complete raw model output.")
        self.raw_output_edit.setMinimumHeight(250)
        layout.addWidget(self.raw_output_edit, 1)
        return self._scroll_page(page, body)

    def _build_evaluation_page(self) -> QWizardPage:
        page = QWizardPage()
        page.setTitle("Evaluation")
        page.setSubTitle("A review is optional. Numeric fields start as Not recorded; a checked zero remains a real score of 0.")
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(9)

        self.record_review_checkbox = QCheckBox("Record a ReviewScore for this run")
        self.record_review_checkbox.setObjectName("recordReviewScore")
        self.record_review_checkbox.setAccessibleName("Record review score")
        self.record_review_checkbox.toggled.connect(self._review_toggled)
        layout.addWidget(self.record_review_checkbox)

        score_group = QGroupBox("Scores")
        score_layout = QGridLayout(score_group)
        score_layout.setHorizontalSpacing(12)
        score_layout.setVerticalSpacing(8)
        self.score_fields: dict[str, OptionalScoreField] = {}
        for position, (key, label) in enumerate(
            (
                ("accuracy_score", "Accuracy (0-5)"),
                ("depth_score", "Depth (0-5)"),
                ("signal_noise_score", "Signal-to-noise (0-5)"),
                ("actionability_score", "Actionability (0-5)"),
                ("seniority_score", "Seniority (0-5)"),
                ("overall_score", "Overall (0-5)"),
            )
        ):
            control = OptionalScoreField(label)
            self.score_fields[key] = control
            row, column = divmod(position, 2)
            score_layout.addWidget(QLabel(label), row, column * 2)
            score_layout.addWidget(control, row, column * 2 + 1)
        layout.addWidget(score_group)

        categorical_group = QGroupBox("Review levels")
        categorical_form = QFormLayout(categorical_group)
        self.hallucination_combo = self._level_combo("Hallucination level")
        self.reliability_combo = self._level_combo("Reliability level")
        categorical_form.addRow("Hallucination level", self.hallucination_combo)
        categorical_form.addRow("Reliability level", self.reliability_combo)
        categorical_note = QLabel("The current engine requires both categorical levels when a ReviewScore exists; Medium is its authoritative default.")
        categorical_note.setObjectName("fieldHint")
        categorical_note.setWordWrap(True)
        categorical_form.addRow("", categorical_note)
        layout.addWidget(categorical_group)

        text_group = QGroupBox("Review text")
        text_layout = QFormLayout(text_group)
        self.strengths_edit = QPlainTextEdit()
        self.weaknesses_edit = QPlainTextEdit()
        self.verdict_edit = QLineEdit()
        self.notes_edit = QPlainTextEdit()
        for field, name, height in (
            (self.strengths_edit, "Strengths", 70),
            (self.weaknesses_edit, "Weaknesses", 70),
            (self.notes_edit, "Notes", 90),
        ):
            field.setAccessibleName(name)
            field.setMinimumHeight(height)
            text_layout.addRow(name, field)
        self.verdict_edit.setAccessibleName("Verdict")
        text_layout.addRow("Verdict", self.verdict_edit)
        layout.addWidget(text_group)
        self._set_evaluation_enabled(False)
        return self._scroll_page(page, body)

    def _build_review_page(self, page: QWizardPage) -> None:
        page.setTitle("Review and Save")
        page.setSubTitle("Review the intended values. Finish saves the run and optional review atomically through the engine.")
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(8)
        self.review_summary = QLabel()
        self.review_summary.setObjectName("addRunReviewSummary")
        self.review_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.review_summary.setWordWrap(True)
        self.review_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        layout.addWidget(self.review_summary)

        layout.addWidget(QLabel("Prompt text"))
        self.review_prompt_text = QPlainTextEdit()
        self.review_prompt_text.setReadOnly(True)
        self.review_prompt_text.setMinimumHeight(110)
        layout.addWidget(self.review_prompt_text)
        layout.addWidget(QLabel("Raw model output"))
        self.review_raw_output = QPlainTextEdit()
        self.review_raw_output.setReadOnly(True)
        self.review_raw_output.setMinimumHeight(160)
        layout.addWidget(self.review_raw_output, 1)
        self.save_error_label = QLabel()
        self.save_error_label.setObjectName("validationError")
        self.save_error_label.setWordWrap(True)
        self.save_error_label.setVisible(False)
        layout.addWidget(self.save_error_label)
        scroll = QScrollArea()
        scroll.setObjectName("wizardScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(4, 4, 4, 4)
        page_layout.addWidget(scroll)

    @staticmethod
    def _scroll_page(page: QWizardPage, body: QWidget) -> QWizardPage:
        scroll = QScrollArea()
        scroll.setObjectName("wizardScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(scroll)
        return page

    @staticmethod
    def _selector(label: str) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName(label.lower().replace(" ", "_") + "_selector")
        combo.setAccessibleName(label)
        combo.addItem(NOT_SELECTED, None)
        return combo

    @staticmethod
    def _level_combo(label: str) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName(label.lower().replace(" ", "_") + "_selector")
        combo.setAccessibleName(label)
        combo.addItems(list(LEVELS))
        combo.setCurrentText("Medium")
        return combo

    def refresh_catalog_choices(self) -> None:
        """Reload current eligible catalog records without restarting the GUI."""

        self._load_catalog_choices(preserve_selection=True)

    def _load_catalog_choices(self, *, preserve_selection: bool = False) -> None:
        try:
            previous = {
                "session": self._selected_id(self.session_combo) if preserve_selection else None,
                "model": self._selected_id(self.model_combo) if preserve_selection else None,
                "benchmark": self._selected_id(self.benchmark_combo) if preserve_selection else None,
                "prompt": self._selected_id(self.prompt_template_combo) if preserve_selection else None,
                "hardware": self._selected_id(self.hardware_combo) if preserve_selection else None,
            }
            sessions = self.context.catalog.list_sessions()
            models = self.context.catalog.list_model_profiles()
            benchmarks = self.context.catalog.list_benchmark_definitions()
            templates = self.context.catalog.list_prompt_templates()
            hardware = self.context.catalog.list_hardware_profiles()
            self._populate(self.session_combo, sessions, lambda value: value.title, selected_id=previous["session"], preserve_selection=preserve_selection)
            default_model = next((value.id for value in models if value.is_default), None)
            self._populate(self.model_combo, models, lambda value: value.name, default_id=default_model, selected_id=previous["model"], preserve_selection=preserve_selection)
            self._populate(self.benchmark_combo, benchmarks, lambda value: value.name, selected_id=previous["benchmark"], preserve_selection=preserve_selection)
            self._populate(self.prompt_template_combo, templates, lambda value: f"{value.name} v{value.version}", selected_id=previous["prompt"], preserve_selection=preserve_selection)
            self._populate(self.hardware_combo, hardware, lambda value: value.name, selected_id=previous["hardware"], preserve_selection=preserve_selection)
            if not any((sessions, models, benchmarks, templates, hardware)):
                self.catalog_status.setText("No catalog records are available. Manual/custom entry is supported; this workflow will not create catalog records automatically.")
            else:
                self.catalog_status.setText("Catalog selectors use current active records where lifecycle rules apply. Leaving a selector unselected keeps that relationship absent.")
        except Exception:
            self.context.logger.exception("Add Run catalog loading failed")
            self.catalog_status.setText("Catalog records could not be loaded. You can still enter a manual/custom run; see logs/error.log for details.")

    @staticmethod
    def _populate(
        combo: QComboBox,
        records: Iterable[Any],
        labeler: Callable[[Any], str],
        *,
        default_id: int | None = None,
        selected_id: int | None = None,
        preserve_selection: bool = False,
    ) -> None:
        ordered = sorted(
            (record for record in records if getattr(record, "id", None) is not None),
            key=lambda record: (labeler(record).casefold(), labeler(record), int(record.id)),
        )
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(NOT_SELECTED, None)
        for record in ordered:
            combo.addItem(labeler(record), record.id)
        target_id = selected_id if preserve_selection else default_id
        if target_id is not None:
            index = combo.findData(target_id)
            if index >= 0:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _prefill_from_template(self, *_args: object) -> None:
        template_id = self._selected_id(self.prompt_template_combo)
        if template_id is None or not hasattr(self, "prompt_name_edit"):
            return
        template = self.context.catalog.get_prompt_template(template_id)
        if template is None:
            return
        if not self.prompt_name_edit.text().strip():
            self.prompt_name_edit.setText(template.name)
        if not self.prompt_text_edit.toPlainText():
            self.prompt_text_edit.setPlainText(template.prompt_text)

    def _prefill_from_benchmark(self, *_args: object) -> None:
        definition_id = self._selected_id(self.benchmark_combo)
        if definition_id is None or not hasattr(self, "prompt_text_edit"):
            return
        definition = self.context.catalog.get_benchmark_definition(definition_id)
        if definition is not None and definition.default_prompt and not self.prompt_text_edit.toPlainText():
            self.prompt_text_edit.setPlainText(definition.default_prompt)

    @staticmethod
    def _selected_id(combo: QComboBox) -> int | None:
        value = combo.currentData()
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None

    def _connect_dirty_tracking(self) -> None:
        for combo in (self.session_combo, self.model_combo, self.benchmark_combo, self.prompt_template_combo, self.hardware_combo, self.hallucination_combo, self.reliability_combo):
            combo.currentIndexChanged.connect(self._mark_dirty)
        for field in (self.prompt_name_edit, self.verdict_edit):
            field.textChanged.connect(self._mark_dirty)
        for field in (self.prompt_text_edit, self.raw_output_edit, self.strengths_edit, self.weaknesses_edit, self.notes_edit):
            field.textChanged.connect(self._mark_dirty)
        self.record_review_checkbox.toggled.connect(self._mark_dirty)
        for field in self.score_fields.values():
            field.record_checkbox.toggled.connect(self._mark_dirty)
            field.spin_box.valueChanged.connect(self._mark_dirty)

    def _mark_dirty(self, *_args: object) -> None:
        if not self._saved:
            self._dirty = True

    def _set_evaluation_enabled(self, enabled: bool) -> None:
        for widget in self._evaluation_children():
            widget.setEnabled(enabled)
        self.record_review_checkbox.setEnabled(True)

    def _evaluation_children(self) -> tuple[QWidget, ...]:
        return (
            self.hallucination_combo,
            self.reliability_combo,
            self.strengths_edit,
            self.weaknesses_edit,
            self.verdict_edit,
            self.notes_edit,
            *(self.score_fields.values()),
        )

    def _review_toggled(self, checked: bool) -> None:
        for widget in self._evaluation_children():
            widget.setEnabled(checked)

    def _build_score(self) -> ReviewScore | None:
        if not self.record_review_checkbox.isChecked():
            return None
        return ReviewScore(
            run_id=0,
            accuracy_score=self.score_fields["accuracy_score"].value(),
            hallucination_level=self.hallucination_combo.currentText(),
            reliability_level=self.reliability_combo.currentText(),
            depth_score=self.score_fields["depth_score"].value(),
            signal_noise_score=self.score_fields["signal_noise_score"].value(),
            actionability_score=self.score_fields["actionability_score"].value(),
            seniority_score=self.score_fields["seniority_score"].value(),
            overall_score=self.score_fields["overall_score"].value(),
            strengths=self.strengths_edit.toPlainText(),
            weaknesses=self.weaknesses_edit.toPlainText(),
            verdict=self.verdict_edit.text(),
            notes=self.notes_edit.toPlainText(),
        )

    def _build_run(self) -> BenchmarkRun:
        return BenchmarkRun(
            raw_model_output=self.raw_output_edit.toPlainText(),
            prompt_name=self.prompt_name_edit.text(),
            prompt_text=self.prompt_text_edit.toPlainText(),
            session_id=self._selected_id(self.session_combo),
            model_profile_id=self._selected_id(self.model_combo),
            benchmark_definition_id=self._selected_id(self.benchmark_combo),
            prompt_template_id=self._selected_id(self.prompt_template_combo),
            hardware_profile_id=self._selected_id(self.hardware_combo),
        )

    def _update_review_summary(self) -> None:
        run = self._build_run()
        score = self._build_score()
        self.review_summary.setText(
            "Context\n"
            f"Session: {self.session_combo.currentText()}\n"
            f"Model: {self.model_combo.currentText()}\n"
            f"Benchmark: {self.benchmark_combo.currentText()}\n"
            f"Prompt template: {self.prompt_template_combo.currentText()}\n"
            f"Hardware: {self.hardware_combo.currentText()}\n\n"
            "Result and review\n"
            f"Prompt name: {run.prompt_name or NOT_SELECTED}\n"
            f"ReviewScore: {'will be recorded' if score is not None else 'not recorded'}\n"
            f"Overall score: {score.overall_score if score and score.overall_score is not None else NOT_SELECTED}"
        )
        self.review_prompt_text.setPlainText(run.prompt_text)
        self.review_raw_output.setPlainText(run.raw_model_output)

    def _save(self) -> bool:
        if self._saved or self._saving:
            return False
        self._saving = True
        self.save_error_label.setVisible(False)
        finish = self.button(QWizard.WizardButton.FinishButton)
        if finish is not None:
            finish.setEnabled(False)
        try:
            run, _ = self.context.benchmarks.save_run_atomic(self._build_run(), self._build_score())
            if run.id is None:
                raise ValueError("saved benchmark run is missing its ID")
            self.saved_run = run
            self._saved = True
            self._dirty = False
            self.run_created.emit(run.id)
            return True
        except Exception as error:
            if is_database_integrity_error(error):
                self.context.logger.info("Add Run rejected by database constraint: %s", error)
                self._show_save_error("This run matches an existing run or another database constraint. Nothing was saved.")
            elif isinstance(error, ValueError):
                self.context.logger.info("Add Run validation failed: %s", error)
                self._show_save_error(f"The run could not be saved: {error}")
            else:
                self.context.logger.exception("Add Run failed")
                self._show_save_error("The run could not be saved. See logs/error.log for details; no partial run was kept.")
            return False
        finally:
            self._saving = False
            if not self._saved and finish is not None:
                finish.setEnabled(True)

    def _show_save_error(self, message: str) -> None:
        self.save_error_label.setText(message)
        self.save_error_label.setVisible(True)

    def _ask_confirm_close(self) -> bool:
        answer = QMessageBox.question(
            self,
            "Discard unsaved run?",
            "This Add Run form has unsaved changes. Discard them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def reject(self) -> None:
        if self._dirty and not self._saved and not self.confirm_close():
            return
        super().reject()

    def closeEvent(self, event: object) -> None:
        if self._dirty and not self._saved and not self.confirm_close():
            event.ignore()  # type: ignore[attr-defined]
            return
        event.accept()  # type: ignore[attr-defined]
        super().closeEvent(event)  # type: ignore[arg-type]


__all__ = ("AddRunWizard", "NOT_SELECTED", "OptionalScoreField")
