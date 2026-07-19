from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cli import BenchmarkTrendOptions, TerminalApp
from engine.domain import BenchmarkRun, ReviewScore
from engine.reporting import BenchmarkRunAggregate
from engine.trends import TrendGrouping, TrendService
from engine.statistics import TimeBucketGranularity


class InputQueue:
    def __init__(self, values: list[str]) -> None:
        self.values = iter(values)

    def __call__(self, _prompt: str = "") -> str:
        return next(self.values)


class TrendCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "cli.db"
        self.outputs: list[str] = []

    def tearDown(self) -> None:
        self.directory.cleanup()

    def app(self, inputs: list[str]) -> TerminalApp:
        return TerminalApp(self.database_path, input_fn=InputQueue(inputs), output_fn=self.outputs.append)

    @staticmethod
    def report(app: TerminalApp):
        run = BenchmarkRun(
            id=1,
            raw_model_output="private output",
            model_snapshot={"model_name": "Alpha", "tokens_per_second": 100.0},
            benchmark_snapshot={"name": "Review", "benchmark_type": "code_review"},
            hardware_snapshot={"name": "Rig"},
            created_at="2026-07-01T12:00:00+00:00",
        )
        aggregate = BenchmarkRunAggregate(run, ReviewScore(run_id=1, overall_score=3.0))
        return TrendService(app.benchmarks, app.catalog).benchmark_run_trend(
            [aggregate], generated_at="fixed"
        )

    def test_main_menu_exposes_trends_and_back_returns_from_trends_screen(self) -> None:
        app = self.app(["B"])
        app.show_main_menu()
        app.trends_screen()
        combined = "\n".join(self.outputs)
        self.assertIn("19) Trends", combined)
        self.assertIn("Benchmark Run Trends", combined)
        self.assertIn("Historical Scoreboard Trends", combined)

    def test_interval_grouping_and_empty_bucket_options_use_vertical_selectors(self) -> None:
        app = self.app(["2", "2", "3", "2", "5", "1", "B"])
        options = app._benchmark_trend_options_screen(BenchmarkTrendOptions())
        self.assertEqual(options.interval, TimeBucketGranularity.WEEK)
        self.assertEqual(options.grouping, TrendGrouping.MODEL)
        self.assertTrue(options.include_empty_buckets)

    def test_benchmark_preview_passes_typed_options_to_trend_service(self) -> None:
        app = self.app(["3", "B", "B"])
        report = self.report(app)
        fake = Mock()
        fake.benchmark_run_trend.return_value = report
        app.trends = fake

        returned = app._benchmark_trend_screen(
            BenchmarkTrendOptions(
                interval=TimeBucketGranularity.MONTH,
                grouping=TrendGrouping.MODEL,
                include_empty_buckets=True,
            )
        )

        self.assertTrue(returned.include_empty_buckets)
        fake.benchmark_run_trend.assert_called_once()
        kwargs = fake.benchmark_run_trend.call_args.kwargs
        self.assertEqual(kwargs["interval"], TimeBucketGranularity.MONTH)
        self.assertEqual(kwargs["grouping"], TrendGrouping.MODEL)
        self.assertTrue(kwargs["include_empty_buckets"])
        combined = "\n".join(self.outputs)
        self.assertIn("Score delta (last minus first): Not available", combined)
        self.assertNotIn("private output", combined)

    def test_successful_trend_write_uses_existing_staged_writer(self) -> None:
        destination = Path(self.directory.name) / "trend.md"
        app = self.app(["4", str(destination), "y", "B", "B"])
        report = self.report(app)
        fake = Mock()
        fake.benchmark_run_trend.return_value = report
        app.trends = fake

        app._benchmark_trend_screen(BenchmarkTrendOptions())

        self.assertTrue(destination.exists())
        self.assertIn("# Benchmark Run Trends", destination.read_text(encoding="utf-8"))
        self.assertIn("Report Write Complete", "\n".join(self.outputs))


if __name__ == "__main__":
    unittest.main()
