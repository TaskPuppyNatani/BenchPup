import hashlib
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cli import BACK, QuitApplication, TerminalApp
from engine.datasets import DatasetFilters, DatasetPreview, DatasetWriteResult, DatasetWriteStatus, RedactionConfig
from engine.domain import BenchmarkDefinition, BenchmarkSession, HardwareProfile, ModelProfile, PromptTemplate


class DatasetCliTests(unittest.TestCase):
    def app_with(self, answers: list[str]) -> tuple[TerminalApp, list[str]]:
        directory = tempfile.TemporaryDirectory()
        output: list[str] = []
        iterator = iter(answers)
        app = TerminalApp(
            Path(directory.name) / "benchmarks.db",
            input_fn=lambda _: next(iterator),
            output_fn=output.append,
        )
        self.addCleanup(directory.cleanup)
        return app, output

    def seed_catalog(self, app: TerminalApp) -> None:
        app.catalog.sessions.create(BenchmarkSession(title="Dataset session"))
        app.catalog.model_profiles.create(ModelProfile(name="Alpha profile", model_name="Alpha"))
        app.catalog.benchmark_definitions.create(
            BenchmarkDefinition(name="Review", file_path="review.py", benchmark_type="code_review")
        )
        text = "Review this result"
        app.catalog.prompt_templates.create(
            PromptTemplate(
                name="Dataset prompt",
                version="1",
                prompt_text=text,
                prompt_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                benchmark_type="code_review",
            )
        )
        app.catalog.hardware_profiles.create(HardwareProfile(name="Dataset rig"))

    def test_dataset_builder_renders_vertical_menu_without_scoreboard(self) -> None:
        app, output = self.app_with(["b"])

        app.dataset_builder_screen()

        rendered = "\n".join(output)
        self.assertIn("1) Build JSONL Dataset\n2) Preview Eligible Runs\n3) Configure Filters", rendered)
        self.assertIn("5) Validate Existing Dataset", rendered)
        self.assertIn("B) Back\nQA) Quit BenchPup completely", rendered)
        self.assertNotIn("Scoreboard", rendered)

    def test_every_filter_option_updates_session_local_filters(self) -> None:
        app, _ = self.app_with([
            "1", "4.5", "2", "3", "3", "4", "4", "approved", "5", "1", "6", "1",
            "7", "1", "8", "2026-01-01", "2026-01-31", "9", "1", "10", "1",
            "11", "1, 2", "12", "3", "13", "2", "b",
        ])
        self.seed_catalog(app)

        filters = app.dataset_filters_screen(DatasetFilters())

        self.assertEqual(filters.min_overall, 4.5)
        self.assertEqual(filters.max_hallucination, "Medium")
        self.assertEqual(filters.min_reliability, "Medium-High")
        self.assertEqual(filters.verdict, "approved")
        self.assertEqual(filters.benchmark_type, "code_review")
        self.assertEqual(filters.model, "Alpha")
        self.assertEqual(filters.session_id, app.catalog.sessions.list()[0].id)
        self.assertEqual(str(filters.date_from), "2026-01-01")
        self.assertEqual(str(filters.date_to), "2026-01-31")
        self.assertEqual(filters.prompt_template_id, app.catalog.prompt_templates.list()[0].id)
        self.assertEqual(filters.hardware_profile_id, app.catalog.hardware_profiles.list()[0].id)
        self.assertEqual(filters.include_run_ids, frozenset({1, 2}))
        self.assertEqual(filters.exclude_run_ids, frozenset({3}))
        self.assertTrue(filters.keep_source_duplicates)

    def test_filter_validation_and_reset_are_friendly(self) -> None:
        app, output = self.app_with([
            "1", "bad", "", "8", "2026-02-01", "2026-01-01", "11", "one,two", "14", "b",
        ])

        filters = app.dataset_filters_screen(DatasetFilters(min_overall=3.0, verdict="old"))

        self.assertEqual(filters, DatasetFilters())
        rendered = "\n".join(output)
        self.assertIn("not a valid number", rendered)
        self.assertIn("Start date must be on or before end date", rendered)
        self.assertIn("positive whole-number run IDs", rendered)

    def test_date_bounds_can_be_cleared(self) -> None:
        app, _ = self.app_with(["8", "", "", "b"])

        filters = app.dataset_filters_screen(
            DatasetFilters(date_from=date(2026, 1, 1), date_to=date(2026, 1, 2))
        )

        self.assertIsNone(filters.date_from)
        self.assertIsNone(filters.date_to)

    def test_redaction_options_and_reset_are_vertical_and_session_local(self) -> None:
        app, output = self.app_with([
            "1", "1", "secret", "b", "2", "2", "3", "1", "4", "2", "5", "2",
            "6", "1", r"token-\d+", "b", "8", "b",
        ])

        redaction = app.dataset_redaction_screen(DatasetFilters(), RedactionConfig())

        self.assertEqual(redaction, RedactionConfig())
        rendered = "\n".join(output)
        self.assertIn("1) Literal Terms", rendered)
        self.assertIn("6) Custom Regex Patterns", rendered)
        self.assertNotIn("Literal terms, comma separated", rendered)

    def test_invalid_regex_is_not_stored(self) -> None:
        app, output = self.app_with(["6", "1", "[", "b", "b"])

        redaction = app.dataset_redaction_screen(DatasetFilters(), RedactionConfig())

        self.assertEqual(redaction.regex_patterns, ())
        self.assertIn("Invalid custom regex", "\n".join(output))

    def test_redaction_rules_can_be_removed(self) -> None:
        app, _ = self.app_with(["1", "1", "alpha", "1", "beta", "2", "1", "b", "b"])

        redaction = app.dataset_redaction_screen(DatasetFilters(), RedactionConfig())

        self.assertEqual(redaction.literals, ("beta",))

    def test_redaction_preview_calls_dataset_builder_and_shows_rule_counts(self) -> None:
        app, output = self.app_with(["b"])
        filters = DatasetFilters(verdict="approved")
        redaction = RedactionConfig(literals=("secret",))
        preview = DatasetPreview(
            records=[{"input": {"raw_model_output": "[REDACTED_LITERAL] output"}}],
            redactions=1,
            redaction_counts={"literal": 1},
            post_redaction_collisions=2,
        )
        app.datasets.preview = Mock(return_value=preview)

        app.dataset_redaction_preview(filters, redaction)

        app.datasets.preview.assert_called_once_with(filters, redaction)
        rendered = "\n".join(output)
        self.assertIn("Redactions by rule: literal: 1", rendered)
        self.assertIn("Post-redaction collisions: 2", rendered)
        self.assertIn("Sample 1: [REDACTED_LITERAL] output", rendered)

    def test_back_and_quit_all_navigation_propagate(self) -> None:
        app, _ = self.app_with(["b"])
        self.assertEqual(app.dataset_filters_screen(DatasetFilters()), DatasetFilters())

        app, _ = self.app_with(["QA"])
        with self.assertRaises(QuitApplication):
            app.dataset_redaction_screen(DatasetFilters(), RedactionConfig())

    def test_builder_preview_uses_session_local_filters_and_displays_summary(self) -> None:
        app, output = self.app_with(["3", "4", "approved", "b", "2", "b", "b"])
        preview = DatasetPreview(
            records=[{"input": {"model": {"model_name": "Alpha"}, "benchmark": {"name": "Review"}, "raw_model_output": "short output"}}],
            excluded={"missing_review": 2},
            warnings={"missing_hardware": 1},
            source_duplicates=3,
            fingerprint_duplicates=2,
            near_duplicates=1,
            post_redaction_collisions=1,
            redactions=4,
            redaction_counts={"literal": 4},
        )
        app.datasets.preview = Mock(return_value=preview)

        app.dataset_builder_screen()

        app.datasets.preview.assert_called_once()
        filters = app.datasets.preview.call_args.args[0]
        self.assertEqual(filters.verdict, "approved")
        rendered = "\n".join(output)
        self.assertIn("Total candidate runs: 0", rendered)
        self.assertIn("missing_review: 2", rendered)
        self.assertIn("missing_hardware: 1", rendered)
        self.assertIn("Redactions by rule: literal: 4", rendered)
        self.assertIn("Alpha — Review (12 output characters)", rendered)

    def test_build_cancellation_writes_nothing(self) -> None:
        app, _ = self.app_with([])
        app.datasets.preview = Mock(return_value=DatasetPreview())
        app.datasets.write_dataset = Mock()

        with patch.object(app, "prompt_path", return_value=BACK):
            app.dataset_build_workflow(DatasetFilters(), RedactionConfig())

        app.datasets.write_dataset.assert_not_called()

    def test_successful_build_uses_structured_write_result(self) -> None:
        app, output = self.app_with(["y", "b"])
        destination = Path(app.benchmarks.database.path.parent) / "dataset.jsonl"
        result = DatasetWriteResult(
            DatasetWriteStatus.SUCCESS,
            destination,
            destination.with_suffix(".jsonl.manifest.json"),
            record_count=2,
            sha256="a" * 64,
            jsonl_finalized=True,
            manifest_finalized=True,
        )
        app.datasets.preview = Mock(return_value=DatasetPreview(records=[{} for _ in range(2)]))
        app.datasets.write_dataset = Mock(return_value=result)

        with patch.object(app, "prompt_path", return_value=str(destination)), patch.object(app, "prepare_export_destination", return_value=destination):
            app.dataset_build_workflow(DatasetFilters(verdict="approved"), RedactionConfig(literals=("secret",)))

        app.datasets.write_dataset.assert_called_once_with(destination, filters=DatasetFilters(verdict="approved"), redaction_config=RedactionConfig(literals=("secret",)))
        rendered = "\n".join(output)
        self.assertIn("Dataset Build Complete", rendered)
        self.assertIn("Record count: 2", rendered)
        self.assertIn("SHA-256: " + "a" * 64, rendered)

    def test_overwrite_requires_second_explicit_confirmation(self) -> None:
        app, output = self.app_with(["y", "y", "b"])
        destination = Path(app.benchmarks.database.path.parent) / "dataset.jsonl"
        overwrite_required = DatasetWriteResult(DatasetWriteStatus.OVERWRITE_REQUIRED, destination, destination.with_suffix(".jsonl.manifest.json"))
        success = DatasetWriteResult(DatasetWriteStatus.SUCCESS, destination, destination.with_suffix(".jsonl.manifest.json"), jsonl_finalized=True, manifest_finalized=True)
        app.datasets.preview = Mock(return_value=DatasetPreview())
        app.datasets.write_dataset = Mock(side_effect=[overwrite_required, success])

        with patch.object(app, "prompt_path", return_value=str(destination)), patch.object(app, "prepare_export_destination", return_value=destination):
            app.dataset_build_workflow(DatasetFilters(), RedactionConfig())

        self.assertEqual(app.datasets.write_dataset.call_count, 2)
        self.assertTrue(app.datasets.write_dataset.call_args_list[1].kwargs["overwrite"])
        self.assertIn("Dataset Build Complete", "\n".join(output))

    def test_validation_failed_and_partial_finalization_are_not_reported_as_success(self) -> None:
        destination = Path(tempfile.gettempdir()) / "dataset.jsonl"
        outcomes = (
            (DatasetWriteStatus.VALIDATION_FAILED, "Dataset Build Validation Failed"),
            (DatasetWriteStatus.PARTIAL_FINALIZATION, "Dataset Build Partially Finalized"),
        )
        for status, expected_title in outcomes:
            with self.subTest(status=status):
                app, output = self.app_with(["y", "b"])
                app.datasets.preview = Mock(return_value=DatasetPreview())
                app.datasets.write_dataset = Mock(return_value=DatasetWriteResult(status, destination, destination.with_suffix(".jsonl.manifest.json"), jsonl_finalized=status is DatasetWriteStatus.PARTIAL_FINALIZATION))
                with patch.object(app, "prompt_path", return_value=str(destination)), patch.object(app, "prepare_export_destination", return_value=destination):
                    app.dataset_build_workflow(DatasetFilters(), RedactionConfig())
                rendered = "\n".join(output)
                self.assertIn(expected_title, rendered)
                self.assertNotIn("Dataset Build Complete", rendered)

    def write_validation_pair(self, app: TerminalApp, *, malformed: bool = False, bad_sha: bool = False, bad_count: bool = False) -> Path:
        dataset = Path(app.benchmarks.database.path.parent) / "existing.jsonl"
        if malformed:
            dataset.write_text("{bad}\n", encoding="utf-8")
            return dataset
        record = {"instruction": "x", "input": {}, "response": {}, "metadata": {}}
        dataset.write_text(json.dumps(record) + "\n\n", encoding="utf-8")
        digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
        manifest = dataset.with_suffix(".jsonl.manifest.json")
        manifest.write_text(json.dumps({
            "dataset_filename": dataset.name, "record_count": 2 if bad_count else 1, "excluded_count": 0,
            "duplicate_count": 0, "redaction_count": 0, "benchpup_version": "test", "schema_version": 5,
            "created_at": "2026-01-01T00:00:00+00:00", "format_version": 1,
            "sha256": "0" * 64 if bad_sha else digest,
        }), encoding="utf-8")
        return dataset

    def test_validate_existing_valid_jsonl_and_manifest_pair(self) -> None:
        app, output = self.app_with(["b"])
        dataset = self.write_validation_pair(app)

        with patch.object(app, "prompt_path", return_value=str(dataset)):
            app.dataset_validate_workflow()

        rendered = "\n".join(output)
        self.assertIn("Dataset: Valid", rendered)
        self.assertIn("Record count: 1", rendered)
        self.assertIn("Blank lines are ignored", rendered)
        self.assertIn("Dataset/manifest pair: Valid", rendered)

    def test_validate_existing_invalid_jsonl_reports_line_number(self) -> None:
        app, output = self.app_with(["n", "b"])
        dataset = self.write_validation_pair(app, malformed=True)

        with patch.object(app, "prompt_path", return_value=str(dataset)):
            app.dataset_validate_workflow()

        self.assertIn("Line 1", "\n".join(output))

    def test_validate_existing_reports_pair_hash_and_count_mismatches(self) -> None:
        for options, expected in (((True, False), "SHA-256"), ((False, True), "record count")):
            with self.subTest(options=options):
                app, output = self.app_with(["b"])
                dataset = self.write_validation_pair(app, bad_sha=options[0], bad_count=options[1])
                with patch.object(app, "prompt_path", return_value=str(dataset)):
                    app.dataset_validate_workflow()
                self.assertIn(expected, "\n".join(output))


if __name__ == "__main__":
    unittest.main()
