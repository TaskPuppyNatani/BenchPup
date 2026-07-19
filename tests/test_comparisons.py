from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine import ComparisonService, ReportingService
from engine.comparisons import ModelComparisonResult, SessionComparisonResult
from engine.database import EngineDatabase
from engine.domain import BenchmarkRun, BenchmarkSession, ReviewScore
from engine.reporting import BenchmarkRunAggregate, render_markdown
from engine.services import BenchmarkService, CatalogService


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
