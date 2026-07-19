from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QDialog, QGroupBox, QLineEdit, QPlainTextEdit, QPushButton

from engine.domain import BenchmarkDefinition, BenchmarkRun, ModelProfile, ReviewScore
from gui.context import GuiApplicationContext
from gui.dialogs.review_editor import ReviewEditorDialog
from gui.views.add_run import AddRunWizard
from gui.views.run_details import RunDetailsDialog


class Phase5C4AEvaluationGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-phase5c4a-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.context = GuiApplicationContext.create(database_path=Path(self.directory.name) / "data" / "benchmark.db")
        self.model = self.context.catalog.model_profiles.create(
            ModelProfile(
                name="Evaluation profile",
                model_name="Evaluation model",
                backend="llama.cpp",
                temperature=0.2,
                top_p=0.9,
                top_k=40,
                min_p=0.05,
                thinking_enabled=True,
                flash_attention=True,
                moe_experts="8",
                quantization="Q4_K_M",
                context_length=8192,
                tokens_per_second=42.5,
                is_default=True,
            )
        )
        self.benchmark = self.context.catalog.benchmark_definitions.create(
            BenchmarkDefinition(name="Evaluation benchmark", file_path="review.py", benchmark_type="code_review")
        )

    def tearDown(self) -> None:
        self.context.close()
        self.directory.cleanup()

    def _save_run(self, score: ReviewScore | None = None) -> BenchmarkRun:
        run, _ = self.context.benchmarks.save_run(
            BenchmarkRun(
                raw_model_output="evaluation output",
                prompt_name="evaluation prompt",
                prompt_text="Review this output.",
                model_profile_id=self.model.id,
                benchmark_definition_id=self.benchmark.id,
            ),
            score,
        )
        return run

    def test_review_editor_creation_exposes_all_review_fields(self) -> None:
        run = self._save_run()
        editor = ReviewEditorDialog(self.context, run.id or 0, confirm_close=lambda: True)

        self.assertIsNone(editor.score)
        self.assertEqual(editor.run_id, run.id)
        self.assertEqual(
            set(editor.score_fields),
            {
                "accuracy_score",
                "depth_score",
                "signal_noise_score",
                "actionability_score",
                "seniority_score",
                "overall_score",
            },
        )
        self.assertEqual(editor.hallucination_combo.currentText(), "Medium")
        self.assertEqual(editor.reliability_combo.currentText(), "Medium")
        self.assertEqual(editor.strengths_edit.toPlainText(), "")
        self.assertEqual(editor.weaknesses_edit.toPlainText(), "")
        self.assertEqual(editor.verdict_edit.text(), "")
        self.assertEqual(editor.notes_edit.toPlainText(), "")

    def test_review_editor_persists_new_and_existing_reviews(self) -> None:
        run = self._save_run()
        editor = ReviewEditorDialog(self.context, run.id or 0, confirm_close=lambda: True)
        editor.score_fields["accuracy_score"].record_checkbox.setChecked(True)
        editor.score_fields["accuracy_score"].spin_box.setValue(4.25)
        editor.score_fields["overall_score"].record_checkbox.setChecked(True)
        editor.score_fields["overall_score"].spin_box.setValue(4.5)
        editor.strengths_edit.setPlainText("Clear explanation")
        editor.verdict_edit.setText("Useful")
        self.assertTrue(editor._save())
        self.assertEqual(editor.result(), QDialog.DialogCode.Accepted)

        persisted_run, persisted_score, _ = self.context.benchmarks.get_run(run.id or 0)
        self.assertIsNotNone(persisted_run)
        self.assertIsNotNone(persisted_score)
        assert persisted_score is not None
        self.assertEqual(persisted_score.accuracy_score, 4.25)
        self.assertEqual(persisted_score.overall_score, 4.5)
        self.assertEqual(persisted_score.strengths, "Clear explanation")

        update = ReviewEditorDialog(self.context, run.id or 0, confirm_close=lambda: True)
        update.score_fields["overall_score"].spin_box.setValue(3.75)
        update.notes_edit.setPlainText("Revised after a second pass")
        self.assertTrue(update._save())
        _, updated_score, _ = self.context.benchmarks.get_run(run.id or 0)
        self.assertIsNotNone(updated_score)
        assert updated_score is not None
        self.assertEqual(updated_score.overall_score, 3.75)
        self.assertEqual(updated_score.notes, "Revised after a second pass")

    def test_add_run_displays_existing_two_state_inference_contract(self) -> None:
        wizard = AddRunWizard(self.context, confirm_close=lambda: True)
        wizard.model_combo.setCurrentIndex(wizard.model_combo.findData(self.model.id))

        self.assertIsNotNone(wizard.findChild(QGroupBox, "inferenceSettings"))
        self.assertEqual(wizard.inference_values["backend"].text(), "llama.cpp")
        self.assertEqual(wizard.inference_values["thinking_enabled"].text(), "Enabled")
        self.assertEqual(wizard.inference_values["temperature"].text(), "0.2")
        self.assertEqual(wizard.inference_values["top_p"].text(), "0.9")
        self.assertEqual(wizard.inference_values["top_k"].text(), "40")
        self.assertEqual(wizard.inference_values["min_p"].text(), "0.05")
        self.assertEqual(wizard.inference_values["context_length"].text(), "8192")
        self.assertEqual(wizard.inference_values["flash_attention"].text(), "Enabled")
        self.assertEqual(wizard.inference_values["moe_experts"].text(), "8")
        self.assertEqual(wizard.inference_values["quantization"].text(), "Q4_K_M")
        self.assertEqual(wizard.inference_values["tokens_per_second"].text(), "42.5 tok/s")

    def test_run_details_displays_inference_review_and_edit_action(self) -> None:
        run = self._save_run(
            ReviewScore(
                run_id=0,
                accuracy_score=4.0,
                hallucination_level="Low",
                reliability_level="High",
                depth_score=3.5,
                signal_noise_score=3.0,
                actionability_score=4.5,
                seniority_score=2.5,
                overall_score=4.25,
                strengths="Useful result",
                weaknesses="Needs more examples",
                verdict="Keep",
                notes="Review note",
            )
        )
        dialog = RunDetailsDialog(self.context, run.id or 0)

        self.assertTrue(any(button.text() == "Edit Review" for button in dialog.findChildren(QPushButton)))
        self.assertTrue(any(group.title() == "Inference settings" for group in dialog.findChildren(QGroupBox)))
        self.assertIn("Enabled", [field.text() for field in dialog.findChildren(QLineEdit)])
        self.assertIn("42.5 tok/s", [field.text() for field in dialog.findChildren(QLineEdit)])
        self.assertTrue(any("Useful result" in field.toPlainText() for field in dialog.findChildren(QPlainTextEdit)))
        self.assertTrue(any("Keep" in field.toPlainText() for field in dialog.findChildren(QPlainTextEdit)))
        self.assertTrue(any("Historical Snapshots" in tab for tab in [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]))
        self.assertEqual(dialog.edit_review_button.accessibleName(), "Edit Review")

    def test_review_edits_do_not_mutate_immutable_run_snapshots(self) -> None:
        run = self._save_run()
        persisted, _, _ = self.context.benchmarks.get_run(run.id or 0)
        assert persisted is not None
        before = {
            "model": copy.deepcopy(persisted.model_snapshot),
            "benchmark": copy.deepcopy(persisted.benchmark_snapshot),
            "prompt": copy.deepcopy(persisted.prompt_snapshot),
            "hardware": copy.deepcopy(persisted.hardware_snapshot),
        }

        editor = ReviewEditorDialog(self.context, run.id or 0, confirm_close=lambda: True)
        editor.score_fields["overall_score"].record_checkbox.setChecked(True)
        editor.score_fields["overall_score"].spin_box.setValue(4.0)
        editor.notes_edit.setPlainText("Only the review changed")
        self.assertTrue(editor._save())

        after, score, _ = self.context.benchmarks.get_run(run.id or 0)
        assert after is not None
        self.assertEqual(after.model_snapshot, before["model"])
        self.assertEqual(after.benchmark_snapshot, before["benchmark"])
        self.assertEqual(after.prompt_snapshot, before["prompt"])
        self.assertEqual(after.hardware_snapshot, before["hardware"])
        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(score.notes, "Only the review changed")


if __name__ == "__main__":
    unittest.main()
