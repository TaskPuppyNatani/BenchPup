from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine import (
    BenchmarkStatisticsFilters,
    BenchmarkTrendGrouping,
    ScoreboardStatisticsFilters,
    TimeBucketGranularity,
    TrendGrouping,
    TrendReport,
    TrendService,
)
from engine.database import EngineDatabase
from engine.domain import (
    BenchmarkRun,
    BenchmarkSession,
    ReviewScore,
    ScoreboardEntry,
    ScoreboardImportBatch,
)
from engine.reporting import (
    BenchmarkRunAggregate,
    ReportWriteStatus,
    ScoreboardEntryAggregate,
    ReportingService,
    render_markdown,
)
from engine.services import BenchmarkService, CatalogService


class TrendServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database = EngineDatabase(Path(self.directory.name) / "trends.db")
        database.migrate()
        self.catalog = CatalogService(database)
        self.service = BenchmarkService(database, self.catalog)
        self.trends = TrendService(self.service, self.catalog)
        self.reporting = ReportingService(self.service, self.catalog)

    def tearDown(self) -> None:
        self.directory.cleanup()

    @staticmethod
    def make_run(
        run_id: int,
        *,
        model: str = "Alpha",
        benchmark: str = "Review",
        benchmark_type: str = "code_review",
        session_id: int | None = 1,
        session_title: str = "Session A",
        hardware: dict[str, object] | None = None,
        created_at: str = "2026-07-01T12:00:00+00:00",
        score: float | None = 2.0,
        speed: float | None = 100.0,
        deleted: bool = False,
    ) -> BenchmarkRunAggregate:
        run = BenchmarkRun(
            id=run_id,
            raw_model_output="private",
            session_id=session_id,
            model_snapshot={"model_name": model, "tokens_per_second": speed},
            benchmark_snapshot={"name": benchmark, "benchmark_type": benchmark_type},
            hardware_snapshot=hardware if hardware is not None else {"name": "Rig A", "cpu": "CPU"},
            created_at=created_at,
            is_deleted=deleted,
        )
        review = ReviewScore(
            run_id=run_id,
            overall_score=score,
            hallucination_level="Low",
            reliability_level="High",
        )
        session = BenchmarkSession(title=session_title, id=session_id) if session_id is not None else None
        return BenchmarkRunAggregate(run, review, session)

    @staticmethod
    def make_entry(
        entry_id: int,
        *,
        model: str,
        imported_at: str,
        score: float | None,
        speed: float | None,
        batch_id: int | None,
        deleted: bool = False,
        consistency: str = "High",
    ) -> ScoreboardEntryAggregate:
        return ScoreboardEntryAggregate(
            ScoreboardEntry(
                id=entry_id,
                model_name=model,
                imported_at=imported_at,
                score=score,
                tokens_per_second=speed,
                import_batch_id=batch_id,
                hallucination_level="Low",
                consistency=consistency,
                reliability_score="High",
                is_deleted=deleted,
            )
        )

    def test_benchmark_daily_trend_uses_utc_buckets_and_neutral_deltas(self) -> None:
        records = [
            self.make_run(1, created_at="2026-07-01T23:30:00-02:00", score=2.0),
            self.make_run(2, created_at="2026-07-03T00:30:00+02:00", score=4.0, speed=None),
            self.make_run(3, created_at="not-a-timestamp", score=5.0),
            self.make_run(4, created_at="2026-07-04T12:00:00+00:00", score=5.0, deleted=True),
        ]
        original = copy.deepcopy(records)
        report = self.trends.benchmark_run_trend(records, generated_at="fixed")

        self.assertIsInstance(report, TrendReport)
        self.assertEqual(report.metadata.contributing_record_count, 2)
        self.assertEqual(report.metadata.excluded_timestamp_count, 1)
        self.assertEqual([point.label for point in report.aggregate_series.points], ["2026-07-02"])
        self.assertEqual(report.aggregate_series.points[0].record_count, 2)
        self.assertEqual(report.aggregate_series.points[0].scored_count, 2)
        self.assertIsNone(report.aggregate_series.score_absolute_delta)
        self.assertIsNone(report.aggregate_series.score_percentage_delta)
        self.assertEqual(report.aggregate_series.speed_summary.missing_count, 1)
        self.assertEqual(records, original)

    def test_week_month_grouping_and_empty_buckets_are_deterministic(self) -> None:
        records = [
            self.make_run(1, created_at="2026-01-02T12:00:00+00:00", model="Beta", score=1.0),
            self.make_run(2, created_at="2026-03-10T12:00:00+00:00", model="Alpha", score=3.0),
        ]
        monthly = self.trends.benchmark_run_trend(
            records,
            granularity=TimeBucketGranularity.MONTH,
            grouping=BenchmarkTrendGrouping.MODEL,
            include_empty_buckets=True,
            generated_at="fixed",
        )
        self.assertEqual([point.label for point in monthly.aggregate_series.points], ["2026-01", "2026-02", "2026-03"])
        self.assertEqual(monthly.aggregate_series.missing_bucket_count, 1)
        self.assertEqual(monthly.aggregate_series.score_absolute_delta, 2.0)
        self.assertAlmostEqual(monthly.aggregate_series.score_percentage_delta or 0.0, 200.0)
        self.assertEqual([series.label for series in monthly.series], ["Alpha", "Beta"])
        self.assertIn("series cover different date ranges", monthly.coverage_warnings)

        weekly = self.trends.benchmark_run_trend(records, granularity=TimeBucketGranularity.WEEK)
        self.assertEqual(weekly.aggregate_series.points[0].label, "2026-W01")

    def test_benchmark_groupings_filters_and_unknown_hardware(self) -> None:
        records = [
            self.make_run(1, model="Alpha", benchmark="A", benchmark_type="code_review", session_id=None, hardware={}, score=1.0),
            self.make_run(2, model="Beta", benchmark="B", benchmark_type="revision", session_id=2, hardware={"name": "Rig B"}, score=4.0),
        ]
        report = self.trends.benchmark_run_trend(
            records,
            grouping=TrendGrouping.HARDWARE,
            filters=BenchmarkStatisticsFilters(model="beta", benchmark_type="revision"),
            generated_at="fixed",
        )
        self.assertEqual([series.label for series in report.series], ["Rig B"])
        self.assertEqual(dict(report.metadata.active_filters)["model"], "beta")
        self.assertIn(
            "Unknown session",
            [series.label for series in self.trends.benchmark_run_trend(records, grouping=TrendGrouping.SESSION).series],
        )

    def test_zero_baseline_and_single_bucket_do_not_invent_percentage_or_direction(self) -> None:
        report = self.trends.benchmark_run_trend(
            [self.make_run(1, score=0.0)],
            generated_at="fixed",
        )
        self.assertIsNone(report.aggregate_series.score_percentage_delta)
        self.assertIsNone(report.aggregate_series.speed_percentage_delta)
        self.assertTrue(any("only one populated bucket" in warning for warning in report.coverage_warnings))
        self.assertIn("last minus first", report.methodology_note)

    def test_scoreboard_trend_keeps_entries_separate_and_excludes_deleted_batches(self) -> None:
        batch = ScoreboardImportBatch(name="July", source_file="july.csv", id=10, imported_at="2026-07-01")
        deleted_batch = ScoreboardImportBatch(name="Old", source_file="old.csv", id=11, is_deleted=True)
        entries = [
            self.make_entry(1, model="Alpha", imported_at="2026-07-01T10:00:00+00:00", score=2.0, speed=10.0, batch_id=10),
            self.make_entry(2, model="Alpha", imported_at="2026-07-02T10:00:00+00:00", score=4.0, speed=20.0, batch_id=10, consistency="Medium"),
            self.make_entry(3, model="Beta", imported_at="bad", score=5.0, speed=30.0, batch_id=None),
            self.make_entry(4, model="Deleted", imported_at="2026-07-03", score=5.0, speed=30.0, batch_id=11),
        ]
        report = self.trends.scoreboard_entry_trend(
            entries,
            batches=[batch, deleted_batch],
            grouping=TrendGrouping.MODEL,
            filters=ScoreboardStatisticsFilters(consistency="medium"),
            generated_at="fixed",
        )
        self.assertEqual(report.metadata.contributing_record_count, 1)
        self.assertEqual(report.metadata.excluded_timestamp_count, 0)
        self.assertEqual(report.series[0].label, "Alpha")
        self.assertEqual(dict(report.series[0].points[0].consistency.counts), {"Medium": 1})
        self.assertEqual(report.series[0].score_absolute_delta, None)

        unknown = self.trends.scoreboard_entry_trend(
            [self.make_entry(5, model="Unknown", imported_at="2026-07-04", score=None, speed=None, batch_id=99)],
            grouping=TrendGrouping.IMPORT_BATCH,
            generated_at="fixed",
        )
        self.assertEqual(unknown.series[0].label, "Unknown import batch")
        self.assertEqual(unknown.series[0].points[0].score_summary.missing_count, 1)

    def test_trend_markdown_and_writer_use_structured_values(self) -> None:
        report = self.trends.benchmark_run_trend(
            [
                self.make_run(1, score=None, speed=None),
                self.make_run(2, created_at="2026-07-03T12:00:00+00:00", score=3.0),
            ],
            include_empty_buckets=True,
            generated_at="fixed",
        )
        markdown = render_markdown(report)
        self.assertIn("## Methodology", markdown)
        self.assertIn("## Coverage and excluded data", markdown)
        self.assertIn("Score mean", markdown)
        self.assertIn("—", markdown)
        self.assertNotIn("private", markdown)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "trend.md"
            result = self.reporting.write_markdown_report(report, destination)
            self.assertEqual(result.status, ReportWriteStatus.SUCCESS)
            self.assertFalse(destination.read_bytes().startswith(b"\xef\xbb\xbf"))
            blocked = self.reporting.write_markdown_report(report, destination)
            self.assertEqual(blocked.status, ReportWriteStatus.OVERWRITE_REQUIRED)


if __name__ == "__main__":
    unittest.main()
