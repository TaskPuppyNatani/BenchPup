from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import engine
from engine import ComparisonService, ReportingService
from engine.comparisons import (
    BenchmarkModelComparisonRequest,
    ComparisonEntitySummary,
    ComparisonResultState,
    ComparisonSourceFamily,
    ComparisonWarning,
    ComparisonWarningCode,
    ModelComparisonResult,
    ScoreboardComparisonEntitySummary,
    ScoreboardModelComparisonRequest,
    ScoreboardModelComparisonResult,
    SessionComparisonResult,
)
from engine.database import EngineDatabase
from engine.domain import (
    BenchmarkRun,
    BenchmarkSession,
    ReviewScore,
    ScoreboardEntry,
    ScoreboardImportBatch,
)
from engine.reporting import BenchmarkRunAggregate, ScoreboardEntryAggregate, render_markdown
from engine.services import BenchmarkService, CatalogService
from engine.statistics import BenchmarkStatisticsFilters, ScoreboardStatisticsFilters


class ComparisonServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database = EngineDatabase(Path(self.directory.name) / "comparisons.db")
        database.migrate()
        self.catalog = CatalogService(database)
        self.service = BenchmarkService(database, self.catalog)
        self.comparisons = ComparisonService(self.service, self.catalog)
        self.reporting = ReportingService(self.service, self.catalog)

    def tearDown(self) -> None:
        self.directory.cleanup()

    @staticmethod
    def make_run(
        run_id: int,
        *,
        model: str,
        benchmark: str,
        session: BenchmarkSession | None,
        score: float | None,
        speed: float | None,
        hallucination: str = "Low",
        reliability: str = "High",
        deleted: bool = False,
        created_at: str = "2026-07-10T12:00:00+00:00",
    ) -> BenchmarkRunAggregate:
        run = BenchmarkRun(
            id=run_id,
            raw_model_output="private output that must not be rendered",
            session_id=session.id if session else None,
            model_snapshot={"model_name": model, "tokens_per_second": speed},
            benchmark_snapshot={"name": benchmark, "benchmark_type": "code_review"},
            hardware_snapshot={"name": "Rig A", "cpu": "CPU A"},
            created_at=created_at,
            is_deleted=deleted,
        )
        review = ReviewScore(
            run_id=run_id,
            overall_score=score,
            hallucination_level=hallucination,
            reliability_level=reliability,
        )
        return BenchmarkRunAggregate(run, review, session)

    def model_runs(self) -> tuple[BenchmarkRunAggregate, ...]:
        first = BenchmarkSession("First", id=1)
        second = BenchmarkSession("Second", id=2)
        return (
            self.make_run(1, model="Alpha", benchmark="Shared", session=first, score=4.0, speed=100.0),
            self.make_run(
                2,
                model="Alpha",
                benchmark="Alpha only",
                session=first,
                score=2.0,
                speed=None,
                hallucination="High",
                reliability="Medium",
            ),
            self.make_run(
                3,
                model="Beta",
                benchmark="Shared",
                session=second,
                score=3.0,
                speed=200.0,
                hallucination="High",
                reliability="Low",
            ),
            self.make_run(4, model="beta", benchmark="Beta only", session=second, score=5.0, speed=150.0),
            self.make_run(5, model="Deleted", benchmark="Shared", session=first, score=5.0, speed=500.0, deleted=True),
        )

    def multi_session_model_runs(
        self,
    ) -> tuple[BenchmarkSession, BenchmarkSession, tuple[BenchmarkRunAggregate, ...]]:
        first = BenchmarkSession("First", id=1)
        second = BenchmarkSession("Second", id=2)
        runs = (
            self.make_run(20, model="Alpha", benchmark="Shared", session=first, score=1.0, speed=10.0),
            self.make_run(21, model="Beta", benchmark="Shared", session=first, score=2.0, speed=20.0),
            self.make_run(22, model="Alpha", benchmark="Shared", session=second, score=9.0, speed=90.0),
            self.make_run(23, model="Beta", benchmark="Shared", session=second, score=8.0, speed=80.0),
        )
        return first, second, runs

    @staticmethod
    def make_scoreboard_entry(
        entry_id: int,
        *,
        model: str,
        score: float | None,
        speed: float | None,
        batch_id: int | None = 10,
        imported_at: str = "2026-07-10T12:00:00+00:00",
        hallucination: str = "Low",
        consistency: str = "High",
        reliability: str = "High",
    ) -> ScoreboardEntryAggregate:
        return ScoreboardEntryAggregate(
            ScoreboardEntry(
                id=entry_id,
                model_name=model,
                score=score,
                tokens_per_second=speed,
                import_batch_id=batch_id,
                imported_at=imported_at,
                hallucination_level=hallucination,
                consistency=consistency,
                reliability_score=reliability,
            )
        )

    def scoreboard_entries(self) -> tuple[tuple[ScoreboardEntryAggregate, ...], tuple[ScoreboardImportBatch, ...]]:
        entries = (
            self.make_scoreboard_entry(10, model="Alpha", score=4.0, speed=100.0),
            self.make_scoreboard_entry(11, model="Beta", score=2.0, speed=None, consistency="Medium"),
            self.make_scoreboard_entry(12, model="alpha", score=5.0, speed=120.0),
        )
        batches = (ScoreboardImportBatch(id=10, name="July", source_file="july.csv"),)
        return entries, batches

    def test_model_comparison_aligns_snapshots_and_calculates_pairwise_deltas(self) -> None:
        result = self.comparisons.compare_models(
            ("alpha", "BETA"),
            self.model_runs(),
            generated_at="2026-07-18T00:00:00+00:00",
        )

        self.assertIsInstance(result, ModelComparisonResult)
        self.assertEqual(result.selected_models, ("Alpha", "Beta"))
        self.assertEqual([entity.record_count for entity in result.entities], [2, 2])
        self.assertEqual(result.alignment.shared_benchmark_count, 1)
        self.assertEqual(result.alignment.shared_benchmarks, ("Shared",))
        self.assertEqual(result.alignment.excluded_benchmark_count, 2)
        self.assertEqual(dict(result.alignment.non_overlapping_benchmarks), {
            "alpha": ("Alpha only",),
            "beta": ("Beta only",),
        })

        self.assertIsNotNone(result.pairwise)
        assert result.pairwise is not None
        score_delta = result.pairwise.metric("mean_overall_score")
        self.assertIsNotNone(score_delta)
        assert score_delta is not None
        self.assertAlmostEqual(score_delta.absolute_delta or 0.0, 1.0)
        self.assertAlmostEqual(score_delta.percentage_delta or 0.0, 33.3333333333)
        self.assertEqual(result.pairwise.delta_direction, "second_minus_first")
        self.assertEqual(result.entities[0].tokens_per_second.missing_count, 1)
        self.assertTrue(any(row.category == "hallucination:Low" for row in result.categorical_comparisons))

        with self.assertRaises(TypeError):
            result.metadata.active_filters["new"] = "mutation"  # type: ignore[index]
        self.assertNotIn("Deleted", result.metadata.selected_entities)

    def test_model_comparison_ranking_is_deterministic_and_unscored_models_remain_unranked(self) -> None:
        runs = self.model_runs() + (
            self.make_run(6, model="Gamma", benchmark="Gamma only", session=BenchmarkSession("Third", id=3), score=None, speed=None),
        )
        result = self.comparisons.compare_models(("Gamma", "Alpha", "Beta"), runs)

        self.assertEqual([entry.label for entry in result.ranking], ["Beta", "Alpha", "Gamma"])
        self.assertEqual([entry.rank for entry in result.ranking], [1, 2, None])
        self.assertEqual([entry.label for entry in result.speed_ranking], ["Beta", "Alpha", "Gamma"])
        self.assertEqual(result.ranking[-1].scored_count, 0)

    def test_pairwise_percentage_is_unavailable_for_zero_baseline(self) -> None:
        sessions = (BenchmarkSession("First", id=1), BenchmarkSession("Second", id=2))
        runs = (
            self.make_run(1, model="Alpha", benchmark="Shared", session=sessions[0], score=0.0, speed=0.0),
            self.make_run(2, model="Beta", benchmark="Shared", session=sessions[1], score=2.0, speed=10.0),
        )
        result = self.comparisons.compare_models(("Alpha", "Beta"), runs)
        assert result.pairwise is not None
        score = result.pairwise.metric("mean_overall_score")
        speed = result.pairwise.metric("mean_tokens_per_second")
        assert score is not None and speed is not None
        self.assertEqual(score.absolute_delta, 2.0)
        self.assertIsNone(score.percentage_delta)
        self.assertEqual(score.unavailable_reason, "baseline is zero")
        self.assertIsNone(speed.percentage_delta)

    def test_typed_benchmark_subjects_and_model_request_preserve_snapshots_and_reviews(self) -> None:
        runs = self.model_runs()
        snapshots_before = [run.run.model_snapshot.copy() for run in runs]

        discovery = self.comparisons.discover_benchmark_model_subjects(runs)
        result = self.comparisons.compare_benchmark_models(
            BenchmarkModelComparisonRequest(("Beta", "Alpha")),
            runs,
        )

        self.assertEqual(discovery.source_family, ComparisonSourceFamily.BENCHMARK_RUN)
        self.assertEqual([subject.identity for subject in discovery], ["alpha", "beta"])
        self.assertEqual([subject.record_count for subject in discovery], [2, 2])
        self.assertTrue(all(subject.review_available for subject in discovery))
        self.assertEqual(result.selected_models, ("Beta", "Alpha"))
        self.assertEqual(result.metadata.source_family, ComparisonSourceFamily.BENCHMARK_RUN)
        self.assertEqual(result.state, ComparisonResultState.READY_WITH_MISSING_VALUES)
        self.assertEqual([entity.review.total_reviews for entity in result.entities], [2, 2])
        self.assertEqual(result.pairwise.baseline_entity if result.pairwise else None, "beta")
        self.assertEqual([run.run.model_snapshot for run in runs], snapshots_before)

    def test_typed_benchmark_model_comparison_preserves_session_filters(self) -> None:
        first, second, runs = self.multi_session_model_runs()
        session_filter = BenchmarkStatisticsFilters(session="First")
        session_request = BenchmarkModelComparisonRequest(
            ("Alpha", "Beta"),
            filters=session_filter,
            generated_at="fixed",
        )

        discovery = self.comparisons.discover_benchmark_model_subjects(
            runs,
            filters=session_filter,
        )
        by_name = self.comparisons.compare_benchmark_models(session_request, runs)

        self.assertEqual([subject.record_count for subject in discovery], [1, 1])
        self.assertEqual([entity.record_count for entity in by_name.entities], [1, 1])
        self.assertEqual([entity.overall_score.mean for entity in by_name.entities], [1.0, 2.0])
        self.assertEqual([entity.tokens_per_second.mean for entity in by_name.entities], [10.0, 20.0])
        self.assertEqual(session_request.filters, session_filter)
        self.assertEqual(by_name.filters, session_filter)

        session_id_filter = BenchmarkStatisticsFilters(session_id=second.id)
        by_id = self.comparisons.compare_benchmark_models(
            BenchmarkModelComparisonRequest(
                ("Alpha", "Beta"),
                filters=session_id_filter,
                generated_at="fixed",
            ),
            runs,
        )
        self.assertEqual([entity.record_count for entity in by_id.entities], [1, 1])
        self.assertEqual([entity.overall_score.mean for entity in by_id.entities], [9.0, 8.0])
        self.assertEqual(session_id_filter, BenchmarkStatisticsFilters(session_id=2))

        legacy_session_result = self.comparisons.compare_sessions(
            (first, second),
            runs,
            filters=BenchmarkStatisticsFilters(session_id=first.id),
            generated_at="fixed",
        )
        self.assertEqual([entity.record_count for entity in legacy_session_result.entities], [2, 2])

    def test_source_specific_result_contracts_reject_mixed_sources(self) -> None:
        runs = self.model_runs()
        entries, batches = self.scoreboard_entries()
        benchmark_result = self.comparisons.compare_benchmark_models(
            BenchmarkModelComparisonRequest(("Alpha", "Beta"), generated_at="fixed"),
            runs,
        )
        scoreboard_result = self.comparisons.compare_scoreboard_models(
            ScoreboardModelComparisonRequest(("Beta", "Alpha"), generated_at="fixed"),
            entries,
            batches=batches,
        )

        self.assertIs(engine.ModelComparisonResult, ModelComparisonResult)
        self.assertIs(engine.ScoreboardModelComparisonResult, ScoreboardModelComparisonResult)
        self.assertIs(engine.ComparisonWarning, ComparisonWarning)
        self.assertIsInstance(replace(benchmark_result), ModelComparisonResult)
        self.assertIsInstance(replace(scoreboard_result), ScoreboardModelComparisonResult)

        with self.assertRaises(ValueError):
            replace(
                scoreboard_result,
                metadata=replace(
                    scoreboard_result.metadata,
                    source_family=ComparisonSourceFamily.BENCHMARK_RUN,
                ),
            )
        with self.assertRaises(ValueError):
            replace(scoreboard_result, entities=(benchmark_result.entities[0],))
        with self.assertRaises(ValueError):
            replace(
                scoreboard_result,
                warnings=(
                    ComparisonWarning(
                        code=ComparisonWarningCode.NO_SOURCE_DATA,
                        source_family=ComparisonSourceFamily.BENCHMARK_RUN,
                        message="wrong source",
                    ),
                ),
            )
        with self.assertRaises(ValueError):
            replace(
                benchmark_result,
                metadata=replace(
                    benchmark_result.metadata,
                    source_family=ComparisonSourceFamily.SCOREBOARD,
                ),
            )
        with self.assertRaises(ValueError):
            replace(benchmark_result, entities=(scoreboard_result.entities[0],))

        benchmark_discovery = self.comparisons.discover_benchmark_model_subjects(runs)
        scoreboard_discovery = self.comparisons.discover_scoreboard_model_subjects(
            entries,
            batches=batches,
        )
        with self.assertRaises(ValueError):
            replace(benchmark_discovery, subjects=scoreboard_discovery.subjects)

        with self.assertRaisesRegex(
            TypeError,
            "ComparisonEntitySummary.summary must be a BenchmarkRunStatisticsSummary",
        ):
            ComparisonEntitySummary(
                "mixed",
                "mixed",
                scoreboard_result.entities[0].summary,
            )
        with self.assertRaisesRegex(
            TypeError,
            "ScoreboardComparisonEntitySummary.summary must be a ScoreboardStatisticsSummary",
        ):
            ScoreboardComparisonEntitySummary(
                "mixed",
                "mixed",
                benchmark_result.entities[0].summary,
            )
        with self.assertRaisesRegex(
            TypeError,
            "ComparisonEntitySummary.review must be a ReviewStatisticsSummary",
        ):
            ComparisonEntitySummary(
                "mixed",
                "mixed",
                benchmark_result.entities[0].summary,
                review=scoreboard_result.entities[0].summary,
            )
        self.assertIsInstance(
            ComparisonEntitySummary(
                "valid",
                "valid",
                benchmark_result.entities[0].summary,
                review=benchmark_result.entities[0].review,
            ),
            ComparisonEntitySummary,
        )
        self.assertIsInstance(
            ScoreboardComparisonEntitySummary(
                "valid",
                "valid",
                scoreboard_result.entities[0].summary,
            ),
            ScoreboardComparisonEntitySummary,
        )

    def test_legacy_model_comparison_preserves_filter_provenance(self) -> None:
        _, _, runs = self.multi_session_model_runs()
        filters = BenchmarkStatisticsFilters(
            benchmark="Shared",
            session_id=1,
            hardware="Rig A",
            date_from="2026-07-01",
            date_to="2026-07-31",
        )

        result = self.comparisons.compare_models(
            ("Alpha", "Beta"),
            runs,
            filters=filters,
            generated_at="fixed",
        )

        self.assertEqual(result.filters, filters)
        self.assertEqual(result.metadata.active_filters["benchmark"], "Shared")
        self.assertEqual(result.metadata.active_filters["session_id"], "1")
        self.assertEqual(result.metadata.active_filters["hardware"], "Rig A")
        self.assertEqual(result.metadata.active_filters["date_from"], "2026-07-01")
        self.assertEqual(result.metadata.active_filters["date_to"], "2026-07-31")
        self.assertEqual([entity.overall_score.mean for entity in result.entities], [1.0, 2.0])

        default_result = self.comparisons.compare_models(
            ("Alpha", "Beta"),
            runs,
            generated_at="fixed",
        )
        self.assertEqual(default_result.filters, BenchmarkStatisticsFilters())

    def test_three_subject_typed_comparison_preserves_order_without_pairwise(self) -> None:
        entries, batches = self.scoreboard_entries()
        entries = entries + (
            self.make_scoreboard_entry(13, model="Gamma", score=3.0, speed=130.0),
        )

        result = self.comparisons.compare_scoreboard_models(
            ScoreboardModelComparisonRequest(
                ("Beta", "Gamma", "Alpha"),
                generated_at="fixed",
            ),
            entries,
            batches=batches,
        )

        self.assertEqual(result.selected_models, ("beta", "gamma", "alpha"))
        self.assertEqual([entity.identity for entity in result.entities], ["beta", "gamma", "alpha"])
        self.assertEqual(len(result.entities), 3)
        self.assertIsNone(result.pairwise)

    def test_repeated_typed_comparisons_are_deterministic_and_non_mutating(self) -> None:
        _, _, runs = self.multi_session_model_runs()
        filters = BenchmarkStatisticsFilters(session_id=2)
        request = BenchmarkModelComparisonRequest(
            ("Beta", "Alpha"),
            filters=filters,
            generated_at="fixed",
        )
        snapshots_before = copy.deepcopy([aggregate.run.__dict__ for aggregate in runs])

        first = self.comparisons.compare_benchmark_models(request, runs)
        second = self.comparisons.compare_benchmark_models(request, runs)

        self.assertEqual(first, second)
        self.assertEqual(first.warnings, second.warnings)
        self.assertEqual(request.filters, filters)
        self.assertEqual(request.selected_models, ("Beta", "Alpha"))
        self.assertEqual([aggregate.run.__dict__ for aggregate in runs], snapshots_before)

    def test_scoreboard_model_comparison_is_source_separated_and_deterministic(self) -> None:
        entries, batches = self.scoreboard_entries()
        entries_before = [entry.entry.__dict__.copy() for entry in entries]

        result = self.comparisons.compare_scoreboard_models(
            ScoreboardModelComparisonRequest(("Beta", "Alpha")),
            entries,
            batches=batches,
        )

        self.assertEqual(result.source_family, ComparisonSourceFamily.SCOREBOARD)
        self.assertEqual(result.selected_models, ("beta", "alpha"))
        self.assertEqual([entity.record_count for entity in result.entities], [1, 2])
        self.assertEqual([entity.scored_count for entity in result.entities], [1, 2])
        self.assertEqual(result.entities[0].score.mean, 2.0)
        self.assertEqual(result.entities[1].score.median, 4.5)
        self.assertEqual(result.entities[0].represented_import_batches, ("July",))
        self.assertFalse(hasattr(result.entities[0], "review"))
        self.assertTrue(any(row.category.startswith("consistency:") for row in result.categorical_comparisons))
        self.assertIsNotNone(result.pairwise)
        assert result.pairwise is not None
        self.assertEqual(result.pairwise.baseline_entity, "beta")
        metric = result.pairwise.metric("mean_score")
        self.assertIsNotNone(metric)
        assert metric is not None
        self.assertEqual(metric.absolute_delta, 2.5)
        self.assertEqual([entry.entry.__dict__ for entry in entries], entries_before)

    def test_scoreboard_pairwise_boundary_values_remain_unavailable_or_zero(self) -> None:
        def compare(
            first_score: float | None,
            first_speed: float | None,
            second_score: float | None,
            second_speed: float | None,
        ) -> ScoreboardModelComparisonResult:
            entries = (
                self.make_scoreboard_entry(
                    20,
                    model="First",
                    score=first_score,
                    speed=first_speed,
                ),
                self.make_scoreboard_entry(
                    21,
                    model="Second",
                    score=second_score,
                    speed=second_speed,
                ),
            )
            return self.comparisons.compare_scoreboard_models(
                ScoreboardModelComparisonRequest(("First", "Second"), generated_at="fixed"),
                entries,
            )

        zero_baseline = compare(0.0, 0.0, 2.0, 10.0)
        self.assertIsNotNone(zero_baseline.pairwise)
        assert zero_baseline.pairwise is not None
        zero_score = zero_baseline.pairwise.metric("mean_score")
        zero_speed = zero_baseline.pairwise.metric("mean_tokens_per_second")
        assert zero_score is not None and zero_speed is not None
        self.assertEqual(zero_score.absolute_delta, 2.0)
        self.assertIsNone(zero_score.percentage_delta)
        self.assertEqual(zero_score.unavailable_reason, "baseline is zero")
        self.assertEqual(zero_speed.absolute_delta, 10.0)
        self.assertIsNone(zero_speed.percentage_delta)
        self.assertEqual(zero_speed.unavailable_reason, "baseline is zero")

        missing_baseline = compare(None, None, 2.0, 10.0)
        assert missing_baseline.pairwise is not None
        for metric_name in ("mean_score", "mean_tokens_per_second"):
            metric = missing_baseline.pairwise.metric(metric_name)
            assert metric is not None
            self.assertIsNone(metric.absolute_delta)
            self.assertIsNone(metric.percentage_delta)
            self.assertEqual(metric.unavailable_reason, "one or both values unavailable")

        missing_compared = compare(2.0, 10.0, None, None)
        assert missing_compared.pairwise is not None
        for metric_name in ("mean_score", "mean_tokens_per_second"):
            metric = missing_compared.pairwise.metric(metric_name)
            assert metric is not None
            self.assertIsNone(metric.absolute_delta)
            self.assertIsNone(metric.percentage_delta)
            self.assertEqual(metric.unavailable_reason, "one or both values unavailable")

        tie = compare(2.0, 10.0, 2.0, 10.0)
        assert tie.pairwise is not None
        tie_score = tie.pairwise.metric("mean_score")
        tie_speed = tie.pairwise.metric("mean_tokens_per_second")
        assert tie_score is not None and tie_speed is not None
        self.assertEqual(tie_score.absolute_delta, 0.0)
        self.assertEqual(tie_score.percentage_delta, 0.0)
        self.assertEqual(tie_speed.absolute_delta, 0.0)
        self.assertEqual(tie_speed.percentage_delta, 0.0)

    def test_typed_comparison_outcomes_and_selection_validation(self) -> None:
        with self.assertRaises(ValueError):
            BenchmarkModelComparisonRequest(("Alpha", " alpha "))

        empty = self.comparisons.compare_scoreboard_models(
            ScoreboardModelComparisonRequest(()),
            (),
        )
        self.assertEqual(empty.state, ComparisonResultState.NO_SOURCE_DATA)
        self.assertIn(ComparisonWarningCode.NO_SOURCE_DATA, {warning.code for warning in empty.warnings})

        entries, batches = self.scoreboard_entries()
        unavailable = self.comparisons.compare_scoreboard_models(
            ScoreboardModelComparisonRequest(("Alpha", "Missing")),
            entries,
            batches=batches,
        )
        self.assertEqual(unavailable.state, ComparisonResultState.SELECTED_SUBJECTS_UNAVAILABLE)
        self.assertEqual(unavailable.entities[1].record_count, 0)
        self.assertIn(
            ComparisonWarningCode.SELECTED_SUBJECT_UNAVAILABLE,
            {warning.code for warning in unavailable.warnings},
        )

        with self.assertRaises(TypeError):
            self.comparisons.compare_scoreboard_models(
                ScoreboardModelComparisonRequest(("Alpha", "Beta")),
                self.model_runs(),
            )
        with self.assertRaises(TypeError):
            self.comparisons.discover_scoreboard_model_subjects(
                self.model_runs(),
            )
        with self.assertRaises(TypeError):
            self.comparisons.discover_benchmark_model_subjects(
                entries,
            )

    def test_benchmark_discovery_distinguishes_empty_filtered_and_deleted_sources(self) -> None:
        empty_filters = (
            BenchmarkStatisticsFilters(benchmark="Missing benchmark"),
            BenchmarkStatisticsFilters(hardware="Missing hardware"),
            BenchmarkStatisticsFilters(date_from="2099-01-01"),
            BenchmarkStatisticsFilters(session="Missing session"),
            BenchmarkStatisticsFilters(include_deleted=True),
        )
        for filters in empty_filters:
            first = self.comparisons.discover_benchmark_model_subjects((), filters=filters)
            second = self.comparisons.discover_benchmark_model_subjects((), filters=filters)
            self.assertEqual(first.state, ComparisonResultState.NO_SOURCE_DATA)
            self.assertNotEqual(first.state, ComparisonResultState.FILTERS_NO_RECORDS)
            self.assertEqual(first.subjects, ())
            self.assertEqual(first.warnings, second.warnings)

        populated = (
            self.make_run(
                100,
                model="Kept",
                benchmark="Kept benchmark",
                session=BenchmarkSession("Kept", id=100),
                score=3.0,
                speed=30.0,
            ),
        )
        filtered_first = self.comparisons.discover_benchmark_model_subjects(
            populated,
            filters=BenchmarkStatisticsFilters(benchmark="Missing benchmark"),
        )
        filtered_second = self.comparisons.discover_benchmark_model_subjects(
            populated,
            filters=BenchmarkStatisticsFilters(benchmark="Missing benchmark"),
        )
        self.assertEqual(filtered_first.state, ComparisonResultState.FILTERS_NO_RECORDS)
        self.assertNotEqual(filtered_first.state, ComparisonResultState.NO_SOURCE_DATA)
        self.assertEqual(filtered_first.subjects, ())
        self.assertEqual(filtered_first.warnings, filtered_second.warnings)

        deleted_only = (
            self.make_run(
                101,
                model="Deleted",
                benchmark="Deleted benchmark",
                session=BenchmarkSession("Deleted", id=101),
                score=3.0,
                speed=30.0,
                deleted=True,
            ),
        )
        default_discovery = self.comparisons.discover_benchmark_model_subjects(deleted_only)
        included_discovery = self.comparisons.discover_benchmark_model_subjects(
            deleted_only,
            filters=BenchmarkStatisticsFilters(include_deleted=True),
        )
        self.assertEqual(default_discovery.state, ComparisonResultState.FILTERS_NO_RECORDS)
        self.assertNotEqual(default_discovery.state, ComparisonResultState.NO_SOURCE_DATA)
        self.assertEqual(default_discovery.subjects, ())
        self.assertEqual(included_discovery.state, ComparisonResultState.READY)
        self.assertEqual([subject.identity for subject in included_discovery], ["deleted"])

    def test_scoreboard_discovery_distinguishes_empty_and_filtered_sources(self) -> None:
        empty_first = self.comparisons.discover_scoreboard_model_subjects(
            (),
            batches=(),
            filters=ScoreboardStatisticsFilters(batch_id=999),
        )
        empty_second = self.comparisons.discover_scoreboard_model_subjects(
            (),
            batches=(),
            filters=ScoreboardStatisticsFilters(batch_id=999),
        )
        self.assertEqual(empty_first.state, ComparisonResultState.NO_SOURCE_DATA)
        self.assertNotEqual(empty_first.state, ComparisonResultState.FILTERS_NO_RECORDS)
        self.assertEqual(empty_first.subjects, ())
        self.assertEqual(empty_first.warnings, empty_second.warnings)

        entries, batches = self.scoreboard_entries()
        filtered_first = self.comparisons.discover_scoreboard_model_subjects(
            entries,
            batches=batches,
            filters=ScoreboardStatisticsFilters(batch_id=999),
        )
        filtered_second = self.comparisons.discover_scoreboard_model_subjects(
            entries,
            batches=batches,
            filters=ScoreboardStatisticsFilters(batch_id=999),
        )
        self.assertEqual(filtered_first.state, ComparisonResultState.FILTERS_NO_RECORDS)
        self.assertNotEqual(filtered_first.state, ComparisonResultState.NO_SOURCE_DATA)
        self.assertEqual(filtered_first.subjects, ())
        self.assertEqual(filtered_first.warnings, filtered_second.warnings)

    def test_explicit_models_unavailable_when_other_filtered_data_exists(self) -> None:
        runs = (
            self.make_run(
                110,
                model="C",
                benchmark="Kept benchmark",
                session=BenchmarkSession("C", id=110),
                score=3.0,
                speed=30.0,
            ),
        )
        filters = BenchmarkStatisticsFilters(benchmark="Kept benchmark")
        self.assertEqual(
            len(self.comparisons.statistics.select_benchmark_runs(runs, filters=filters)),
            1,
        )

        result = self.comparisons.compare_benchmark_models(
            BenchmarkModelComparisonRequest(("A", "B"), filters=filters),
            runs,
        )

        self.assertEqual(result.state, ComparisonResultState.SELECTED_SUBJECTS_UNAVAILABLE)
        self.assertNotEqual(result.state, ComparisonResultState.FILTERS_NO_RECORDS)
        self.assertEqual([entity.identity for entity in result.entities], ["a", "b"])
        self.assertEqual([entity.record_count for entity in result.entities], [0, 0])
        unavailable_subjects = [
            warning.subject_identity
            for warning in result.warnings
            if warning.code is ComparisonWarningCode.SUBJECT_HAS_NO_RECORDS
        ]
        self.assertEqual(unavailable_subjects, ["a", "b"])

    def test_explicit_sessions_unavailable_when_other_filtered_data_exists(self) -> None:
        first = BenchmarkSession("First", id=111)
        second = BenchmarkSession("Second", id=112)
        other = BenchmarkSession("Other", id=113)
        runs = (
            self.make_run(
                111,
                model="C",
                benchmark="Kept benchmark",
                session=other,
                score=3.0,
                speed=30.0,
            ),
        )
        filters = BenchmarkStatisticsFilters(benchmark="Kept benchmark")
        self.assertEqual(
            len(self.comparisons.statistics.select_benchmark_runs(runs, filters=filters)),
            1,
        )

        result = self.comparisons.compare_sessions(
            (first, second),
            runs,
            filters=filters,
            generated_at="fixed",
        )

        self.assertEqual(result.state, ComparisonResultState.SELECTED_SUBJECTS_UNAVAILABLE)
        self.assertNotEqual(result.state, ComparisonResultState.FILTERS_NO_RECORDS)
        self.assertEqual(
            [entity.identity for entity in result.entities],
            ["session:111", "session:112"],
        )
        self.assertEqual([entity.record_count for entity in result.entities], [0, 0])
        unavailable_subjects = [
            warning.subject_identity
            for warning in result.warnings
            if warning.code is ComparisonWarningCode.SUBJECT_HAS_NO_RECORDS
        ]
        self.assertEqual(unavailable_subjects, ["session:111", "session:112"])

    def test_session_comparison_aligns_models_benchmarks_and_pairs(self) -> None:
        first = BenchmarkSession("First", description="first metadata", started_at="2026-07-01", id=1)
        second = BenchmarkSession("Second", description="second metadata", id=2)
        runs = (
            self.make_run(1, model="M1", benchmark="A", session=first, score=2.0, speed=10.0),
            self.make_run(2, model="M2", benchmark="B", session=first, score=3.0, speed=20.0),
            self.make_run(3, model="M1", benchmark="A", session=second, score=4.0, speed=30.0),
            self.make_run(4, model="M2", benchmark="C", session=second, score=5.0, speed=40.0),
            self.make_run(5, model="M3", benchmark="C", session=second, score=1.0, speed=5.0, deleted=True),
        )
        result = self.comparisons.compare_sessions((first, second), runs, generated_at="fixed")

        self.assertIsInstance(result, SessionComparisonResult)
        self.assertEqual(result.metadata.selected_entities, ("First", "Second"))
        self.assertEqual([entity.record_count for entity in result.entities], [2, 2])
        self.assertEqual(result.alignment.shared_models, ("M1", "M2"))
        self.assertEqual(result.alignment.shared_benchmarks, ("A",))
        self.assertEqual(result.alignment.shared_model_benchmark_pair_count, 1)
        self.assertEqual(result.alignment.shared_model_benchmark_pairs, (("M1", "A"),))
        self.assertEqual(result.alignment.non_overlapping_benchmarks["session:1"], ("B",))
        self.assertEqual(result.alignment.non_overlapping_benchmarks["session:2"], ("C",))
        self.assertEqual(result.selected_sessions[0].description, "first metadata")

        complete_runs = tuple(
            replace(
                aggregate,
                score=replace(
                    aggregate.score,
                    accuracy_score=4.0,
                    depth_score=3.0,
                    signal_noise_score=4.0,
                    actionability_score=4.0,
                    seniority_score=3.0,
                ),
            )
            for aggregate in runs
            if aggregate.score is not None
        )
        complete_result = self.comparisons.compare_sessions(
            (first, second),
            complete_runs,
            generated_at="fixed",
        )
        self.assertEqual(complete_result.state, ComparisonResultState.READY)
        self.assertEqual([entity.record_count for entity in complete_result.entities], [2, 2])
        self.assertEqual(
            [entity.label for entity in complete_result.entities],
            ["First", "Second"],
        )
        self.assertIsNotNone(complete_result.pairwise)
        assert complete_result.pairwise is not None
        self.assertEqual(complete_result.pairwise.baseline_entity, "session:1")
        self.assertEqual(complete_result.pairwise.comparison_entity, "session:2")
        score_delta = complete_result.pairwise.metric("mean_overall_score")
        speed_delta = complete_result.pairwise.metric("mean_tokens_per_second")
        assert score_delta is not None and speed_delta is not None
        self.assertEqual(score_delta.absolute_delta, 2.0)
        self.assertAlmostEqual(score_delta.percentage_delta or 0.0, 80.0)
        self.assertEqual(speed_delta.absolute_delta, 20.0)
        self.assertAlmostEqual(speed_delta.percentage_delta or 0.0, (20.0 / 15.0) * 100.0)

        zero_first = BenchmarkSession("Zero First", id=71)
        zero_second = BenchmarkSession("Zero Second", id=72)
        zero_result = self.comparisons.compare_sessions(
            (zero_first, zero_second),
            (
                self.make_run(71, model="M1", benchmark="A", session=zero_first, score=0.0, speed=0.0),
                self.make_run(72, model="M2", benchmark="A", session=zero_second, score=2.0, speed=10.0),
            ),
            generated_at="fixed",
        )
        self.assertIsNotNone(zero_result.pairwise)
        assert zero_result.pairwise is not None
        for metric_name in ("mean_overall_score", "mean_tokens_per_second"):
            metric = zero_result.pairwise.metric(metric_name)
            assert metric is not None
            self.assertIsNone(metric.percentage_delta)
            self.assertEqual(metric.unavailable_reason, "baseline is zero")

    def test_session_comparison_reports_no_source_data_without_raising(self) -> None:
        first = self.catalog.create_session(BenchmarkSession("First"))
        second = self.catalog.create_session(BenchmarkSession("Second"))

        first_result = self.comparisons.compare_sessions(runs=(), generated_at="fixed")
        second_result = self.comparisons.compare_sessions(runs=(), generated_at="fixed")

        self.assertEqual(first_result.state, ComparisonResultState.NO_SOURCE_DATA)
        self.assertNotEqual(first_result.state, ComparisonResultState.READY)
        self.assertEqual(first_result.warnings, second_result.warnings)
        self.assertEqual(
            [entity.identity for entity in first_result.entities],
            [f"session:{first.id}", f"session:{second.id}"],
        )
        self.assertEqual([entity.record_count for entity in first_result.entities], [0, 0])
        self.assertEqual(
            {
                warning.code
                for warning in first_result.warnings
            },
            {
                ComparisonWarningCode.NO_SOURCE_DATA,
                ComparisonWarningCode.SUBJECT_HAS_NO_RECORDS,
                ComparisonWarningCode.SUBJECT_HAS_NO_SCORE_VALUES,
                ComparisonWarningCode.SUBJECT_HAS_NO_THROUGHPUT_VALUES,
                ComparisonWarningCode.SUBJECT_HAS_NO_REVIEW_DATA,
            },
        )
        self.assertTrue(
            all(
                "session" in warning.message.casefold()
                for warning in first_result.warnings
                if warning.code
                in {
                    ComparisonWarningCode.SUBJECT_HAS_NO_RECORDS,
                    ComparisonWarningCode.SUBJECT_HAS_NO_SCORE_VALUES,
                    ComparisonWarningCode.SUBJECT_HAS_NO_THROUGHPUT_VALUES,
                    ComparisonWarningCode.SUBJECT_HAS_NO_REVIEW_DATA,
                }
            )
        )

    def test_session_comparison_preserves_no_source_state_with_active_or_ignored_filters(self) -> None:
        first = self.catalog.create_session(BenchmarkSession("First"))
        second = self.catalog.create_session(BenchmarkSession("Second"))
        active_filters = BenchmarkStatisticsFilters(benchmark="Missing benchmark")

        first_result = self.comparisons.compare_sessions(
            runs=(),
            filters=active_filters,
            generated_at="fixed",
        )
        second_result = self.comparisons.compare_sessions(
            runs=(),
            filters=active_filters,
            generated_at="fixed",
        )

        self.assertEqual(first_result.state, ComparisonResultState.NO_SOURCE_DATA)
        self.assertEqual(first_result.warnings, second_result.warnings)
        self.assertNotIn(
            ComparisonWarningCode.FILTERS_NO_RECORDS,
            {warning.code for warning in first_result.warnings},
        )

        ignored_filters = BenchmarkStatisticsFilters(
            session="Ignored session filter",
            session_id=999,
            include_deleted=True,
        )
        ignored_result = self.comparisons.compare_sessions(
            runs=(),
            filters=ignored_filters,
            generated_at="fixed",
        )
        self.assertEqual(ignored_result.state, ComparisonResultState.NO_SOURCE_DATA)
        self.assertEqual(ignored_result.filters, ignored_filters)
        self.assertIn("session", ignored_result.metadata.active_filters)
        self.assertIn("session_id", ignored_result.metadata.active_filters)

    def test_session_comparison_marks_selected_sessions_without_records(self) -> None:
        first = BenchmarkSession("First", id=11)
        second = BenchmarkSession("Second", id=12)
        other = BenchmarkSession("Other", id=13)
        runs = (
            self.make_run(
                30,
                model="Alpha",
                benchmark="Other",
                session=other,
                score=3.0,
                speed=30.0,
            ),
        )

        result = self.comparisons.compare_sessions(
            (first, second),
            runs,
            generated_at="fixed",
        )

        self.assertEqual(result.state, ComparisonResultState.SELECTED_SUBJECTS_UNAVAILABLE)
        self.assertEqual(
            [entity.identity for entity in result.entities],
            ["session:11", "session:12"],
        )
        self.assertEqual([entity.record_count for entity in result.entities], [0, 0])
        self.assertEqual(
            {
                warning.subject_identity
                for warning in result.warnings
                if warning.code is ComparisonWarningCode.SUBJECT_HAS_NO_RECORDS
            },
            {"session:11", "session:12"},
        )

    def test_session_comparison_distinguishes_filtered_no_data(self) -> None:
        first = BenchmarkSession("First", id=21)
        second = BenchmarkSession("Second", id=22)
        runs = (
            self.make_run(
                31,
                model="Alpha",
                benchmark="Kept",
                session=first,
                score=3.0,
                speed=30.0,
            ),
            self.make_run(
                32,
                model="Beta",
                benchmark="Kept",
                session=second,
                score=4.0,
                speed=40.0,
            ),
        )
        filters = BenchmarkStatisticsFilters(benchmark="Does not match")

        first_result = self.comparisons.compare_sessions(
            (first, second),
            runs,
            filters=filters,
            generated_at="fixed",
        )
        second_result = self.comparisons.compare_sessions(
            (first, second),
            runs,
            filters=filters,
            generated_at="fixed",
        )

        self.assertEqual(first_result.state, ComparisonResultState.FILTERS_NO_RECORDS)
        self.assertNotEqual(first_result.state, ComparisonResultState.READY)
        self.assertEqual(first_result.warnings, second_result.warnings)
        self.assertIn(
            ComparisonWarningCode.FILTERS_NO_RECORDS,
            {warning.code for warning in first_result.warnings},
        )
        self.assertEqual([entity.record_count for entity in first_result.entities], [0, 0])

    def test_session_comparison_marks_one_unavailable_without_zero_filling(self) -> None:
        first = BenchmarkSession("First", id=41)
        second = BenchmarkSession("Second", id=42)
        runs = (
            self.make_run(
                41,
                model="Alpha",
                benchmark="Shared",
                session=first,
                score=4.0,
                speed=40.0,
            ),
        )

        result = self.comparisons.compare_sessions(
            (first, second),
            runs,
            generated_at="fixed",
        )

        self.assertEqual(result.state, ComparisonResultState.SELECTED_SUBJECTS_UNAVAILABLE)
        self.assertEqual([entity.label for entity in result.entities], ["First", "Second"])
        self.assertEqual(result.entities[0].overall_score.mean, 4.0)
        self.assertIsNone(result.entities[1].overall_score.mean)
        self.assertIsNotNone(result.pairwise)
        assert result.pairwise is not None
        score_delta = result.pairwise.metric("mean_overall_score")
        speed_delta = result.pairwise.metric("mean_tokens_per_second")
        assert score_delta is not None and speed_delta is not None
        self.assertIsNone(score_delta.absolute_delta)
        self.assertIsNone(speed_delta.absolute_delta)
        self.assertIn(
            ComparisonWarningCode.SUBJECT_HAS_NO_RECORDS,
            {warning.code for warning in result.warnings},
        )

    def test_session_comparison_warns_for_missing_session_metrics(self) -> None:
        first = BenchmarkSession("First", id=61)
        second = BenchmarkSession("Second", id=62)
        runs = (
            self.make_run(
                61,
                model="Alpha",
                benchmark="Shared",
                session=first,
                score=4.0,
                speed=40.0,
            ),
            replace(
                self.make_run(
                    62,
                    model="Beta",
                    benchmark="Shared",
                    session=second,
                    score=None,
                    speed=None,
                ),
                score=None,
            ),
        )

        result = self.comparisons.compare_sessions(
            (first, second),
            runs,
            generated_at="fixed",
        )

        self.assertEqual(result.state, ComparisonResultState.READY_WITH_MISSING_VALUES)
        self.assertEqual([entity.record_count for entity in result.entities], [1, 1])
        self.assertEqual(result.entities[0].overall_score.mean, 4.0)
        self.assertIsNone(result.entities[1].overall_score.mean)
        warning_codes = {
            warning.code
            for warning in result.entities[1].warnings
        }
        self.assertEqual(
            warning_codes,
            {
                ComparisonWarningCode.SUBJECT_HAS_NO_SCORE_VALUES,
                ComparisonWarningCode.SUBJECT_HAS_NO_THROUGHPUT_VALUES,
                ComparisonWarningCode.SUBJECT_HAS_NO_REVIEW_DATA,
            },
        )

    def test_session_comparison_reports_insufficient_subjects_as_typed_state(self) -> None:
        only = BenchmarkSession("Only", id=51)

        result = self.comparisons.compare_sessions((only,), runs=(), generated_at="fixed")

        self.assertEqual(result.state, ComparisonResultState.INSUFFICIENT_SUBJECTS)
        self.assertIn(
            ComparisonWarningCode.INSUFFICIENT_SUBJECTS,
            {warning.code for warning in result.warnings},
        )

        populated = self.comparisons.compare_sessions(
            (only,),
            self.model_runs(),
            generated_at="fixed",
        )
        self.assertEqual(populated.state, ComparisonResultState.INSUFFICIENT_SUBJECTS)

    def test_session_comparison_explicit_empty_selection_is_insufficient(self) -> None:
        empty_first = self.comparisons.compare_sessions((), runs=(), generated_at="fixed")
        empty_second = self.comparisons.compare_sessions((), runs=(), generated_at="fixed")
        self.assertEqual(empty_first.state, ComparisonResultState.INSUFFICIENT_SUBJECTS)
        self.assertEqual(empty_first.warnings, empty_second.warnings)
        self.assertEqual(empty_first.selected_sessions, ())
        self.assertEqual(empty_first.entities, ())

        populated_first, populated_second, populated_runs = self.multi_session_model_runs()
        populated = self.comparisons.compare_sessions(
            (),
            populated_runs,
            generated_at="fixed",
        )
        self.assertEqual(populated.state, ComparisonResultState.INSUFFICIENT_SUBJECTS)
        self.assertEqual(populated.selected_sessions, ())
        self.assertEqual(populated.entities, ())
        self.assertEqual(
            [session.id for session in (populated_first, populated_second)],
            [1, 2],
        )

    def test_comparison_markdown_and_staged_writer_use_structured_results(self) -> None:
        model_result = self.comparisons.compare_models(("Alpha", "Beta"), self.model_runs(), generated_at="fixed")
        model_markdown = render_markdown(model_result)
        self.assertIn("## Broad comparison", model_markdown)
        self.assertIn("## Aligned benchmark comparison", model_markdown)
        self.assertIn("## Pairwise deltas", model_markdown)
        self.assertIn("## Model ranking", model_markdown)
        self.assertIn("## Methodology and unavailable values", model_markdown)
        self.assertIn("Not available", model_markdown)
        self.assertNotIn("private output", model_markdown)

        sessions = (BenchmarkSession("First", id=1), BenchmarkSession("Second", id=2))
        session_runs = (
            self.make_run(10, model="M", benchmark="A", session=sessions[0], score=1.0, speed=1.0),
            self.make_run(11, model="M", benchmark="A", session=sessions[1], score=2.0, speed=2.0),
        )
        session_result = self.comparisons.compare_sessions(sessions, session_runs, generated_at="fixed")
        session_markdown = self.reporting.render_session_comparison_markdown(session_result)
        self.assertIn("## Selected session metadata", session_markdown)
        self.assertIn("Shared model/benchmark pair summaries", session_markdown)

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "comparison.md"
            write_result = self.reporting.write_markdown_report(model_result, destination)
            self.assertTrue(write_result.succeeded)
            content = destination.read_bytes()
            self.assertFalse(content.startswith(b"\xef\xbb\xbf"))
            self.assertIn("# Model Comparison", content.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
