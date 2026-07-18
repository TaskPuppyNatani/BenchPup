import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine.database import EngineDatabase
from engine.domain import (
    BenchmarkDefinition,
    BenchmarkRun,
    BenchmarkSession,
    PromptTemplate,
    ReviewScore,
    RunAttachment,
    ScoreboardEntry,
    ScoreboardImportBatch,
)
from engine.reporting import (
    BenchmarkReportFilters,
    BenchmarkRunAggregate,
    ReportWriteStatus,
    ScoreboardReportFilters,
    build_benchmark_run_report,
    build_model_leaderboard,
    build_scoreboard_report,
    render_benchmark_run_markdown,
    render_model_leaderboard_markdown,
    render_scoreboard_markdown,
    write_markdown_report,
)
from engine.services import BenchmarkService, CatalogService


class ReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database = EngineDatabase(Path(self.directory.name) / "reporting.db")
        database.migrate()
        self.catalog = CatalogService(database)
        self.service = BenchmarkService(database, self.catalog)
        self.session = self.catalog.sessions.create(BenchmarkSession(title="July session"))
        self.definition = self.catalog.benchmark_definitions.create(
            BenchmarkDefinition(name="Review benchmark", file_path="review.py", benchmark_type="code_review")
        )
        prompt_text = "Review the code carefully."
        self.prompt = self.catalog.prompt_templates.create(
            PromptTemplate(
                name="Review prompt",
                version="2",
                prompt_text=prompt_text,
                prompt_hash=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
                benchmark_type="code_review",
            )
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def make_run(
        self,
        *,
        model_name: str = "Alpha",
        score: float | None = 4.5,
        created_at: str = "2026-07-10T12:00:00+00:00",
        tokens_per_second: float | None = 100.0,
        hallucination: str = "Low",
        reliability: str = "High",
        benchmark_type: str = "code_review",
        with_context: bool = True,
    ) -> BenchmarkRunAggregate:
        run = BenchmarkRun(
            raw_model_output="Résumé: useful model output",
            session_id=self.session.id if with_context else None,
            model_profile_id=None,
            benchmark_definition_id=self.definition.id if with_context else None,
            prompt_template_id=self.prompt.id if with_context else None,
            model_snapshot={"model_name": model_name, "backend": "LM Studio", "tokens_per_second": tokens_per_second},
            benchmark_snapshot={
                "name": self.definition.name if with_context else "Review benchmark",
                "file_path": self.definition.file_path,
                "benchmark_type": benchmark_type,
            },
            prompt_snapshot=(
                {"name": self.prompt.name, "version": self.prompt.version, "prompt_hash": self.prompt.prompt_hash,
                 "prompt_text": self.prompt.prompt_text}
                if with_context else {}
            ),
            hardware_snapshot=(
                {"name": "Rig A", "cpu": "CPU", "gpu": "GPU", "operating_system": "Windows"}
                if with_context else {}
            ),
            prompt_text=self.prompt.prompt_text if with_context else "",
            fingerprint=f"{model_name}-{created_at}-{score}-{benchmark_type}",
            created_at=created_at,
        )
        review = ReviewScore(
            run_id=0,
            accuracy_score=score,
            hallucination_level=hallucination,
            reliability_level=reliability,
            overall_score=score,
            verdict="Useful",
            strengths="Clear reasoning",
            weaknesses="Could be shorter",
            notes="Reviewed in July",
        ) if score is not None else None
        return BenchmarkRunAggregate(run, review, self.session if with_context else None)

    def test_detailed_report_complete_records_and_default_redaction(self) -> None:
        aggregate = self.make_run()
        aggregate = BenchmarkRunAggregate(
            aggregate.run,
            aggregate.score,
            aggregate.session,
            (
                RunAttachment(
                    run_id=1,
                    attachment_type="log",
                    file_path="attachments/result.log",
                    original_filename="result.log",
                    notes="metadata only",
                ),
            ),
        )

        report = build_benchmark_run_report([aggregate], generated_at="2026-07-18T00:00:00+00:00")
        markdown = render_benchmark_run_markdown(report)

        self.assertEqual(report.metadata.record_count, 1)
        self.assertEqual(report.summary.scored_count, 1)
        self.assertEqual(report.summary.average, 4.5)
        self.assertEqual(report.model_sections[0].model_name, "Alpha")
        self.assertIn("review.py", markdown)
        self.assertIn("LM Studio", markdown)
        self.assertIn("Review prompt", markdown)
        self.assertIn("Clear reasoning", markdown)
        self.assertNotIn("Review the code carefully.", markdown)
        self.assertNotIn("Résumé: useful model output", markdown)
        self.assertNotIn("result.log", markdown)

    def test_detailed_report_can_include_prompt_output_and_attachment_metadata(self) -> None:
        aggregate = self.make_run()
        aggregate = BenchmarkRunAggregate(
            aggregate.run,
            aggregate.score,
            aggregate.session,
            (RunAttachment(1, "screenshot", "screenshots/a.png", "a.png", "caption"),),
        )
        report = build_benchmark_run_report(
            [aggregate],
            include_prompt_text=True,
            include_raw_model_output=True,
            include_attachment_metadata=True,
        )
        markdown = render_benchmark_run_markdown(report)
        self.assertIn("Review the code carefully.", markdown)
        self.assertIn("Résumé: useful model output", markdown)
        self.assertIn("a.png", markdown)
        self.assertNotIn("<binary>", markdown)

    def test_detailed_report_handles_missing_optional_metadata(self) -> None:
        aggregate = self.make_run(with_context=False, score=None)
        report = build_benchmark_run_report([aggregate])
        item = report.records[0]
        markdown = render_benchmark_run_markdown(report)

        self.assertIsNone(item.session)
        self.assertIsNone(item.hardware)
        self.assertEqual(item.prompt.name, "")
        self.assertIn("Review: Unavailable", markdown)
        self.assertIn("Model settings", markdown)

    def test_soft_deleted_runs_are_excluded_by_default(self) -> None:
        active = self.make_run(model_name="Active")
        deleted_run = copy.deepcopy(self.make_run(model_name="Deleted").run)
        deleted_run.is_deleted = True
        deleted = BenchmarkRunAggregate(deleted_run, ReviewScore(run_id=2, overall_score=5.0))

        report = build_benchmark_run_report([active, deleted])
        included = build_benchmark_run_report(
            [active, deleted],
            filters=BenchmarkReportFilters(include_deleted=True),
        )
        self.assertEqual([item.model_name for item in report.records], ["Active"])
        self.assertEqual(len(included.records), 2)

    def test_scoreboard_report_groups_batches_and_keeps_unbatched_entries_separate(self) -> None:
        batch = ScoreboardImportBatch(name="Historical July", source_file="july.csv", imported_at="2026-07-10")
        batched = ScoreboardEntry(
            model_name="Qwen",
            score=4.5,
            temperature=0.3,
            moe_experts="8",
            context_length=32768,
            tokens_per_second=120,
            review_quality="Strong",
            hallucination_level="Low",
            consistency="High",
            reliability_score="High",
            verdict="Useful",
            notes="Primary",
            source_file="july.csv",
            import_batch_id=10,
        )
        batch = ScoreboardImportBatch(**{**batch.__dict__, "id": 10})
        unbatched = ScoreboardEntry(model_name="Llama", score=None, imported_at="2026-07-11")

        report = build_scoreboard_report(
            [batched, unbatched],
            batches=[batch],
            generated_at="2026-07-18T00:00:00+00:00",
        )
        markdown = render_scoreboard_markdown(report)

        self.assertEqual(report.summary.count, 2)
        self.assertEqual(report.summary.scored_count, 1)
        self.assertEqual([section.label for section in report.batch_sections], ["Historical July", "Unbatched scoreboard entries"])
        self.assertIn("Historical July", markdown)
        self.assertIn("Qwen", markdown)
        self.assertIn("120", markdown)
        self.assertIn("Llama", markdown)

    def test_soft_deleted_scoreboard_entries_and_batches_are_excluded(self) -> None:
        deleted_batch = ScoreboardImportBatch(name="Deleted batch", source_file="old.csv", id=20, is_deleted=True)
        entry = ScoreboardEntry(model_name="Deleted", score=5.0, import_batch_id=20, is_deleted=False)
        deleted_entry = ScoreboardEntry(model_name="Also deleted", score=4.0, is_deleted=True)

        report = build_scoreboard_report([entry, deleted_entry], batches=[deleted_batch])
        included = build_scoreboard_report(
            [entry, deleted_entry],
            batches=[deleted_batch],
            filters=ScoreboardReportFilters(include_deleted=True),
        )
        self.assertEqual(report.entries, ())
        self.assertEqual(len(included.entries), 2)

    def test_model_leaderboard_aggregates_without_zero_filling_and_ranks_deterministically(self) -> None:
        aggregates = [
            self.make_run(model_name="Alpha", score=4.0, tokens_per_second=None),
            self.make_run(model_name="Alpha", score=2.0, tokens_per_second=None, created_at="2026-07-11"),
            self.make_run(model_name="Beta", score=4.0, tokens_per_second=120.0, hallucination="Low", reliability="High"),
            self.make_run(model_name="Beta", score=4.0, tokens_per_second=None, created_at="2026-07-11", hallucination="High", reliability="Medium"),
            self.make_run(model_name="Gamma", score=4.0, tokens_per_second=None),
            self.make_run(model_name="NoScore", score=None, tokens_per_second=None),
        ]
        report = build_model_leaderboard(aggregates)
        by_name = {entry.model_name: entry for entry in report.models}
        markdown = render_model_leaderboard_markdown(report, include_model_details=True)

        self.assertEqual([entry.model_name for entry in report.models], ["Beta", "Gamma", "Alpha", "NoScore"])
        self.assertEqual((by_name["Alpha"].run_count, by_name["Alpha"].scored_run_count), (2, 2))
        self.assertEqual((by_name["Alpha"].average_overall_score, by_name["Alpha"].median_overall_score), (3.0, 3.0))
        self.assertEqual(by_name["Beta"].average_overall_score, 4.0)
        self.assertEqual(by_name["Beta"].median_overall_score, 4.0)
        self.assertEqual(by_name["Beta"].average_tokens_per_second, 120.0)
        self.assertEqual(by_name["NoScore"].scored_run_count, 0)
        self.assertIsNone(by_name["NoScore"].average_overall_score)
        self.assertIsNone(by_name["NoScore"].average_tokens_per_second)
        self.assertEqual(dict(by_name["Beta"].score_distribution), {4.0: 2})
        self.assertEqual(dict(by_name["Beta"].hallucination_distribution), {"Low": 1, "High": 1})
        self.assertEqual(dict(by_name["Beta"].reliability_distribution), {"High": 1, "Medium": 1})
        self.assertIn("Score distribution", markdown)
        self.assertIn("Hallucination distribution", markdown)

    def test_leaderboard_supports_odd_median(self) -> None:
        aggregates = [
            self.make_run(model_name="Odd", score=1.0, created_at="2026-07-10T12:00:00+00:00"),
            self.make_run(model_name="Odd", score=5.0, created_at="2026-07-11T12:00:00+00:00"),
            self.make_run(model_name="Odd", score=3.0, created_at="2026-07-12T12:00:00+00:00"),
        ]

        report = build_model_leaderboard(aggregates)

        self.assertEqual(report.models[0].median_overall_score, 3.0)

    def test_leaderboard_filters_and_source_records_are_not_mutated(self) -> None:
        aggregate = self.make_run(model_name="Filter me", score=4.0, benchmark_type="code_generation")
        original_run = copy.deepcopy(aggregate.run)
        original_score = copy.deepcopy(aggregate.score)

        report = build_model_leaderboard(
            [aggregate],
            filters=BenchmarkReportFilters(benchmark_type="code_generation", model="filter"),
        )

        self.assertEqual(report.models[0].model_name, "Filter me")
        self.assertEqual(aggregate.run, original_run)
        self.assertEqual(aggregate.score, original_score)

    def test_safe_utf8_markdown_write_and_overwrite_protection(self) -> None:
        report = build_benchmark_run_report([self.make_run()], title="Résumé report")
        path = Path(self.directory.name) / "reports" / "report.md"

        first = write_markdown_report(report, path)
        second = write_markdown_report(report, path)
        overwritten = write_markdown_report(report, path, overwrite=True)

        self.assertEqual(first.status, ReportWriteStatus.SUCCESS)
        self.assertEqual(second.status, ReportWriteStatus.OVERWRITE_REQUIRED)
        self.assertEqual(overwritten.status, ReportWriteStatus.SUCCESS)
        self.assertIn("Résumé", path.read_text(encoding="utf-8"))
        self.assertEqual(path.read_bytes().decode("utf-8").count("# Résumé report"), 1)


if __name__ == "__main__":
    unittest.main()
