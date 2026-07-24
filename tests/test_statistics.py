from __future__ import annotations

import copy
import math
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine import NumericSummary, StatisticsOverview, StatisticsService
from engine.database import EngineDatabase
from engine.domain import BenchmarkRun, BenchmarkSession, ReviewScore, ScoreboardEntry, ScoreboardImportBatch
from engine.reporting import (
    BenchmarkReportFilters,
    BenchmarkRunAggregate,
    ReportingService,
    ScoreboardEntryAggregate,
    normalize_hardware_snapshot,
)
from engine.services import BenchmarkService, CatalogService
from engine.statistics import (
    BenchmarkRunGroupBy,
    BenchmarkStatisticsFilters,
    ScoreboardGroupBy,
    ScoreboardStatisticsFilters,
    TimeBucketGranularity,
    TimeBucketValue,
    build_time_buckets,
    categorical_distribution,
    numeric_summary,
    time_bucket_for,
)


class StatisticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database = EngineDatabase(Path(self.directory.name) / "statistics.db")
        database.migrate()
        self.catalog = CatalogService(database)
        self.service = BenchmarkService(database, self.catalog)
        self.statistics = StatisticsService(self.service, self.catalog)

    def tearDown(self) -> None:
        self.directory.cleanup()

    @staticmethod
    def make_run(
        *,
        run_id: int,
        model_name: str = "Alpha",
        benchmark_name: str = "Review benchmark",
        benchmark_type: str = "code_review",
        session_id: int | None = 1,
        session_title: str = "Session A",
        session_deleted: bool = False,
        hardware: dict[str, object] | None = None,
        score: float | None = 4.0,
        with_review: bool = True,
        speed: float | None = 100.0,
        hallucination: str = "Low",
        reliability: str = "High",
        accuracy: float | None = None,
        depth: float | None = None,
        signal_noise: float | None = None,
        actionability: float | None = None,
        seniority: float | None = None,
        created_at: str = "2026-07-10T12:00:00+00:00",
        is_deleted: bool = False,
    ) -> BenchmarkRunAggregate:
        run = BenchmarkRun(
            raw_model_output="output",
            session_id=session_id,
            model_snapshot={"model_name": model_name, "tokens_per_second": speed},
            benchmark_snapshot={"name": benchmark_name, "benchmark_type": benchmark_type},
            hardware_snapshot=hardware if hardware is not None else {
                "name": "Rig A",
                "cpu": "CPU A",
                "gpu": "GPU A",
                "operating_system": "Windows",
            },
            created_at=created_at,
            is_deleted=is_deleted,
            id=run_id,
        )
        review = (
            ReviewScore(
                run_id=run_id,
                overall_score=score,
                accuracy_score=accuracy,
                hallucination_level=hallucination,
                reliability_level=reliability,
                depth_score=depth,
                signal_noise_score=signal_noise,
                actionability_score=actionability,
                seniority_score=seniority,
            )
            if with_review
            else None
        )
        session = (
            BenchmarkSession(title=session_title, is_deleted=session_deleted, id=session_id)
            if session_id is not None
            else None
        )
        return BenchmarkRunAggregate(run, review, session)

    @staticmethod
    def make_scoreboard_entry(
        *,
        entry_id: int,
        model_name: str,
        score: float | None,
        speed: float | None,
        batch_id: int | None,
        imported_at: str,
        hallucination: str = "Low",
        consistency: str = "High",
        reliability: str = "High",
        is_deleted: bool = False,
    ) -> ScoreboardEntryAggregate:
        return ScoreboardEntryAggregate(
            ScoreboardEntry(
                id=entry_id,
                model_name=model_name,
                score=score,
                tokens_per_second=speed,
                import_batch_id=batch_id,
                imported_at=imported_at,
                hallucination_level=hallucination,
                consistency=consistency,
                reliability_score=reliability,
                is_deleted=is_deleted,
            )
        )

    def benchmark_records(self) -> list[BenchmarkRunAggregate]:
        return [
            self.make_run(
                run_id=1,
                model_name="Alpha",
                benchmark_name="Zeta benchmark",
                benchmark_type="code_review",
                session_id=1,
                session_title="Session B",
                hardware={"name": "Rig B", "cpu": "CPU B", "gpu": "GPU B"},
                score=4.0,
                speed=100.0,
                created_at="2026-07-10T12:00:00+00:00",
            ),
            self.make_run(
                run_id=2,
                model_name="Alpha",
                benchmark_name="Alpha benchmark",
                benchmark_type="code_generation",
                session_id=2,
                session_title="Session A",
                hardware={"name": "Rig A", "cpu": "CPU A", "gpu": "GPU A"},
                score=2.0,
                speed=None,
                hallucination="High",
                reliability="Medium",
                created_at="2026-07-11T12:00:00+00:00",
            ),
            self.make_run(
                run_id=3,
                model_name="Beta",
                benchmark_name="Zeta benchmark",
                benchmark_type="code_review",
                session_id=1,
                session_title="Session B",
                hardware={"name": "Rig B", "cpu": "CPU B", "gpu": "GPU B"},
                score=None,
                speed=200.0,
                hallucination="Medium",
                reliability="Low",
                created_at="2026-07-12T12:00:00+00:00",
            ),
            self.make_run(
                run_id=4,
                model_name="Deleted",
                score=5.0,
                is_deleted=True,
                created_at="2026-07-13T12:00:00+00:00",
            ),
        ]

    def scoreboard_records(self) -> tuple[list[ScoreboardEntryAggregate], list[ScoreboardImportBatch]]:
        july = ScoreboardImportBatch(name="July", source_file="july.csv", imported_at="2026-07-10", id=10)
        deleted_batch = ScoreboardImportBatch(
            name="Deleted batch",
            source_file="old.csv",
            imported_at="2026-07-09",
            is_deleted=True,
            id=11,
        )
        entries = [
            self.make_scoreboard_entry(
                entry_id=1,
                model_name="Alpha",
                score=4.5,
                speed=120.0,
                batch_id=10,
                imported_at="2026-07-10T09:00:00+00:00",
            ),
            self.make_scoreboard_entry(
                entry_id=2,
                model_name="Beta",
                score=None,
                speed=None,
                batch_id=10,
                imported_at="2026-07-11T09:00:00+00:00",
                hallucination="High",
                consistency="Medium",
                reliability="Medium",
            ),
            self.make_scoreboard_entry(
                entry_id=3,
                model_name="Alpha",
                score=3.0,
                speed=80.0,
                batch_id=11,
                imported_at="2026-07-12T09:00:00+00:00",
            ),
            self.make_scoreboard_entry(
                entry_id=4,
                model_name="Deleted entry",
                score=5.0,
                speed=50.0,
                batch_id=None,
                imported_at="2026-07-13T09:00:00+00:00",
                is_deleted=True,
            ),
        ]
        return entries, [july, deleted_batch]

    def test_numeric_summary_definitions_and_source_immutability(self) -> None:
        self.assertEqual(numeric_summary([]), NumericSummary())

        one = numeric_summary([4.0])
        self.assertEqual((one.total_count, one.available_count, one.missing_count), (1, 1, 0))
        self.assertEqual((one.mean, one.median, one.minimum, one.maximum), (4.0, 4.0, 4.0, 4.0))
        self.assertEqual((one.population_standard_deviation, one.sample_standard_deviation), (0.0, None))

        odd = numeric_summary([1, 2, 3, 4, 5])
        self.assertEqual((odd.median, odd.lower_quartile, odd.upper_quartile, odd.interquartile_range), (3.0, 1.5, 4.5, 3.0))
        even = numeric_summary([1, 2, 3, 4])
        self.assertEqual((even.median, even.lower_quartile, even.upper_quartile), (2.5, 1.5, 3.5))

        missing = [1.0, None, 3.0, "not numeric"]
        original = copy.deepcopy(missing)
        summary = numeric_summary(missing)
        self.assertEqual(missing, original)
        self.assertEqual((summary.total_count, summary.available_count, summary.missing_count), (4, 2, 2))
        self.assertEqual((summary.mean, summary.minimum, summary.maximum), (2.0, 1.0, 3.0))
        self.assertAlmostEqual(summary.population_standard_deviation or 0.0, 1.0)
        self.assertAlmostEqual(summary.sample_standard_deviation or 0.0, math.sqrt(2.0))

    def test_categorical_distribution_counts_percentages_and_order(self) -> None:
        distribution = categorical_distribution(["b", "A", None, "a", "", "b"])
        self.assertEqual(distribution.total_count, 6)
        self.assertEqual(distribution.observed_count, 4)
        self.assertEqual(distribution.missing_count, 2)
        self.assertEqual(tuple(distribution.counts), ("A", "a", "b"))
        self.assertEqual(dict(distribution.counts), {"A": 1, "a": 1, "b": 2})
        self.assertEqual(distribution.percentages["b"], 50.0)
        with self.assertRaises(TypeError):
            distribution.counts["new"] = 1  # type: ignore[index]

    def test_benchmark_summary_filters_missing_values_and_bounds(self) -> None:
        records = self.benchmark_records()
        summary = self.statistics.benchmark_run_statistics(records)

        self.assertEqual((summary.total_eligible_runs, summary.scored_runs, summary.unscored_runs), (3, 2, 1))
        self.assertEqual(
            (
                summary.unique_model_count,
                summary.unique_benchmark_count,
                summary.unique_session_count,
                summary.unique_hardware_environment_count,
            ),
            (2, 2, 2, 2),
        )
        self.assertEqual(summary.overall_score.mean, 3.0)
        self.assertEqual((summary.overall_score.minimum, summary.overall_score.maximum), (2.0, 4.0))
        self.assertEqual(summary.tokens_per_second.available_count, 2)
        self.assertEqual(summary.tokens_per_second.missing_count, 1)
        self.assertEqual(dict(summary.hallucination.counts), {"High": 1, "Low": 1, "Medium": 1})
        self.assertEqual(dict(summary.reliability.counts), {"High": 1, "Low": 1, "Medium": 1})
        self.assertEqual(dict(summary.benchmark_type.counts), {"code_generation": 1, "code_review": 2})
        self.assertEqual(summary.created_at_min, "2026-07-10T12:00:00+00:00")
        self.assertEqual(summary.created_at_max, "2026-07-12T12:00:00+00:00")

        self.assertEqual(self.statistics.benchmark_run_statistics(records, filters=BenchmarkStatisticsFilters(model="beta")).total_eligible_runs, 1)
        self.assertEqual(self.statistics.benchmark_run_statistics(records, filters=BenchmarkStatisticsFilters(benchmark="zeta")).total_eligible_runs, 2)
        self.assertEqual(self.statistics.benchmark_run_statistics(records, filters=BenchmarkStatisticsFilters(benchmark_type="generation")).total_eligible_runs, 1)
        self.assertEqual(self.statistics.benchmark_run_statistics(records, filters=BenchmarkStatisticsFilters(session="session a")).total_eligible_runs, 1)
        self.assertEqual(self.statistics.benchmark_run_statistics(records, filters=BenchmarkStatisticsFilters(hardware="Rig A")).total_eligible_runs, 1)
        normalized = normalize_hardware_snapshot(records[1].run.hardware_snapshot)
        self.assertEqual(self.statistics.benchmark_run_statistics(records, filters=BenchmarkStatisticsFilters(hardware=normalized)).total_eligible_runs, 1)
        self.assertEqual(self.statistics.benchmark_run_statistics(records, filters=BenchmarkStatisticsFilters(date_from=date(2026, 7, 11), date_to=date(2026, 7, 11))).total_eligible_runs, 1)
        self.assertEqual(self.statistics.benchmark_run_statistics(records, filters=BenchmarkStatisticsFilters(minimum_score=2.5, maximum_score=4.5)).total_eligible_runs, 1)
        self.assertEqual(self.statistics.benchmark_run_statistics(records, filters=BenchmarkStatisticsFilters(hallucination="high")).total_eligible_runs, 1)
        self.assertEqual(self.statistics.benchmark_run_statistics(records, filters=BenchmarkStatisticsFilters(reliability="high")).total_eligible_runs, 1)

    def test_benchmark_historical_snapshot_and_deleted_session_remain_usable(self) -> None:
        historical = self.make_run(
            run_id=20,
            model_name="Historical",
            session_id=20,
            session_title="Deleted catalog session",
            session_deleted=True,
            hardware={"name": "Historical rig"},
        )
        source_before = copy.deepcopy(historical.run.__dict__)
        summary = self.statistics.benchmark_statistics([historical])
        self.assertEqual(summary.total_runs, 1)
        self.assertEqual(summary.unique_hardware_environment_count, 1)
        self.assertEqual(historical.run.__dict__, source_before)

    def test_summary_tracks_review_and_known_hardware_availability(self) -> None:
        records = self.benchmark_records()
        records.append(
            self.make_run(
                run_id=5,
                model_name="No hardware",
                hardware={},
                with_review=False,
                created_at="2026-07-14T12:00:00+00:00",
            )
        )
        summary = self.statistics.benchmark_run_statistics(records)

        self.assertEqual((summary.reviewed_runs, summary.unreviewed_runs), (3, 1))
        self.assertEqual(summary.known_hardware_environment_count, 2)
        self.assertEqual(summary.missing_hardware_count, 1)

    def test_statistics_overview_is_typed_source_separated_and_read_only(self) -> None:
        records = self.benchmark_records()
        rated = self.make_run(
            run_id=20,
            model_name="Rated",
            score=4.5,
            accuracy=4.0,
            depth=3.5,
            signal_noise=4.0,
            actionability=4.5,
            seniority=3.0,
            hardware={"name": "Rated rig"},
        )
        records.append(rated)
        entries, batches = self.scoreboard_records()
        runs_before = copy.deepcopy([aggregate.run.__dict__ for aggregate in records])
        entries_before = copy.deepcopy([aggregate.entry.__dict__ for aggregate in entries])

        overview = self.statistics.statistics_overview(records, entries, batches=batches)

        self.assertIsInstance(overview, StatisticsOverview)
        self.assertEqual(overview.benchmark_runs.total_eligible_runs, 4)
        self.assertEqual(overview.scoreboard_entries.total_eligible_entries, 2)
        self.assertEqual(overview.reviews.total_reviews, 4)
        self.assertEqual(overview.reviews.accuracy_score.mean, 4.0)
        self.assertEqual(overview.reviews.depth_score.mean, 3.5)
        self.assertEqual(overview.reviews.signal_noise_score.mean, 4.0)
        self.assertEqual(overview.reviews.actionability_score.mean, 4.5)
        self.assertEqual(overview.reviews.seniority_score.mean, 3.0)
        self.assertTrue(overview.availability.benchmark_runs)
        self.assertTrue(overview.availability.scoreboard_entries)
        self.assertTrue(overview.availability.benchmark_reviews)
        self.assertTrue(overview.availability.benchmark_hardware)
        self.assertEqual([aggregate.run.__dict__ for aggregate in records], runs_before)
        self.assertEqual([aggregate.entry.__dict__ for aggregate in entries], entries_before)

    def test_empty_statistics_overview_marks_optional_metrics_unavailable(self) -> None:
        overview = self.statistics.statistics_overview()

        self.assertEqual(overview.benchmark_runs.total_eligible_runs, 0)
        self.assertEqual(overview.scoreboard_entries.total_eligible_entries, 0)
        self.assertEqual(overview.reviews.total_reviews, 0)
        self.assertFalse(overview.availability.benchmark_runs)
        self.assertFalse(overview.availability.scoreboard_entries)
        self.assertFalse(overview.availability.benchmark_scores)
        self.assertFalse(overview.availability.benchmark_hardware)

    def test_database_statistics_overview_is_read_only(self) -> None:
        saved, _ = self.service.save_run(
            BenchmarkRun(
                raw_model_output="persisted output",
                model_snapshot={"model_name": "Persisted"},
                benchmark_snapshot={"name": "Persisted benchmark"},
            )
        )
        runs_before = copy.deepcopy(self.service.runs.list())
        reviews_before = copy.deepcopy(self.service.scores.list())

        overview = self.statistics.statistics_overview()

        self.assertEqual(overview.benchmark_runs.total_eligible_runs, 1)
        self.assertEqual(self.service.runs.list(), runs_before)
        self.assertEqual(self.service.scores.list(), reviews_before)
        self.assertEqual(self.service.runs.get(saved.id), runs_before[0])

    def test_benchmark_grouping_is_separate_and_deterministic(self) -> None:
        records = self.benchmark_records()
        unknown = self.make_run(
            run_id=5,
            model_name="",
            benchmark_name="",
            benchmark_type="",
            session_id=None,
            hardware={},
            with_review=False,
            created_at="2026-07-14T12:00:00+00:00",
        )
        records_with_unknown = records + [unknown]

        models = self.statistics.group_benchmark_runs(records_with_unknown, group_by=BenchmarkRunGroupBy.MODEL)
        self.assertEqual([group.label for group in models], ["Alpha", "Beta", "Unknown model"])
        self.assertEqual([group.record_count for group in models], [2, 1, 1])
        self.assertEqual(models[-1].summary.scored_runs, 0)

        benchmarks = self.statistics.group_benchmark_statistics(records, group_by="benchmark")
        self.assertEqual([group.label for group in benchmarks], ["Alpha benchmark", "Zeta benchmark"])
        sessions = self.statistics.group_benchmark_runs(records_with_unknown, group_by="session")
        self.assertEqual([group.label for group in sessions], ["Session A", "Session B", "Unknown session"])
        hardware = self.statistics.group_benchmark_runs(records_with_unknown, group_by="hardware")
        self.assertEqual([group.label for group in hardware], ["Rig A, CPU A, GPU A", "Rig B, CPU B, GPU B", "Unknown hardware"])
        reversed_hardware = self.statistics.group_benchmark_runs(list(reversed(records_with_unknown)), group_by="hardware")
        self.assertEqual(
            [(group.key, group.label, group.record_count) for group in hardware],
            [(group.key, group.label, group.record_count) for group in reversed_hardware],
        )

    def test_scoreboard_summary_filters_and_deleted_batch_rules(self) -> None:
        entries, batches = self.scoreboard_records()
        summary = self.statistics.scoreboard_statistics(entries, batches=batches)
        self.assertEqual((summary.total_entries, summary.scored_entries, summary.unscored_entries), (2, 1, 1))
        self.assertEqual((summary.unique_model_count, summary.unique_import_batch_count), (2, 1))
        self.assertEqual(summary.score.mean, 4.5)
        self.assertEqual(summary.score.missing_count, 1)
        self.assertEqual(summary.tokens_per_second.missing_count, 1)
        self.assertEqual(summary.imported_at_min, "2026-07-10T09:00:00+00:00")
        self.assertEqual(summary.imported_at_max, "2026-07-11T09:00:00+00:00")

        self.assertEqual(self.statistics.scoreboard_statistics(entries, batches=batches, filters=ScoreboardStatisticsFilters(model="alpha")).total_entries, 1)
        self.assertEqual(self.statistics.scoreboard_statistics(entries, batches=batches, filters=ScoreboardStatisticsFilters(batch_id=10)).total_entries, 2)
        self.assertEqual(self.statistics.scoreboard_statistics(entries, batches=batches, filters=ScoreboardStatisticsFilters(date_from="2026-07-11", date_to="2026-07-11")).total_entries, 1)
        self.assertEqual(self.statistics.scoreboard_statistics(entries, batches=batches, filters=ScoreboardStatisticsFilters(hallucination="high", consistency="medium", reliability="medium")).total_entries, 1)

        included = self.statistics.scoreboard_statistics(entries, batches=batches, filters=ScoreboardStatisticsFilters(include_deleted=True))
        self.assertEqual(included.total_entries, 4)
        self.assertEqual(included.unique_import_batch_count, 2)

    def test_scoreboard_grouping_and_source_immutability(self) -> None:
        entries, batches = self.scoreboard_records()
        before = [copy.deepcopy(aggregate.entry.__dict__) for aggregate in entries]
        models = self.statistics.group_scoreboard_entries(entries, batches=batches, group_by=ScoreboardGroupBy.MODEL)
        self.assertEqual([group.label for group in models], ["Alpha", "Beta"])
        self.assertEqual([group.record_count for group in models], [1, 1])
        batch_groups = self.statistics.group_scoreboard_statistics(entries, batches=batches, group_by="batch")
        self.assertEqual([group.label for group in batch_groups], ["July"])
        self.assertEqual([aggregate.entry.__dict__ for aggregate in entries], before)

    def test_time_buckets_are_utc_deterministic_and_count_invalid_timestamps(self) -> None:
        values = [
            TimeBucketValue("2026-07-10T23:30:00-07:00", score=4.0, tokens_per_second=100.0),
            TimeBucketValue("2026-07-11T00:30:00+00:00", score=2.0),
            TimeBucketValue("2026-07-13T12:00:00+00:00", score=None, tokens_per_second=200.0),
            TimeBucketValue(None, score=5.0),
            TimeBucketValue("not a timestamp", score=1.0),
            TimeBucketValue("2026-08-01T01:00:00+00:00", score=3.0),
        ]
        daily = build_time_buckets(values, granularity=TimeBucketGranularity.DAY)
        self.assertEqual(daily.invalid_timestamp_count, 2)
        self.assertEqual([bucket.label for bucket in daily], ["2026-07-11", "2026-07-13", "2026-08-01"])
        self.assertEqual(daily[0].record_count, 2)
        self.assertEqual(daily[0].scored_count, 2)
        self.assertEqual(daily[1].scored_count, 0)

        weekly = build_time_buckets(values, granularity="week")
        self.assertEqual([bucket.label for bucket in weekly], ["2026-W28", "2026-W29", "2026-W31"])
        self.assertTrue(all(bucket.bucket_start.endswith("+00:00") for bucket in weekly))
        monthly = build_time_buckets(values, granularity="month")
        self.assertEqual([bucket.label for bucket in monthly], ["2026-07", "2026-08"])

        self.assertEqual(time_bucket_for("2026-07-10T23:30:00-07:00", "day"), ("2026-07-11T00:00:00+00:00", "2026-07-11"))
        self.assertEqual(time_bucket_for("2026-07-12T23:00:00-07:00", "week"), ("2026-07-13T00:00:00+00:00", "2026-W29"))

    def test_statistics_service_public_import_and_time_bucket_api(self) -> None:
        self.assertIs(StatisticsService, type(self.statistics))
        result = self.statistics.time_buckets(
            [TimeBucketValue("2026-07-01T00:00:00+00:00", score=4.0)],
            granularity="month",
        )
        self.assertEqual(result[0].score_summary.mean, 4.0)

    def test_reporting_selection_remains_compatible_without_explicit_filters(self) -> None:
        records = self.benchmark_records()
        reporting = ReportingService(self.service, self.catalog)
        self.assertEqual(len(reporting.select_benchmark_runs(records)), 4)
        self.assertEqual(len(reporting.select_benchmark_runs(records, filters=BenchmarkReportFilters())), 3)


if __name__ == "__main__":
    unittest.main()
