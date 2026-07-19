from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from engine.domain import BenchmarkRun, BenchmarkSession, ModelProfile, ReviewScore, ScoreboardEntry
from gui.context import GuiApplicationContext
from gui.views.dashboard import DashboardView


class GuiDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-dashboard-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.context = GuiApplicationContext.create(
            database_path=Path(self.directory.name) / "data" / "benchmark.db"
        )

    def tearDown(self) -> None:
        self.context.close()
        self.directory.cleanup()

    def make_run(
        self,
        *,
        model: str,
        benchmark: str,
        created_at: str,
        speed: float | None,
        score: float | None,
    ) -> int:
        run = BenchmarkRun(
            raw_model_output="output",
            model_snapshot={"model_name": model, "tokens_per_second": speed},
            benchmark_snapshot={"name": benchmark, "benchmark_type": "code_review"},
            created_at=created_at,
        )
        review = (
            ReviewScore(
                run_id=0,
                overall_score=score,
                hallucination_level="Low",
                reliability_level="High",
            )
            if score is not None
            else None
        )
        saved, _ = self.context.benchmarks.save_run(run, review)
        assert saved.id is not None
        return saved.id

    def test_empty_database_has_readable_empty_state_and_unavailable_average(self) -> None:
        view = DashboardView(self.context)
        self.assertEqual(view.summary_cards["benchmark_run_count"].value_label.text(), "0")
        self.assertEqual(view.summary_cards["scored_run_count"].value_label.text(), "0")
        self.assertEqual(view.summary_cards["average_overall_score"].value_label.text(), "Unavailable")
        self.assertFalse(view.empty_state.isHidden())
        self.assertEqual(view.table_model.rowCount(), 0)
        self.assertEqual(view.refresh_status.text(), "Updated")
        view.deleteLater()

    def test_populated_summary_and_recent_runs_use_typed_engine_data(self) -> None:
        self.context.catalog.model_profiles.create(ModelProfile(name="Alpha profile", model_name="Alpha"))
        self.context.catalog.sessions.create(BenchmarkSession(title="July session"))
        self.context.catalog.scoreboard_entries.create(ScoreboardEntry(model_name="Alpha", score=3.5))
        self.make_run(
            model="Older",
            benchmark="Older benchmark",
            created_at="2026-07-10T12:00:00+00:00",
            speed=88.0,
            score=4.0,
        )
        self.make_run(
            model="Newest",
            benchmark="Newest benchmark",
            created_at="2026-07-12T12:00:00+00:00",
            speed=None,
            score=2.5,
        )
        self.make_run(
            model="Unscored",
            benchmark="Unscored benchmark",
            created_at="2026-07-11T12:00:00+00:00",
            speed=120.0,
            score=None,
        )

        view = DashboardView(self.context)
        self.assertEqual(view.summary_cards["benchmark_run_count"].value_label.text(), "3")
        self.assertEqual(view.summary_cards["scored_run_count"].value_label.text(), "2")
        self.assertEqual(view.summary_cards["model_count"].value_label.text(), "1")
        self.assertEqual(view.summary_cards["session_count"].value_label.text(), "1")
        self.assertEqual(view.summary_cards["scoreboard_entry_count"].value_label.text(), "1")
        self.assertEqual(view.summary_cards["average_overall_score"].value_label.text(), "3.25 / 5")
        self.assertEqual(view.table_model.data(view.table_model.index(0, 1), Qt.ItemDataRole.DisplayRole), "Newest")
        self.assertEqual(view.table_model.data(view.table_model.index(1, 1), Qt.ItemDataRole.DisplayRole), "Unscored")
        self.assertTrue(view.empty_state.isHidden())
        view.deleteLater()

    def test_missing_score_and_speed_are_not_rendered_as_zero(self) -> None:
        self.make_run(
            model="No speed",
            benchmark="Benchmark A",
            created_at="2026-07-12T12:00:00+00:00",
            speed=None,
            score=4.0,
        )
        self.make_run(
            model="No score",
            benchmark="Benchmark B",
            created_at="2026-07-11T12:00:00+00:00",
            speed=100.0,
            score=None,
        )
        view = DashboardView(self.context)
        no_speed = view.table_model.data(view.table_model.index(0, 4), Qt.ItemDataRole.DisplayRole)
        no_score = view.table_model.data(view.table_model.index(1, 3), Qt.ItemDataRole.DisplayRole)
        self.assertEqual(no_speed, "Unavailable")
        self.assertEqual(no_score, "Unavailable")
        self.assertNotIn("0", str(no_speed))
        self.assertNotIn("0.00", str(no_score))
        view.deleteLater()

    def test_refresh_rereads_and_does_not_write_or_mutate_source_records(self) -> None:
        run_id = self.make_run(
            model="Stable",
            benchmark="Stable benchmark",
            created_at="2026-07-12T12:00:00+00:00",
            speed=100.0,
            score=4.0,
        )
        before = self.context.benchmarks.runs.get(run_id)
        before_rows = len(self.context.benchmarks.runs.list())
        view = DashboardView(self.context)
        original_load = view.provider.load
        view.provider.load = Mock(wraps=original_load)  # type: ignore[method-assign]
        view.refresh()
        view.provider.load.assert_called_once_with()
        self.assertEqual(len(self.context.benchmarks.runs.list()), before_rows)
        self.assertEqual(self.context.benchmarks.runs.get(run_id), before)
        view.deleteLater()

    def test_refresh_failure_is_friendly_and_logged(self) -> None:
        view = DashboardView(self.context)
        view.provider.load = Mock(side_effect=RuntimeError("read failed"))  # type: ignore[method-assign]
        view.refresh()
        self.assertIn("could not be refreshed", view.empty_state.text())
        self.assertEqual(view.refresh_status.text(), "Refresh failed")
        self.assertTrue(self.context.paths.log_path.exists())
        self.assertIn("Dashboard refresh failed", self.context.paths.log_path.read_text(encoding="utf-8"))
        view.deleteLater()


if __name__ == "__main__":
    unittest.main()
