from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cli import QuitApplication, TerminalApp
from engine.html_reporting import AnalyticsSourceFamily, HtmlAnalyticsReportOptions, build_html_analytics_report
from engine.reporting import BenchmarkRunAggregate, ReportWriteResult, ReportWriteStatus, ReportingService
from engine.domain import BenchmarkRun, ReviewScore
from engine.statistics import TimeBucketGranularity


class InputQueue:
    def __init__(self, values: list[str]) -> None:
        self.values = iter(values)

    def __call__(self, _prompt: str = "") -> str:
        return next(self.values)


class HtmlReportingCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.outputs: list[str] = []

    def tearDown(self) -> None:
        self.directory.cleanup()

    def app(self, inputs: list[str]) -> TerminalApp:
        return TerminalApp(
            Path(self.directory.name) / "cli.db",
            input_fn=InputQueue(inputs),
            output_fn=self.outputs.append,
        )

    def report(self, app: TerminalApp):
        aggregate = BenchmarkRunAggregate(
            BenchmarkRun(
                id=1,
                raw_model_output="private output",
                model_snapshot={"model_name": "Alpha", "tokens_per_second": 100.0},
                benchmark_snapshot={"name": "Review", "benchmark_type": "code_review"},
                created_at="2026-07-01T00:00:00+00:00",
            ),
            ReviewScore(run_id=1, overall_score=4.0),
        )
        return build_html_analytics_report(app.benchmarks, app.catalog, runs=[aggregate], generated_at="fixed")

    def test_export_screen_adds_html_analytics_without_changing_legacy_menu(self) -> None:
        app = self.app(["6", "b"])
        app.export_screen()
        rendered = "\n".join(self.outputs)
        self.assertIn("5) Scoreboard HTML", rendered)
        self.assertIn("6) HTML Analytics Report", rendered)
        self.assertIn("HTML Analytics Report", rendered)

    def test_source_selection_is_vertical_and_keeps_families_separate(self) -> None:
        app = self.app(["1", "1", "2", "b", "b"])
        options = app._html_analytics_options_screen(HtmlAnalyticsReportOptions())
        self.assertEqual(options.source_family, AnalyticsSourceFamily.SCOREBOARD)
        self.assertFalse(options.include_benchmark_run_dashboard)
        self.assertTrue(options.include_scoreboard_dashboard)

    def test_trend_interval_propagates_as_typed_option(self) -> None:
        app = self.app(["4", "2", "b"])
        options = app._html_analytics_options_screen(HtmlAnalyticsReportOptions())
        self.assertEqual(options.trend_interval, TimeBucketGranularity.WEEK)

    def test_typed_preview_uses_engine_metadata_and_does_not_write(self) -> None:
        app = self.app(["2", "b", "b"])
        report = self.report(app)
        fake = Mock(spec=ReportingService)
        fake.html_analytics_report.return_value = report
        app.reporting = fake

        app._html_analytics_screen(HtmlAnalyticsReportOptions())

        fake.html_analytics_report.assert_called_once()
        fake.write_html_analytics_report.assert_not_called()
        rendered = "\n".join(self.outputs)
        self.assertIn("Contributing records: 1", rendered)
        self.assertIn("Model quality", rendered)
        self.assertNotIn("private output", rendered)

    def test_successful_write_uses_staged_html_writer_and_shows_path(self) -> None:
        app = self.app(["3", str(Path(self.directory.name) / "analytics"), "y", "b", "b"])
        report = self.report(app)
        fake = Mock(spec=ReportingService)
        fake.html_analytics_report.return_value = report
        destination = Path(self.directory.name) / "analytics.html"
        fake.write_html_analytics_report.return_value = ReportWriteResult(ReportWriteStatus.SUCCESS, destination, "written")
        app.reporting = fake

        app._html_analytics_screen(HtmlAnalyticsReportOptions())

        fake.write_html_analytics_report.assert_called_once_with(report, destination)
        self.assertIn("HTML Analytics Write Complete", "\n".join(self.outputs))
        self.assertIn(str(destination), "\n".join(self.outputs))

    def test_overwrite_declined_does_not_retry(self) -> None:
        destination = Path(self.directory.name) / "analytics.html"
        app = self.app(["3", str(destination), "y", "n", "b", "b"])
        report = self.report(app)
        fake = Mock(spec=ReportingService)
        fake.html_analytics_report.return_value = report
        fake.write_html_analytics_report.return_value = ReportWriteResult(
            ReportWriteStatus.OVERWRITE_REQUIRED,
            destination,
            "existing",
        )
        app.reporting = fake

        app._html_analytics_screen(HtmlAnalyticsReportOptions())

        fake.write_html_analytics_report.assert_called_once_with(report, destination)
        self.assertIn("no replacement was written", "\n".join(self.outputs).lower())

    def test_qa_quits_globally_from_html_analytics(self) -> None:
        app = self.app(["qa"])
        with self.assertRaises(QuitApplication):
            app._html_analytics_screen(HtmlAnalyticsReportOptions())

    def test_expected_engine_error_is_friendly(self) -> None:
        app = self.app(["2", "b", "b"])
        fake = Mock(spec=ReportingService)
        fake.html_analytics_report.side_effect = ValueError("no eligible records")
        app.reporting = fake

        app._html_analytics_screen(HtmlAnalyticsReportOptions())

        rendered = "\n".join(self.outputs)
        self.assertIn("HTML Analytics Failed", rendered)
        self.assertIn("no eligible records", rendered)
        self.assertNotIn("Traceback", rendered)


if __name__ == "__main__":
    unittest.main()
