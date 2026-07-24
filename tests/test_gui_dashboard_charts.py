from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication, QGridLayout, QWidget

from engine.domain import BenchmarkRun, ReviewScore, ScoreboardEntry
from gui.context import GuiApplicationContext
from gui.views.dashboard import DashboardView
from gui.widgets.statistics_charts import StatisticsChartPanel, format_chart_value


class GuiDashboardChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-dashboard-chart-tests"])

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
        score: float | None,
        speed: float | None,
        created_at: str,
        hallucination: str = "Low",
        reliability: str = "High",
        with_review: bool = True,
        hardware: dict[str, object] | None = None,
    ) -> int:
        run = BenchmarkRun(
            raw_model_output="output",
            model_snapshot={"model_name": model, "tokens_per_second": speed},
            benchmark_snapshot={"name": benchmark, "benchmark_type": "code_review"},
            hardware_snapshot=hardware or {},
            created_at=created_at,
        )
        review = (
            ReviewScore(
                run_id=0,
                overall_score=score,
                hallucination_level=hallucination,
                reliability_level=reliability,
            )
            if with_review
            else None
        )
        saved, _ = self.context.benchmarks.save_run(run, review)
        assert saved.id is not None
        return saved.id

    def add_scoreboard(
        self,
        *,
        model: str,
        score: float | None,
        speed: float | None,
        imported_at: str,
        hallucination: str = "Low",
        consistency: str = "High",
        reliability: str = "Medium",
    ) -> int:
        saved = self.context.catalog.scoreboard_entries.create(
            ScoreboardEntry(
                model_name=model,
                score=score,
                tokens_per_second=speed,
                imported_at=imported_at,
                hallucination_level=hallucination,
                consistency=consistency,
                reliability_score=reliability,
            )
        )
        assert saved.id is not None
        return saved.id

    @staticmethod
    def bar_values(chart: object) -> tuple[float, ...]:
        series = chart.chart.series()  # type: ignore[attr-defined]
        if not series:
            return ()
        bar_set = series[0].barSets()[0]
        return tuple(float(bar_set.at(index)) for index in range(bar_set.count()))

    def close_view(self, view: DashboardView) -> None:
        view.close()
        view.deleteLater()
        QApplication.processEvents()

    @staticmethod
    def grid_position(layout: QGridLayout, widget: QWidget) -> tuple[int, int, int, int]:
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item is not None and item.widget() is widget:
                return layout.getItemPosition(index)
        raise AssertionError("widget is not in the chart grid")

    def test_chart_values_use_readable_decimal_format_without_mutation(self) -> None:
        self.assertEqual(format_chart_value(600.0), "600")
        self.assertEqual(format_chart_value(100.0), "100")
        self.assertEqual(format_chart_value(90.0), "90")
        self.assertEqual(format_chart_value(562.5), "562.5")
        self.assertEqual(format_chart_value(0.25), "0.25")
        self.assertEqual(format_chart_value(1e20), "100000000000000000000")
        self.assertEqual(format_chart_value(-0.0), "0")

        panel = StatisticsChartPanel("Formatting")
        panel.set_bars(
            (("Six hundred", 600.0), ("Decimal", 562.5), ("Fraction", 0.25)),
            value_title="Mean",
            empty_message="No data",
        )
        bar_set = panel.chart.series()[0].barSets()[0]
        self.assertEqual(
            tuple(bar_set.at(index) for index in range(bar_set.count())),
            (600.0, 562.5, 0.25),
        )
        self.assertEqual(panel.chart.series()[0].labelsPrecision(), 12)
        self.assertNotIn("e+", panel.chart_view.toolTip())
        self.assertIn("Six hundred: 600", panel.chart_view.toolTip())
        self.assertIn("Decimal: 562.5", panel.chart_view.toolTip())

    def test_chart_layout_and_filter_geometry_are_readable(self) -> None:
        self.add_scoreboard(
            model="Long model label for chart accessibility",
            score=4.0,
            speed=100.0,
            imported_at="2026-07-12T12:00:00+00:00",
        )
        view = DashboardView(self.context)
        self.assertEqual(
            self.grid_position(view.scoreboard_chart_grid, view.scoreboard_score_chart),
            (0, 0, 1, 1),
        )
        self.assertEqual(
            self.grid_position(view.scoreboard_chart_grid, view.scoreboard_speed_chart),
            (0, 1, 1, 1),
        )
        self.assertEqual(
            self.grid_position(view.scoreboard_chart_grid, view.scoreboard_hallucination_chart),
            (1, 0, 1, 1),
        )
        self.assertEqual(
            self.grid_position(view.scoreboard_chart_grid, view.scoreboard_consistency_chart),
            (1, 1, 1, 1),
        )
        self.assertEqual(
            self.grid_position(view.scoreboard_chart_grid, view.scoreboard_reliability_chart),
            (2, 0, 1, 2),
        )
        self.assertGreaterEqual(view.scoreboard_score_chart.minimumHeight(), 300)
        self.assertGreaterEqual(view.scoreboard_score_chart.chart_view.minimumHeight(), 260)
        self.assertGreaterEqual(view.scoreboard_filter_panel.layout().horizontalSpacing(), 10)
        self.assertGreaterEqual(view.scoreboard_filter_panel.date_range.from_date.minimumWidth(), 112)
        self.assertGreaterEqual(view.scoreboard_filter_panel.date_range.to_date.minimumWidth(), 112)
        self.assertGreaterEqual(view.scoreboard_filter_panel.date_range.from_check.minimumWidth(), 52)
        self.assertGreaterEqual(view.scoreboard_filter_panel.date_range.to_check.minimumWidth(), 42)
        self.assertGreaterEqual(view.scoreboard_score_chart.chart.titleFont().pointSize(), 11)
        self.assertTrue(view.scoreboard_score_chart.chart_view.accessibleName())

        view.scoreboard_filter_panel.date_range.from_check.setChecked(True)
        self.assertTrue(view.scoreboard_filter_panel.date_range.from_date.isEnabled())
        view.scoreboard_filter_panel.date_range.to_check.setChecked(True)
        self.assertTrue(view.scoreboard_filter_panel.date_range.to_date.isEnabled())
        view.scoreboard_filter_panel.date_range.from_check.setChecked(False)
        view.scoreboard_filter_panel.date_range.to_check.setChecked(False)
        self.assertFalse(view.scoreboard_filter_panel.date_range.from_date.isEnabled())
        self.assertFalse(view.scoreboard_filter_panel.date_range.to_date.isEnabled())

        view.resize(760, 600)
        QApplication.processEvents()
        self.assertTrue(view.visualization_tabs.isWidgetType())
        self.assertTrue(view.scoreboard_filter_panel.isWidgetType())
        self.close_view(view)

    def test_visualization_tabs_and_native_charts_are_created_once(self) -> None:
        self.make_run(
            model="Historical model with a deliberately long snapshot label",
            benchmark="Benchmark",
            score=4.0,
            speed=100.0,
            created_at="2026-07-12T12:00:00+00:00",
        )
        self.add_scoreboard(
            model="Scoreboard Alpha",
            score=4.5,
            speed=120.0,
            imported_at="2026-07-12T12:00:00+00:00",
        )
        view = DashboardView(self.context)
        chart_objects = (
            view.benchmark_score_chart,
            view.benchmark_speed_chart,
            view.scoreboard_score_chart,
            view.scoreboard_speed_chart,
        )
        chart_ids = tuple(id(chart) for chart in chart_objects)
        native_chart_ids = tuple(id(chart.chart) for chart in chart_objects)

        self.assertEqual(view.visualization_tabs.count(), 2)
        self.assertEqual(view.visualization_tabs.tabText(0), "Benchmark Runs")
        self.assertEqual(view.visualization_tabs.tabText(1), "Scoreboard")
        self.assertEqual(view.benchmark_score_chart.labels, ("Historical model with a deliberately long snapshot label",))
        self.assertTrue(view.benchmark_score_chart.chart.title())
        self.assertEqual(len(view.benchmark_score_chart.chart.axes()), 2)
        self.assertTrue(view.benchmark_score_chart.chart_view.accessibleName())
        self.assertIn("Historical model with a deliberately long snapshot label", view.benchmark_score_chart.chart_view.toolTip())
        self.assertEqual(self.bar_values(view.benchmark_score_chart), (4.0,))
        self.assertEqual(self.bar_values(view.scoreboard_score_chart), (4.5,))

        view.refresh()
        view.refresh()
        self.assertEqual(chart_ids, tuple(id(chart) for chart in chart_objects))
        self.assertEqual(native_chart_ids, tuple(id(chart.chart) for chart in chart_objects))
        self.assertEqual(len(view.benchmark_score_chart.chart.series()), 1)
        self.assertEqual(len(view.benchmark_score_chart.chart.axes()), 2)
        self.close_view(view)

    def test_source_separation_and_typed_filters_update_only_related_charts(self) -> None:
        self.make_run(
            model="Benchmark Alpha",
            benchmark="Benchmark A",
            score=4.0,
            speed=100.0,
            created_at="2026-07-10T12:00:00+00:00",
            hardware={"name": "Rig A"},
        )
        self.make_run(
            model="Benchmark Beta",
            benchmark="Benchmark B",
            score=2.0,
            speed=80.0,
            created_at="2026-07-12T12:00:00+00:00",
            hardware={"name": "Rig B"},
        )
        self.add_scoreboard(
            model="Scoreboard Alpha",
            score=4.5,
            speed=120.0,
            imported_at="2026-07-10T12:00:00+00:00",
        )
        self.add_scoreboard(
            model="Scoreboard Beta",
            score=3.0,
            speed=90.0,
            imported_at="2026-07-12T12:00:00+00:00",
        )
        view = DashboardView(self.context)
        benchmark_labels = view.benchmark_score_chart.labels
        scoreboard_labels = view.scoreboard_score_chart.labels

        view.scoreboard_filter_panel.model_edit.setText("Scoreboard Alpha")
        self.assertEqual(view.scoreboard_score_chart.labels, ("Scoreboard Alpha",))
        self.assertEqual(view.benchmark_score_chart.labels, benchmark_labels)
        self.assertEqual(view.scoreboard_filter_panel.filters().model, "Scoreboard Alpha")

        view.benchmark_filter_panel.model_edit.setText("Benchmark Beta")
        self.assertEqual(view.benchmark_score_chart.labels, ("Benchmark Beta",))
        self.assertEqual(view.scoreboard_score_chart.labels, ("Scoreboard Alpha",))
        self.assertEqual(view.benchmark_filter_panel.filters().model, "Benchmark Beta")

        view.benchmark_filter_panel.model_edit.clear()
        view.benchmark_filter_panel.benchmark_edit.setText("Benchmark A")
        self.assertEqual(view.benchmark_score_chart.labels, ("Benchmark Alpha",))
        self.assertEqual(view.benchmark_filter_panel.filters().benchmark, "Benchmark A")

        view.benchmark_filter_panel.benchmark_edit.clear()
        view.benchmark_filter_panel.hardware_edit.setText("Rig B")
        self.assertEqual(view.benchmark_score_chart.labels, ("Benchmark Beta",))
        self.assertEqual(view.benchmark_filter_panel.filters().hardware, "Rig B")

        view.benchmark_filter_panel.date_range.from_check.setChecked(True)
        view.benchmark_filter_panel.date_range.from_date.setDate(QDate(2026, 7, 11))
        self.assertEqual(view.benchmark_filter_panel.filters().date_from.isoformat(), "2026-07-11")
        self.assertEqual(view.scoreboard_score_chart.labels, ("Scoreboard Alpha",))

        view.scoreboard_filter_panel.model_edit.clear()
        view.scoreboard_filter_panel.date_range.from_check.setChecked(True)
        view.scoreboard_filter_panel.date_range.from_date.setDate(QDate(2026, 7, 11))
        self.assertEqual(view.scoreboard_filter_panel.filters().date_from.isoformat(), "2026-07-11")
        self.assertEqual(view.scoreboard_score_chart.labels, ("Scoreboard Beta",))
        self.assertNotEqual(view.scoreboard_score_chart.labels, scoreboard_labels)
        self.close_view(view)

    def test_distributions_and_missing_values_do_not_become_zero(self) -> None:
        self.make_run(
            model="With review",
            benchmark="Benchmark A",
            score=4.0,
            speed=100.0,
            created_at="2026-07-12T12:00:00+00:00",
            hallucination="Low",
            reliability="High",
        )
        self.make_run(
            model="Without score",
            benchmark="Benchmark B",
            score=None,
            speed=None,
            created_at="2026-07-11T12:00:00+00:00",
            with_review=False,
        )
        self.add_scoreboard(
            model="Scoreboard",
            score=None,
            speed=None,
            imported_at="2026-07-12T12:00:00+00:00",
            hallucination="",
            consistency="",
            reliability="",
        )
        view = DashboardView(self.context)
        self.assertEqual(self.bar_values(view.benchmark_score_chart), (4.0,))
        self.assertEqual(self.bar_values(view.benchmark_speed_chart), (100.0,))
        self.assertIn("unavailable", view.benchmark_score_chart.status.text())
        self.assertEqual(self.bar_values(view.benchmark_hallucination_chart), (1.0,))
        self.assertEqual(view.scoreboard_score_chart.labels, ())
        self.assertIn("No Scoreboard score data is available", view.scoreboard_score_chart.placeholder.text())
        self.assertEqual(self.bar_values(view.scoreboard_score_chart), ())
        self.close_view(view)

    def test_empty_and_no_match_states_are_explicit(self) -> None:
        view = DashboardView(self.context)
        self.assertFalse(view.benchmark_score_chart.has_data)
        self.assertIn("No BenchmarkRun score data is available", view.benchmark_score_chart.placeholder.text())
        self.assertFalse(view.scoreboard_score_chart.has_data)
        self.assertIn("No Scoreboard score data is available", view.scoreboard_score_chart.placeholder.text())

        view.scoreboard_filter_panel.model_edit.setText("does-not-exist")
        self.assertFalse(view.scoreboard_score_chart.has_data)
        self.assertEqual(view.scoreboard_score_chart.placeholder.text(), "No records match the selected filters.")
        self.close_view(view)

    def test_filters_refresh_read_only_data_and_preserve_historical_snapshot_labels(self) -> None:
        run_id = self.make_run(
            model="Historical snapshot",
            benchmark="Benchmark",
            score=4.0,
            speed=100.0,
            created_at="2026-07-12T12:00:00+00:00",
        )
        entry_id = self.add_scoreboard(
            model="Scoreboard",
            score=4.5,
            speed=120.0,
            imported_at="2026-07-12T12:00:00+00:00",
        )
        before_run = copy.deepcopy(self.context.benchmarks.runs.get(run_id))
        before_entry = copy.deepcopy(self.context.catalog.scoreboard_entries.get(entry_id))
        before_runs = tuple(self.context.benchmarks.runs.list())
        before_entries = tuple(self.context.catalog.scoreboard_entries.list())

        view = DashboardView(self.context)
        view.refresh()
        view.benchmark_filter_panel.model_edit.setText("Historical snapshot")
        view.scoreboard_filter_panel.model_edit.setText("Scoreboard")

        self.assertEqual(view.benchmark_score_chart.labels, ("Historical snapshot",))
        self.assertEqual(view.scoreboard_score_chart.labels, ("Scoreboard",))
        self.assertEqual(tuple(self.context.benchmarks.runs.list()), before_runs)
        self.assertEqual(tuple(self.context.catalog.scoreboard_entries.list()), before_entries)
        self.assertEqual(self.context.benchmarks.runs.get(run_id), before_run)
        self.assertEqual(self.context.catalog.scoreboard_entries.get(entry_id), before_entry)
        self.close_view(view)


if __name__ == "__main__":
    unittest.main()
