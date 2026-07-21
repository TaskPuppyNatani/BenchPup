from __future__ import annotations

import copy
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine.database import EngineDatabase
from engine.domain import (
    BenchmarkRun,
    BenchmarkSession,
    HardwareProfile,
    ReviewScore,
    ScoreboardEntry,
    ScoreboardImportBatch,
)
from engine.exporters import export_benchmark_runs_csv, export_scoreboard_csv
from engine.html_reporting import AnalyticsSourceFamily, HtmlAnalyticsReportOptions
from engine.reporting import BenchmarkReportFilters, ScoreboardReportFilters
from engine.services import BenchmarkService, CatalogService
from engine.standard_exports import (
    StandardExportKind,
    StandardExportRequest,
    StandardExportService,
    StandardExportStatus,
)


class StandardExportEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.output = self.root / "exports"
        self.output.mkdir()
        database = EngineDatabase(self.root / "data" / "benchmark.db")
        database.migrate()
        self.catalog = CatalogService(database)
        self.benchmarks = BenchmarkService(database, self.catalog)
        self.exports = StandardExportService(self.benchmarks, self.catalog)
        self.session = self.catalog.sessions.create(BenchmarkSession(title="July session"))
        self.hardware = self.catalog.hardware_profiles.create(
            HardwareProfile(name="Rig A", computer_name="BENCH-01", cpu="CPU", gpu="GPU")
        )
        run = BenchmarkRun(
            raw_model_output="Useful output",
            session_id=self.session.id,
            hardware_profile_id=self.hardware.id,
            model_snapshot={"model_name": "Alpha", "backend": "LM Studio", "tokens_per_second": 100.0},
            benchmark_snapshot={"name": "Review benchmark", "benchmark_type": "code_review"},
            prompt_snapshot={"name": "Review prompt", "prompt_text": "Review carefully."},
            hardware_snapshot={"name": "Rig A", "cpu": "CPU", "gpu": "GPU"},
            prompt_name="Review prompt",
            prompt_text="Review carefully.",
        )
        self.saved_run, _ = self.benchmarks.save_run(
            run,
            ReviewScore(
                run_id=1,
                accuracy_score=4.0,
                hallucination_level="Low",
                reliability_level="High",
                overall_score=4.0,
                verdict="Useful",
            ),
        )
        self.batch = self.catalog.scoreboard_import_batches.create(
            ScoreboardImportBatch(name="July scoreboard", source_file="july.csv")
        )
        self.catalog.scoreboard_entries.create(
            ScoreboardEntry(
                model_name="Alpha",
                score=4.5,
                import_batch_id=self.batch.id,
                source_file="july.csv",
                notes="Historical result",
            )
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _request(self, kind: StandardExportKind, name: str | None = None, **kwargs: object) -> StandardExportRequest:
        return StandardExportRequest(
            kind,
            self.output / (name or kind.value),
            **kwargs,
        )

    def test_preview_is_metadata_only_and_all_enabled_kinds_write(self) -> None:
        cases = (
            (StandardExportKind.BENCHMARK_RUNS_CSV, {}),
            (StandardExportKind.SCOREBOARD_CSV, {}),
            (StandardExportKind.COMBINED_MARKDOWN, {}),
            (StandardExportKind.BENCHMARK_RUN_MARKDOWN, {"run_id": self.saved_run.id}),
            (StandardExportKind.SCOREBOARD_MARKDOWN, {}),
            (StandardExportKind.MODEL_LEADERBOARD_MARKDOWN, {}),
            (StandardExportKind.SESSION_MARKDOWN, {"session_id": self.session.id}),
            (StandardExportKind.HARDWARE_MARKDOWN, {"hardware_profile_id": self.hardware.id}),
            (StandardExportKind.SCOREBOARD_HTML, {}),
            (
                StandardExportKind.HTML_ANALYTICS,
                {"analytics_options": HtmlAnalyticsReportOptions(source_family=AnalyticsSourceFamily.COMBINED)},
            ),
        )
        for kind, values in cases:
            with self.subTest(kind=kind):
                request = self._request(kind, **values)
                preview = self.exports.preview(request)
                self.assertEqual(preview.status, StandardExportStatus.SUCCESS)
                self.assertEqual(preview.record_count, 2 if kind in {StandardExportKind.COMBINED_MARKDOWN, StandardExportKind.HTML_ANALYTICS} else 1)
                self.assertFalse(preview.destination.exists())
                result = self.exports.write(request)
                self.assertTrue(result.succeeded, result)
                self.assertTrue(result.destination.exists())

    def test_preview_and_empty_selection_perform_no_writes(self) -> None:
        request = self._request(
            StandardExportKind.BENCHMARK_RUNS_CSV,
            "empty.csv",
            benchmark_filters=BenchmarkReportFilters(model="Does not exist"),
        )
        before = set(self.output.iterdir())
        preview = self.exports.preview(request)
        self.assertEqual(preview.status, StandardExportStatus.EMPTY_SELECTION)
        self.assertTrue(preview.empty_selection)
        result = self.exports.write(request)
        self.assertEqual(result.status, StandardExportStatus.EMPTY_SELECTION)
        self.assertEqual(set(self.output.iterdir()), before)

    def test_destination_validation_and_extension_are_typed(self) -> None:
        request = StandardExportRequest(StandardExportKind.BENCHMARK_RUNS_CSV, self.output / "without-extension")
        preview = self.exports.preview(request)
        self.assertEqual(preview.destination, (self.output / "without-extension.csv").resolve())
        self.assertFalse(preview.destination.exists())

        missing_parent = self.exports.preview(
            StandardExportRequest(StandardExportKind.BENCHMARK_RUNS_CSV, self.root / "missing" / "runs.csv")
        )
        self.assertEqual(missing_parent.status, StandardExportStatus.INVALID_REQUEST)
        self.assertFalse((self.root / "missing").exists())

        directory_request = self.exports.preview(
            StandardExportRequest(StandardExportKind.BENCHMARK_RUNS_CSV, self.output)
        )
        self.assertEqual(directory_request.status, StandardExportStatus.INVALID_REQUEST)

    def test_overwrite_requires_confirmation_and_confirmed_write_replaces(self) -> None:
        target = self.output / "existing.csv"
        target.write_text("old", encoding="utf-8")
        request = StandardExportRequest(StandardExportKind.BENCHMARK_RUNS_CSV, target)
        refused = self.exports.write(request)
        self.assertEqual(refused.status, StandardExportStatus.OVERWRITE_REQUIRED)
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        replaced = self.exports.write(request, overwrite=True)
        self.assertTrue(replaced.succeeded)
        self.assertIn("Alpha", target.read_text(encoding="utf-8"))

    def test_failed_staging_or_finalization_preserves_existing_destination(self) -> None:
        target = self.output / "protected.csv"
        target.write_text("keep me", encoding="utf-8")
        request = StandardExportRequest(StandardExportKind.BENCHMARK_RUNS_CSV, target)
        with patch("engine.standard_exports.tempfile.mkstemp", side_effect=OSError("no temp space")):
            temporary_failure = self.exports.write(request, overwrite=True)
        self.assertEqual(temporary_failure.status, StandardExportStatus.TEMPORARY_WRITE_FAILED)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me")
        with patch("engine.standard_exports.os.replace", side_effect=OSError("replace denied")):
            finalization_failure = self.exports.write(request, overwrite=True)
        self.assertEqual(finalization_failure.status, StandardExportStatus.FINALIZATION_FAILED)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep me")

    def test_engine_csv_serialization_matches_legacy_cli_exporter(self) -> None:
        legacy_runs = self.output / "legacy-runs.csv"
        legacy_scoreboard = self.output / "legacy-scoreboard.csv"
        export_benchmark_runs_csv(self.benchmarks, legacy_runs)
        export_scoreboard_csv(self.catalog, legacy_scoreboard)
        gui_runs = self.exports.write(self._request(StandardExportKind.BENCHMARK_RUNS_CSV, "gui-runs.csv"))
        gui_scoreboard = self.exports.write(self._request(StandardExportKind.SCOREBOARD_CSV, "gui-scoreboard.csv"))
        self.assertTrue(gui_runs.succeeded)
        self.assertTrue(gui_scoreboard.succeeded)
        self.assertEqual(legacy_runs.read_bytes(), gui_runs.destination.read_bytes())
        self.assertEqual(legacy_scoreboard.read_bytes(), gui_scoreboard.destination.read_bytes())

    def test_jsonl_is_typed_unavailable_and_snapshots_remain_unchanged(self) -> None:
        before = copy.deepcopy(self.saved_run)
        request = self._request(StandardExportKind.JSONL_TRAINING_DATA, "training.jsonl")
        preview = self.exports.preview(request)
        self.assertEqual(preview.status, StandardExportStatus.INVALID_REQUEST)
        self.assertIn("Dataset Builder", preview.message)
        result = self.exports.write(request)
        self.assertEqual(result.status, StandardExportStatus.INVALID_REQUEST)
        current, _score, _attachments = self.benchmarks.get_run(self.saved_run.id or 0)
        self.assertIsNotNone(current)
        self.assertEqual(current.model_snapshot, before.model_snapshot)  # type: ignore[union-attr]
        self.assertEqual(current.benchmark_snapshot, before.benchmark_snapshot)  # type: ignore[union-attr]
        self.assertEqual(current.prompt_snapshot, before.prompt_snapshot)  # type: ignore[union-attr]
        self.assertEqual(current.hardware_snapshot, before.hardware_snapshot)  # type: ignore[union-attr]

    def test_html_analytics_source_modes_keep_record_families_separate(self) -> None:
        expected = (
            (AnalyticsSourceFamily.BENCHMARK_RUNS, "BenchmarkRun", 1),
            (AnalyticsSourceFamily.SCOREBOARD, "ScoreboardEntry", 1),
            (AnalyticsSourceFamily.COMBINED, "BenchmarkRun + ScoreboardEntry", 2),
        )
        for source, family, count in expected:
            with self.subTest(source=source):
                request = self._request(
                    StandardExportKind.HTML_ANALYTICS,
                    f"analytics-{source.value}.html",
                    analytics_options=HtmlAnalyticsReportOptions(source_family=source),
                )
                preview = self.exports.preview(request)
                self.assertEqual(preview.source_record_family, family)
                self.assertEqual(preview.record_count, count)
                result = self.exports.write(request)
                self.assertTrue(result.succeeded, result)


if __name__ == "__main__":
    unittest.main()
