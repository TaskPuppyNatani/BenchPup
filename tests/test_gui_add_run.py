from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QWizard

from engine.domain import BenchmarkDefinition, BenchmarkRun, ModelProfile, PromptTemplate, ReviewScore
from gui.context import GuiApplicationContext
from gui.main_window import MainWindow
from gui.views.add_run import AddRunWizard


class GuiAddRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-add-run-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.context = GuiApplicationContext.create(database_path=Path(self.directory.name) / "data" / "benchmark.db")
        self.model_zed = self.context.catalog.model_profiles.create(ModelProfile(name="Zed profile", model_name="Zed"))
        self.model_alpha = self.context.catalog.model_profiles.create(ModelProfile(name="Alpha profile", model_name="Alpha", is_default=True))
        self.benchmark_zed = self.context.catalog.benchmark_definitions.create(
            BenchmarkDefinition(name="Zed benchmark", file_path="z.py", benchmark_type="code_review")
        )
        self.benchmark_alpha = self.context.catalog.benchmark_definitions.create(
            BenchmarkDefinition(name="Alpha benchmark", file_path="a.py", benchmark_type="code_review", default_prompt="Review this.")
        )
        prompt_text = "Review the submitted code."
        self.prompt = self.context.catalog.prompt_templates.create(
            PromptTemplate(
                name="Review prompt",
                version="1",
                prompt_text=prompt_text,
                prompt_hash=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
                benchmark_type="code_review",
            )
        )

    def tearDown(self) -> None:
        self.context.close()
        self.directory.cleanup()

    def _show(self, wizard: AddRunWizard) -> None:
        wizard.show()
        self.application.processEvents()

    def _finish(self, wizard: AddRunWizard) -> None:
        self._show(wizard)
        while wizard.currentId() < 3:
            wizard.next()
            self.application.processEvents()
        wizard.button(QWizard.WizardButton.FinishButton).click()
        self.application.processEvents()

    def test_global_and_runs_page_add_run_use_the_same_enabled_workflow(self) -> None:
        window = MainWindow(self.context)
        try:
            with patch("gui.main_window.AddRunWizard") as wizard_type:
                wizard_type.return_value.exec.return_value = 0
                window.add_run_button.click()
                window.runs.add_run_button.click()
            self.assertTrue(window.add_run_button.isEnabled())
            self.assertEqual(wizard_type.call_count, 2)
            self.assertIs(wizard_type.call_args_list[0].args[0], self.context)
            self.assertIs(wizard_type.call_args_list[1].args[0], self.context)
        finally:
            window.close()

    def test_successful_creation_coordinator_refreshes_runs_and_dashboard(self) -> None:
        window = MainWindow(self.context)
        try:
            saved, _ = self.context.benchmarks.save_run(BenchmarkRun(raw_model_output="coordinator output"))
            window._handle_run_created(saved.id or 0)
            self.assertEqual(window.current_page_key(), "runs")
            self.assertEqual(window.runs.table_model.rowCount(), 1)
            self.assertEqual(window.dashboard.summary_cards["benchmark_run_count"].value_label.text(), "1")
            self.assertIn("saved successfully", window.status_bar.currentMessage())
        finally:
            window.close()

    def test_selectors_are_real_sorted_records_and_default_model_is_selected(self) -> None:
        wizard = AddRunWizard(self.context, confirm_close=lambda: True)
        self.assertEqual(
            [wizard.model_combo.itemText(index) for index in range(wizard.model_combo.count())],
            ["Not selected", "Alpha profile", "Zed profile"],
        )
        self.assertEqual(wizard.model_combo.currentData(), self.model_alpha.id)
        self.assertEqual(
            [wizard.benchmark_combo.itemText(index) for index in range(wizard.benchmark_combo.count())],
            ["Not selected", "Alpha benchmark", "Zed benchmark"],
        )
        self.assertEqual(wizard.prompt_template_combo.itemText(1), "Review prompt v1")

    def test_cancel_and_back_write_nothing(self) -> None:
        cancelled = AddRunWizard(self.context, confirm_close=lambda: True)
        cancelled.raw_output_edit.setPlainText("cancelled")
        cancelled.reject()
        self.assertEqual(self.context.benchmarks.runs.list(), [])

        backed = AddRunWizard(self.context, confirm_close=lambda: True)
        self._show(backed)
        backed.raw_output_edit.setPlainText("not saved")
        backed.next()
        self.assertEqual(backed.currentId(), 1)
        backed.back()
        self.assertEqual(backed.currentId(), 0)
        self.assertEqual(self.context.benchmarks.runs.list(), [])
        backed.close()

    def test_empty_catalogs_are_friendly_and_do_not_create_records(self) -> None:
        empty_directory = tempfile.TemporaryDirectory()
        empty_context = GuiApplicationContext.create(database_path=Path(empty_directory.name) / "data" / "benchmark.db")
        try:
            wizard = AddRunWizard(empty_context, confirm_close=lambda: True)
            self.assertEqual(wizard.model_combo.count(), 1)
            self.assertIn("Manual/custom entry", wizard.catalog_status.text())
            wizard.close()
            self.assertEqual(empty_context.catalog.model_profiles.list(), [])
        finally:
            empty_context.close()
            empty_directory.cleanup()

    def test_optional_numeric_field_keeps_none_and_entered_zero(self) -> None:
        wizard = AddRunWizard(self.context, confirm_close=lambda: True)
        field = wizard.score_fields["accuracy_score"]
        self.assertIsNone(field.value())
        field.record_checkbox.setChecked(True)
        field.spin_box.setValue(0.0)
        self.assertEqual(field.value(), 0.0)

    def test_run_only_creation_supports_manual_custom_entry(self) -> None:
        wizard = AddRunWizard(self.context, confirm_close=lambda: True)
        wizard.model_combo.setCurrentIndex(0)
        wizard.prompt_name_edit.setText("Manual prompt")
        wizard.prompt_text_edit.setPlainText("Custom prompt")
        wizard.raw_output_edit.setPlainText("Custom output")
        source = wizard._build_run()
        self._finish(wizard)
        self.assertIsNotNone(wizard.saved_run)
        self.assertEqual(len(self.context.benchmarks.runs.list()), 1)
        saved = wizard.saved_run
        assert saved is not None
        self.assertIsNone(self.context.benchmarks.get_run(saved.id or 0)[1])
        self.assertIsNone(saved.model_profile_id)
        self.assertEqual(saved.model_snapshot, {})
        self.assertTrue(saved.fingerprint)
        self.assertEqual(source.model_snapshot, {})
        self.assertEqual(source.fingerprint, "")
        self.assertFalse(wizard.isVisible())

    def test_review_creation_uses_engine_snapshots_and_valid_values(self) -> None:
        wizard = AddRunWizard(self.context, confirm_close=lambda: True)
        wizard.model_combo.setCurrentIndex(wizard.model_combo.findData(self.model_alpha.id))
        wizard.benchmark_combo.setCurrentIndex(wizard.benchmark_combo.findData(self.benchmark_alpha.id))
        wizard.prompt_template_combo.setCurrentIndex(wizard.prompt_template_combo.findData(self.prompt.id))
        wizard.raw_output_edit.setPlainText("Scored output")
        wizard.record_review_checkbox.setChecked(True)
        wizard.score_fields["accuracy_score"].record_checkbox.setChecked(True)
        wizard.score_fields["accuracy_score"].spin_box.setValue(0.0)
        wizard.score_fields["overall_score"].record_checkbox.setChecked(True)
        wizard.score_fields["overall_score"].spin_box.setValue(4.25)
        source = wizard._build_run()
        wizard._update_review_summary()
        self.assertIn("Alpha profile", wizard.review_summary.text())
        self.assertIn("will be recorded", wizard.review_summary.text())
        self._finish(wizard)
        saved = wizard.saved_run
        self.assertIsNotNone(saved)
        assert saved is not None
        persisted, score, _ = self.context.benchmarks.get_run(saved.id or 0)
        self.assertIsNotNone(persisted)
        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(score.accuracy_score, 0.0)
        self.assertEqual(score.overall_score, 4.25)
        self.assertEqual(saved.model_snapshot["model_name"], "Alpha")
        self.assertEqual(saved.benchmark_snapshot["name"], "Alpha benchmark")
        self.assertEqual(saved.prompt_snapshot["prompt_text"], "Review the submitted code.")
        self.assertTrue(saved.fingerprint)
        self.assertEqual(source.model_snapshot, {})
        self.assertEqual(source.fingerprint, "")

    def test_invalid_engine_score_is_rejected_without_a_partial_run(self) -> None:
        wizard = AddRunWizard(self.context, confirm_close=lambda: True)
        with patch.object(wizard, "_build_score", return_value=ReviewScore(run_id=0, overall_score=6.0)):
            self.assertFalse(wizard._save())
        self.assertEqual(self.context.benchmarks.runs.list(), [])
        self.assertIn("could not be saved", wizard.save_error_label.text())

    def test_duplicate_fingerprint_is_friendly_and_finish_cannot_duplicate(self) -> None:
        first = AddRunWizard(self.context, confirm_close=lambda: True)
        first.raw_output_edit.setPlainText("duplicate output")
        first.prompt_text_edit.setPlainText("same prompt")
        self._finish(first)
        self.assertEqual(len(self.context.benchmarks.runs.list()), 1)

        second = AddRunWizard(self.context, confirm_close=lambda: True)
        second.raw_output_edit.setPlainText("duplicate output")
        second.prompt_text_edit.setPlainText("same prompt")
        self._finish(second)
        self.assertEqual(len(self.context.benchmarks.runs.list()), 1)
        self.assertIn("matches an existing run", second.save_error_label.text())

    def test_modified_close_uses_unsaved_change_confirmation(self) -> None:
        decisions: list[str] = []
        wizard = AddRunWizard(self.context, confirm_close=lambda: decisions.append("asked") or False)
        wizard.raw_output_edit.setPlainText("unsaved")
        wizard.reject()
        self.assertEqual(decisions, ["asked"])
        self.assertFalse(wizard._saved)
        wizard.confirm_close = lambda: True
        wizard.reject()
        self.assertEqual(self.context.benchmarks.runs.list(), [])


if __name__ == "__main__":
    unittest.main()
