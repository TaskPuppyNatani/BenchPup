from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine.database import EngineDatabase
from engine.domain import BenchmarkRun, BenchmarkSession, ReviewScore, ScoreboardEntry, ScoreboardImportBatch
from engine.html_reporting import (
    AnalyticsSourceFamily,
    ChartPoint,
    HtmlAnalyticsReportOptions,
    build_html_analytics_report,
    render_html_analytics_report,
    write_html_analytics_report,
)
from engine.reporting import BenchmarkRunAggregate, ReportWriteStatus, ScoreboardEntryAggregate
from engine.services import BenchmarkService, CatalogService
from engine.statistics import TimeBucketGranularity


class HtmlReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database = EngineDatabase(Path(self.directory.name) / "analytics.db")
        database.migrate()
        self.catalog = CatalogService(database)
        self.service = BenchmarkService(database, self.catalog)

    def tearDown(self) -> None:
        self.directory.cleanup()

    @staticmethod
    def make_run(
        run_id: int,
        *,
        model: str = "Alpha",
        benchmark: str = "Review",
        score: float | None = 4.0,
        speed: float | None = 100.0,
        created_at: str = "2026-07-01T12:00:00+00:00",
        deleted: bool = False,
        session_id: int | None = 1,
        session_title: str = "July Session",
    ) -> BenchmarkRunAggregate:
        run = BenchmarkRun(
            id=run_id,
            raw_model_output="PRIVATE RAW OUTPUT",
            session_id=session_id,
            model_snapshot={"model_name": model, "tokens_per_second": speed},
            benchmark_snapshot={"name": benchmark, "benchmark_type": "code_review"},
            prompt_snapshot={"prompt_text": "PRIVATE PROMPT"},
            created_at=created_at,
            is_deleted=deleted,
        )
        review = ReviewScore(
            run_id=run_id,
            overall_score=score,
            hallucination_level="Low",
            reliability_level="High",
        )
        session = BenchmarkSession(id=session_id, title=session_title) if session_id is not None else None
        return BenchmarkRunAggregate(run, review, session)

    @staticmethod
    def entry(
        entry_id: int,
        *,
        model: str = "Alpha",
        score: float | None = 4.0,
        speed: float | None = 100.0,
        imported_at: str = "2026-07-01T12:00:00+00:00",
        batch_id: int | None = 1,
        deleted: bool = False,
        notes: str = "Useful",
    ) -> ScoreboardEntryAggregate:
        return ScoreboardEntryAggregate(
            ScoreboardEntry(
                id=entry_id,
                model_name=model,
                score=score,
                tokens_per_second=speed,
                imported_at=imported_at,
                import_batch_id=batch_id,
                hallucination_level="Low",
                consistency="High",
                reliability_score="High",
                notes=notes,
                is_deleted=deleted,
            )
        )

    def test_chart_point_is_immutable_and_metadata_isolated(self) -> None:
        source = {"record_count": 2}
        point = ChartPoint("stable", "Alpha", 4.0, tooltip_metadata=source)
        source["record_count"] = 99
        self.assertEqual(point.stable_key, "stable")
        self.assertEqual(point.tooltip_metadata["record_count"], 2)
        with self.assertRaises(TypeError):
            point.tooltip_metadata["new"] = 1  # type: ignore[index]

    def test_benchmark_dashboard_uses_statistics_and_keeps_missing_values_unavailable(self) -> None:
        records = [
            self.make_run(1, model="Alpha", score=4.0, speed=100.0),
            self.make_run(2, model="Beta", score=None, speed=None, session_id=None),
            self.make_run(3, model="Deleted", score=5.0, deleted=True),
        ]
        original = copy.deepcopy(records)
        report = build_html_analytics_report(self.service, self.catalog, runs=records, generated_at="fixed")
        dashboard = report.dashboards[0]
        self.assertEqual(dashboard.source_record_family, "BenchmarkRun")
        self.assertEqual(dashboard.metadata.contributing_record_count, 2)
        self.assertEqual(dashboard.metadata.scored_record_count, 1)
        self.assertEqual(dashboard.metadata.mean_overall_score, 4.0)
        self.assertEqual(dashboard.metadata.median_overall_score, 4.0)
        self.assertEqual(dashboard.metadata.mean_tokens_per_second, 100.0)
        quality = next(chart for chart in dashboard.charts if chart.chart_id == "model_quality")
        self.assertEqual([point.value for point in quality.series[0].points], [4.0, None])
        speed = next(chart for chart in dashboard.charts if chart.chart_id == "model_speed")
        self.assertEqual([point.value for point in speed.series[0].points], [100.0, None])
        self.assertNotIn("Deleted", dashboard.metadata.represented_models)
        self.assertEqual(records, original)

    def test_benchmark_dashboard_reuses_trend_interval_and_exposes_coverage(self) -> None:
        report = build_html_analytics_report(
            self.service,
            self.catalog,
            runs=[
                self.make_run(1, created_at="2026-01-01T12:00:00+00:00", score=2.0),
                self.make_run(2, created_at="2026-03-01T12:00:00+00:00", score=4.0),
                self.make_run(3, created_at="bad", score=5.0),
            ],
            options=HtmlAnalyticsReportOptions(
                trend_interval=TimeBucketGranularity.MONTH,
                include_empty_trend_buckets=True,
            ),
            generated_at="fixed",
        )
        trend = next(chart for chart in report.charts if chart.chart_id == "score_trend")
        self.assertEqual([point.label for point in trend.series[0].points], ["2026-01", "2026-02", "2026-03"])
        self.assertTrue(any("invalid" in warning.lower() for warning in report.dashboards[0].metadata.coverage_warnings))

    def test_scoreboard_dashboard_remains_separate_and_handles_unknown_batch(self) -> None:
        active_batch = ScoreboardImportBatch(id=1, name="July Import", source_file="july.csv")
        deleted_batch = ScoreboardImportBatch(id=2, name="Deleted Import", source_file="old.csv", is_deleted=True)
        report = build_html_analytics_report(
            self.service,
            self.catalog,
            options=HtmlAnalyticsReportOptions(source_family=AnalyticsSourceFamily.SCOREBOARD),
            entries=[
                self.entry(1, model="Alpha", batch_id=1),
                self.entry(2, model="Beta", batch_id=99, score=None, speed=None),
                self.entry(3, model="Deleted", batch_id=2, deleted=True),
            ],
            batches=[active_batch, deleted_batch],
            generated_at="fixed",
        )
        self.assertEqual(len(report.dashboards), 1)
        dashboard = report.dashboards[0]
        self.assertEqual(dashboard.source_record_family, "ScoreboardEntry")
        self.assertEqual(dashboard.metadata.contributing_record_count, 2)
        self.assertEqual(dashboard.metadata.scored_record_count, 1)
        self.assertIn("July Import", dashboard.metadata.represented_import_batches)
        self.assertIn("Import batch 99", dashboard.metadata.represented_import_batches)
        self.assertNotIn("Deleted", dashboard.metadata.represented_models)
        self.assertIn("imported_time_score_trend", report.included_chart_ids)

    def test_empty_data_has_useful_omitted_chart_states(self) -> None:
        report = build_html_analytics_report(self.service, self.catalog, runs=[], generated_at="fixed")
        dashboard = report.dashboards[0]
        self.assertEqual(dashboard.metadata.contributing_record_count, 0)
        self.assertEqual(dashboard.charts, ())
        self.assertTrue(dashboard.omitted_charts)
        self.assertTrue(any("No eligible" in item.reason or "No scored" in item.reason for item in dashboard.omitted_charts))
        html = render_html_analytics_report(report)
        self.assertIn("No charts are available", html)

    def test_render_is_single_file_offline_accessible_and_safe(self) -> None:
        hostile = self.make_run(
            1,
            model='<script>alert("x")</script> & ☃',
            session_title='Session "quoted"',
        )
        report = build_html_analytics_report(self.service, self.catalog, runs=[hostile], generated_at="fixed")
        html = render_html_analytics_report(report)
        self.assertIn("&lt;script&gt;alert", html)
        self.assertIn("\u2603", html)
        self.assertNotIn('PRIVATE RAW OUTPUT', html)
        self.assertNotIn('PRIVATE PROMPT', html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertIn("Content-Security-Policy", html)
        self.assertIn("accessible data", html.lower())
        self.assertIn("<noscript>", html)
        self.assertIn("data-series-control", html)
        self.assertIn("<svg", html)
        self.assertIn("benchpup-analytics-data", html)
        self.assertNotIn("</script>alert", html)

    def test_render_is_deterministic_when_timestamp_is_fixed(self) -> None:
        records = [self.make_run(1), self.make_run(2, model="Beta", score=3.0)]
        first = render_html_analytics_report(build_html_analytics_report(self.service, self.catalog, runs=records, generated_at="fixed"))
        second = render_html_analytics_report(build_html_analytics_report(self.service, self.catalog, runs=records, generated_at="fixed"))
        self.assertEqual(first, second)

    def test_combined_report_keeps_source_families_in_separate_dashboards(self) -> None:
        report = build_html_analytics_report(
            self.service,
            self.catalog,
            options=HtmlAnalyticsReportOptions(source_family=AnalyticsSourceFamily.COMBINED),
            runs=[self.make_run(1)],
            entries=[self.entry(2)],
            batches=[ScoreboardImportBatch(id=1, name="July", source_file="july.csv")],
            generated_at="fixed",
        )
        self.assertEqual([dashboard.source_record_family for dashboard in report.dashboards], ["BenchmarkRun", "ScoreboardEntry"])
        self.assertEqual(report.dashboards[0].metadata.contributing_record_count, 1)
        self.assertEqual(report.dashboards[1].metadata.contributing_record_count, 1)
        html = render_html_analytics_report(report)
        self.assertIn('data-series-control="BenchmarkRun-score_distribution-series"', html)
        self.assertIn('data-series-control="ScoreboardEntry-score_distribution-series"', html)

    def test_staged_writer_success_overwrite_protection_and_no_bom(self) -> None:
        report = build_html_analytics_report(self.service, self.catalog, runs=[self.make_run(1)], generated_at="fixed")
        destination = Path(self.directory.name) / "reports" / "analytics.html"
        first = write_html_analytics_report(report, destination)
        self.assertEqual(first.status, ReportWriteStatus.SUCCESS)
        self.assertTrue(destination.exists())
        self.assertFalse(destination.read_bytes().startswith(b"\xef\xbb\xbf"))
        blocked = write_html_analytics_report(report, destination)
        self.assertEqual(blocked.status, ReportWriteStatus.OVERWRITE_REQUIRED)
        overwritten = write_html_analytics_report(report, destination, overwrite=True)
        self.assertEqual(overwritten.status, ReportWriteStatus.SUCCESS)

    def test_staged_writer_rejects_directory_without_partial_output(self) -> None:
        report = build_html_analytics_report(self.service, self.catalog, runs=[self.make_run(1)], generated_at="fixed")
        destination = Path(self.directory.name) / "directory"
        destination.mkdir()
        result = write_html_analytics_report(report, destination)
        self.assertEqual(result.status, ReportWriteStatus.TEMP_WRITE_FAILED)
        self.assertTrue(destination.is_dir())
        self.assertEqual(list(Path(self.directory.name).glob(".directory.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
