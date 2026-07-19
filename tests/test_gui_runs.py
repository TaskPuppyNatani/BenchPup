from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from engine.domain import BenchmarkDefinition, BenchmarkRun, BenchmarkSession, ModelProfile, ReviewScore
from gui.context import GuiApplicationContext
from gui.main_window import MainWindow
from gui.models.run_table_model import ROW_ROLE
from gui.views.runs import RunsView


class GuiRunsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-runs-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.context = GuiApplicationContext.create(database_path=Path(self.directory.name) / "data" / "benchmark.db")
        self.model_alpha = self.context.catalog.model_profiles.create(
            ModelProfile(name="Alpha profile", model_name="Alpha", tokens_per_second=123.0)
        )
        self.model_beta = self.context.catalog.model_profiles.create(
            ModelProfile(name="Beta profile", model_name="Beta")
        )
        self.benchmark_review = self.context.catalog.benchmark_definitions.create(
            BenchmarkDefinition(name="Code review", file_path="review.py", benchmark_type="code_review")
        )
        self.benchmark_generation = self.context.catalog.benchmark_definitions.create(
            BenchmarkDefinition(name="Code generation", file_path="generate.py", benchmark_type="code_generation")
        )
        self.session = self.context.catalog.sessions.create(
            BenchmarkSession(title="July session")
        )
        self.scored_run, _ = self.context.benchmarks.save_run(
            BenchmarkRun(
                raw_model_output="alpha output",
                prompt_name="Review prompt",
                session_id=self.session.id,
                model_profile_id=self.model_alpha.id,
                benchmark_definition_id=self.benchmark_review.id,
            ),
            ReviewScore(run_id=0, accuracy_score=0.0, overall_score=4.5),
        )
        self.unscored_run, _ = self.context.benchmarks.save_run(
            BenchmarkRun(
                raw_model_output="beta output",
                prompt_name="Generation prompt",
                model_profile_id=self.model_beta.id,
                benchmark_definition_id=self.benchmark_generation.id,
            )
        )
        same_time = "2026-07-18T12:00:00+00:00"
        self.scored_run = self.context.benchmarks.runs.update(replace(self.scored_run, created_at=same_time))
        self.unscored_run = self.context.benchmarks.runs.update(replace(self.unscored_run, created_at=same_time))
        self.deleted_run, _ = self.context.benchmarks.save_run(BenchmarkRun(raw_model_output="deleted output"))
        self.context.benchmarks.delete_run(self.deleted_run.id)

    def tearDown(self) -> None:
        self.context.close()
        self.directory.cleanup()

    def test_populated_runs_are_newest_first_with_id_tiebreak_and_soft_deleted_excluded(self) -> None:
        view = RunsView(self.context)
        self.assertEqual([row.run_id for row in view.rows], [self.unscored_run.id, self.scored_run.id])
        self.assertNotIn(self.deleted_run.id, [row.run_id for row in view.rows])
        self.assertEqual(view.result_count.text(), "2 visible of 2 runs")
        self.assertEqual(view.table_model.data(view.table_model.index(0, 5)), "Not recorded")
        self.assertEqual(view.table_model.data(view.table_model.index(0, 9)), "Unavailable")
        self.assertEqual(view.table_model.flags(view.table_model.index(0, 0)) & Qt.ItemFlag.ItemIsEditable, Qt.ItemFlag.NoItemFlags)

    def test_search_and_structured_filters_combine_and_clear(self) -> None:
        view = RunsView(self.context)
        view.search_edit.setText("generation prompt")
        self.assertEqual(view.proxy_model.rowCount(), 1)
        view.search_edit.clear()
        view.model_filter.setCurrentIndex(view.model_filter.findData("Alpha"))
        view.benchmark_filter.setCurrentIndex(view.benchmark_filter.findData("Code review"))
        view.session_filter.setCurrentIndex(view.session_filter.findData("July session"))
        view.score_filter.setCurrentIndex(view.score_filter.findData("scored"))
        self.assertEqual(view.proxy_model.rowCount(), 1)
        self.assertEqual(view.proxy_model.index(0, 0).data(ROW_ROLE).run_id, self.scored_run.id)
        view.clear_filters()
        self.assertEqual(view.proxy_model.rowCount(), 2)
        self.assertIn("2 visible of 2 runs", view.result_count.text())

    def test_no_match_state_is_distinct_from_empty_database(self) -> None:
        view = RunsView(self.context)
        view.search_edit.setText("does not exist")
        self.assertEqual(view.proxy_model.rowCount(), 0)
        self.assertIn("No runs match", view.empty_state.text())

        empty_directory = tempfile.TemporaryDirectory()
        empty_context = GuiApplicationContext.create(database_path=Path(empty_directory.name) / "data" / "benchmark.db")
        try:
            empty_view = RunsView(empty_context)
            self.assertEqual(empty_view.table_model.rowCount(), 0)
            self.assertIn("No benchmark runs", empty_view.empty_state.text())
        finally:
            empty_context.close()
            empty_directory.cleanup()

    def test_refresh_reloads_service_data_and_detail_activation_uses_run_id(self) -> None:
        view = RunsView(self.context)
        view.model_filter.setCurrentIndex(view.model_filter.findData("Alpha"))
        new_run, _ = self.context.benchmarks.save_run(BenchmarkRun(raw_model_output="new output"))
        view.refresh()
        self.assertEqual(view.table_model.rowCount(), 3)
        self.assertEqual(view.result_count.text(), "1 visible of 3 runs")
        self.assertEqual(view.model_filter.currentData(), "Alpha")
        view.clear_filters()

        fake_dialog = Mock()
        with patch("gui.views.runs.RunDetailsDialog", return_value=fake_dialog) as dialog_type:
            view.runs_table.activated.emit(view.proxy_model.index(0, 0))
        dialog_type.assert_called_once_with(self.context, new_run.id, view)
        fake_dialog.exec.assert_called_once_with()

    def test_main_window_reuses_functional_runs_page(self) -> None:
        window = MainWindow(self.context)
        try:
            runs = window.runs
            window.navigate_to("runs")
            self.assertIs(window.page_stack.currentWidget(), runs)
            self.assertIs(window.pages["runs"], runs)
            self.assertTrue(window.add_run_button.isEnabled())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
