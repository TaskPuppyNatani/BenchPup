import hashlib
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cli import QuitApplication, TerminalApp
from engine.datasets import DatasetFilters, DatasetPreview, RedactionConfig
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


if __name__ == "__main__":
    unittest.main()
