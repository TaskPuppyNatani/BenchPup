from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPlainTextEdit

from engine.domain import BenchmarkRun, ModelProfile, ReviewScore
from engine.reporting import BenchmarkRunAggregate
from gui.context import GuiApplicationContext
from gui.views.run_details import RunDetailsDialog, _format_bool


class GuiRunDetailsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-run-details-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.context = GuiApplicationContext.create(database_path=Path(self.directory.name) / "data" / "benchmark.db")

    def tearDown(self) -> None:
        self.context.close()
        self.directory.cleanup()

    def test_complete_aggregate_display_preserves_unicode_long_text_and_historical_snapshots(self) -> None:
        current = self.context.catalog.model_profiles.create(ModelProfile(name="Current profile", model_name="Current"))
        prompt = "Révise ce code — keep every character\n" + ("prompt line\n" * 80)
        output = "結果: " + ("raw output line\n" * 120)
        saved, _ = self.context.benchmarks.save_run(
            BenchmarkRun(
                raw_model_output=output,
                prompt_name="Unicode prompt",
                prompt_text=prompt,
                model_profile_id=current.id,
                model_snapshot={"model_name": "Historical model", "backend": "Historical backend", "tokens_per_second": 0.0, "thinking_enabled": False, "context_length": 4096},
                benchmark_snapshot={"name": "Historical benchmark", "file_path": "old.py"},
                prompt_snapshot={"name": "Historical prompt", "prompt_text": prompt},
                hardware_snapshot={"name": "Historical rig", "gpu": "Old GPU"},
            ),
            ReviewScore(
                run_id=0,
                accuracy_score=0.0,
                hallucination_level="Low",
                reliability_level="High",
                depth_score=3.0,
                signal_noise_score=2.0,
                actionability_score=4.0,
                seniority_score=1.0,
                overall_score=0.0,
                strengths="Useful",
                weaknesses="None",
                verdict="Keep",
                notes="Long note",
            ),
        )
        self.assertIsNotNone(saved.id)
        dialog = RunDetailsDialog(self.context, saved.id or 0)
        self.assertEqual(dialog.prompt_text.toPlainText(), prompt)
        self.assertEqual(dialog.raw_output.toPlainText(), output)
        self.assertTrue(dialog.prompt_text.isReadOnly())
        self.assertTrue(dialog.raw_output.isReadOnly())
        snapshots = dialog.findChild(QPlainTextEdit, "historicalSnapshots")
        self.assertIsNotNone(snapshots)
        snapshot_text = snapshots.toPlainText()  # type: ignore[union-attr]
        self.assertIn("Historical model", snapshot_text)
        self.assertIn("Historical benchmark", snapshot_text)
        self.assertIn("Historical rig", snapshot_text)
        self.assertNotIn("Current profile", snapshot_text)
        self.assertIn("0.00 / 5", [field.text() for field in dialog.findChildren(QLineEdit)])

        dialog.copy_prompt()
        self.assertEqual(QApplication.clipboard().text(), prompt)
        dialog.copy_output()
        self.assertEqual(QApplication.clipboard().text(), output)
        dialog.show()
        self.application.processEvents()
        self.assertTrue(dialog.close())

    def test_run_without_review_or_relationships_has_neutral_empty_states(self) -> None:
        saved, _ = self.context.benchmarks.save_run(
            BenchmarkRun(raw_model_output="output", prompt_text="prompt")
        )
        dialog = RunDetailsDialog(self.context, saved.id or 0)
        self.assertTrue(any("No review score was recorded" in label.text() for label in dialog.findChildren(QLabel)))
        self.assertIn("Not recorded", [field.text() for field in dialog.findChildren(QLineEdit)])
        self.assertEqual(dialog.aggregate.run.session_id, None)

    def test_malformed_optional_snapshot_data_is_rendered_safely(self) -> None:
        run = BenchmarkRun(
            raw_model_output="legacy output",
            prompt_text="legacy prompt",
            model_snapshot=["legacy model"],  # type: ignore[arg-type]
            benchmark_snapshot="legacy benchmark",  # type: ignore[arg-type]
            prompt_snapshot=None,  # type: ignore[arg-type]
            hardware_snapshot=object(),  # type: ignore[arg-type]
            id=88,
        )
        dialog = RunDetailsDialog(self.context, 88, aggregate=BenchmarkRunAggregate(run))
        snapshots = dialog.findChild(QPlainTextEdit, "historicalSnapshots")
        self.assertIsNotNone(snapshots)
        self.assertIn("legacy_value", snapshots.toPlainText())  # type: ignore[union-attr]
        dialog.copy_prompt()
        self.assertEqual(QApplication.clipboard().text(), "legacy prompt")

    def test_malformed_persisted_snapshot_json_is_rendered_without_rewriting(self) -> None:
        saved, _ = self.context.benchmarks.save_run(BenchmarkRun(raw_model_output="output", prompt_text="prompt"))
        raw_values = ("{malformed-model", "{malformed-benchmark", "{malformed-prompt", "{malformed-hardware")
        with self.context.database.connection() as connection:
            connection.execute(
                "UPDATE benchmark_runs SET model_snapshot = ?, benchmark_snapshot = ?, prompt_snapshot = ?, hardware_snapshot = ? WHERE id = ?",
                (*raw_values, saved.id),
            )

        dialog = RunDetailsDialog(self.context, saved.id or 0)
        snapshots = dialog.findChild(QPlainTextEdit, "historicalSnapshots")
        self.assertIsNotNone(snapshots)
        snapshot_text = snapshots.toPlainText()  # type: ignore[union-attr]
        for raw_value in raw_values:
            self.assertIn(raw_value, snapshot_text)

        with self.context.database.connection() as connection:
            persisted = connection.execute(
                "SELECT model_snapshot, benchmark_snapshot, prompt_snapshot, hardware_snapshot FROM benchmark_runs WHERE id = ?",
                (saved.id,),
            ).fetchone()
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(tuple(persisted), raw_values)

    def test_run_details_thinking_mode_accepts_legacy_integer_values(self) -> None:
        self.assertEqual(_format_bool(1), "Enabled")
        self.assertEqual(_format_bool(0), "Disabled")
        self.assertEqual(_format_bool("1"), "Not recorded")

    def test_copy_empty_text_reports_friendly_status(self) -> None:
        dialog = RunDetailsDialog(
            self.context,
            1,
            aggregate=BenchmarkRunAggregate(BenchmarkRun(raw_model_output="", prompt_text="", id=1)),
        )
        dialog.copy_prompt()
        self.assertIn("No prompt text", dialog.copy_status.text())
        dialog.copy_output()
        self.assertIn("No output text", dialog.copy_status.text())


if __name__ == "__main__":
    unittest.main()
