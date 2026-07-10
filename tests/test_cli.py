import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cli import BACK, CANCEL, TerminalApp
from engine.domain import BenchmarkDefinition, ModelProfile, PromptTemplate, ScoreboardEntry, ScoreboardImportBatch


def score_answers():
    return ["4", "", "", "", "", "", "", "4", "", "", "Good", ""]


class CliPolishTests(unittest.TestCase):
    def app_with(self, answers):
        directory = tempfile.TemporaryDirectory()
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        self.addCleanup(directory.cleanup)
        return app, output

    def seed_catalog(self, app):
        app.catalog.model_profiles.create(ModelProfile(name="Local Qwen", model_name="Qwen 3"))
        app.catalog.benchmark_definitions.create(BenchmarkDefinition(name="Speech review", file_path="speech_server.py", benchmark_type="code_review"))
        text = "Review the code"
        app.catalog.prompt_templates.create(PromptTemplate(name="Review prompt", version="1.0", prompt_text=text, prompt_hash=hashlib.sha256(text.encode()).hexdigest(), benchmark_type="code_review"))

    def test_wizard_reviews_and_edits_before_save(self):
        answers = iter(["", "1", "1", "1", "", "draft output", *score_answers(), "n", "e", "6", "final output", *score_answers(), "n", "s"])
        app, _ = self.app_with(answers); self.seed_catalog(app)
        app.add_run_wizard()
        runs = app.benchmarks.runs.list()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].raw_model_output, "final output")

    def test_invalid_integer_and_float_are_reprompted(self):
        app, output = self.app_with(iter(["abc", "3", "bad", "2.5"]))
        self.assertEqual(app.ask_id(), 3)
        self.assertEqual(app.ask_float("Temperature"), 2.5)
        self.assertTrue(any("not a valid Run ID" in line for line in output))
        self.assertTrue(any("not a valid number" in line for line in output))

    def test_back_and_cancel_commands_are_case_insensitive(self):
        app, _ = self.app_with(iter(["BACK", "Cancel"]))
        self.assertIs(app.ask("Field", navigation=True), BACK)
        self.assertIs(app.ask("Field", navigation=True), CANCEL)

    def test_uppercase_menu_command_and_quit_are_accepted(self):
        app, output = self.app_with(iter(["LIST", "", "Quit"]))
        app.run()
        self.assertIn("No benchmark runs found.", output)

    def test_main_menu_uses_grouped_vertical_layout(self):
        app, output = self.app_with(iter(["q"]))
        app.run()
        menu = "\n".join(output)
        self.assertIn(" Runs\n ----\n 1) Add Run", menu)
        self.assertIn(" Reference Data\n --------------\n 6) Sessions", menu)
        self.assertIn(" Data\n ----\n11) Import", menu)
        self.assertIn("13) Scoreboard", menu)
        self.assertIn("Scoreboard entries : 0", menu)
        self.assertIn(" Help\n ----\nH) Help", menu)
        self.assertIn("Version 0.2 Alpha", menu)
        self.assertIn("Database : benchmarks.db", menu)

    def test_title_lines_are_centered_to_the_menu_width(self):
        app, output = self.app_with(iter(["q"]))
        app.run()
        title = next(line for line in output if "Local LLM Benchmark Recorder" in line)
        version = next(line for line in output if "Version 0.2 Alpha" in line)
        self.assertEqual(len(title), len(version))
        title_center = title.index("Local") + len("Local LLM Benchmark Recorder") / 2
        version_center = version.index("Version") + len("Version 0.2 Alpha") / 2
        self.assertLessEqual(abs(title_center - version_center), 0.5)

    def test_keyboard_interrupt_returns_to_main_menu(self):
        calls = iter([KeyboardInterrupt(), "quit"])
        def interrupted_input(_):
            value = next(calls)
            if isinstance(value, BaseException): raise value
            return value
        directory = tempfile.TemporaryDirectory(); output = []
        self.addCleanup(directory.cleanup)
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=interrupted_input, output_fn=output.append)
        app.run()
        self.assertTrue(any("Returning to the main menu" in line for line in output))

    def test_import_menu_flow_imports_csv(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "runs.csv"
        source.write_text("Model Name,Benchmark,Prompt Text,Raw Model Output,Overall Score\nQwen,main.py,Review it,Found a bug,4\n", encoding="utf-8")
        answers = iter([str(source), "y", "y", "s"])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.import_csv("runs")
        self.assertEqual(len(app.benchmarks.runs.list()), 1)
        self.assertTrue(any("Imported 1" in line for line in output))

    def test_import_mapping_edit_uses_numbered_choices(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "runs.csv"
        source.write_text("Model Name,Experts,Benchmark\nQwen,8,main.py\n", encoding="utf-8")
        answers = iter([str(source), "e", "2", "10", "d", "n", "y", "y", "s"])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.import_csv("runs")
        self.assertEqual(app.benchmarks.runs.list()[0].model_snapshot["moe_experts"], "8")
        self.assertTrue(any("10) moe_experts" in line for line in output))

    def test_yes_is_not_accepted_as_a_mapping_field(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "runs.csv"
        source.write_text("Model Name,Experts,Benchmark\nQwen,8,main.py\n", encoding="utf-8")
        answers = iter([str(source), "e", "2", "yes", "10", "d", "n", "y", "y", "s"])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.import_csv("runs")
        self.assertTrue(any("10) moe_experts" in line for line in output))
        self.assertEqual(len(app.benchmarks.runs.list()), 1)

    def test_import_cancel_does_not_write_rows(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "runs.csv"
        source.write_text("Model Name\nQwen\n", encoding="utf-8")
        answers = iter([str(source), "c"])
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=lambda _: None)
        app.import_csv()
        self.assertEqual(app.benchmarks.runs.list(), [])

    def test_import_default_benchmark_is_applied_to_blank_rows(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "runs.csv"
        source.write_text("Model Name,Raw Model Output\nQwen,Found a bug\n", encoding="utf-8")
        answers = iter([str(source), "y", "y", ""])
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=lambda _: None)
        app.import_csv()
        self.assertEqual(app.catalog.scoreboard_entries.list()[0].model_name, "Qwen")

    def test_import_without_benchmark_column_can_be_summary_rows(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "summary.csv"
        source.write_text("Model Name,Score\nQwen,4\n", encoding="utf-8")
        answers = iter([str(source), "y", "y", ""])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.import_csv()
        self.assertEqual(len(app.catalog.scoreboard_entries.list()), 1)
        self.assertTrue(any("Imported 1 scoreboard entry" in line for line in output))

    def test_scoreboard_preview_reports_skipped_non_data_rows(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "summary.csv"
        source.write_text("Model Name,Score\nQwen,4\nLEGEND,Out of five\n", encoding="utf-8")
        answers = iter([str(source), "y", "y", ""])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.import_csv()
        self.assertTrue(any("Preview: 1 importable row(s), 1 skipped non-data row(s)" in line for line in output))

    def test_scoreboard_browser_lists_views_and_filters_historical_entries(self):
        app, output = self.app_with(iter(["1", "2", "1", "3", "4", "1", "b"]))
        batch = app.catalog.scoreboard_import_batches.create(
            ScoreboardImportBatch(name="Historical July", source_file="july.csv", imported_at="2026-07-10T12:00:00Z")
        )
        app.catalog.scoreboard_entries.create(
            ScoreboardEntry(model_name="Qwen", score=4.5, verdict="Useful", import_batch_id=batch.id, imported_at="2026-07-10T12:00:00Z")
        )
        app.scoreboard_screen()
        rendered = "\n".join(output)
        self.assertIn("Scoreboard (historical summary imports)", rendered)
        self.assertIn("#1 | Qwen | score=4.5 | batch=Historical July | imported=2026-07-10T12:00:00Z", rendered)
        self.assertIn("Scoreboard entry #1", rendered)
        self.assertIn("Batch: Historical July", rendered)
        self.assertIn("#1 | Historical July | entries=1 | imported=2026-07-10T12:00:00Z | source=july.csv", rendered)

    def test_scoreboard_html_export_prompts_to_open_report(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        destination = Path(directory.name) / "scoreboard.html"
        answers = iter(["5", str(destination), "n"])
        output = []
        app = TerminalApp(Path(directory.name) / "benchmarks.db", input_fn=lambda _: next(answers), output_fn=output.append)
        app.catalog.scoreboard_entries.create(ScoreboardEntry(model_name="Qwen", notes="<script>alert(1)</script>"))
        app.export_screen()
        self.assertTrue(destination.exists())
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", destination.read_text(encoding="utf-8"))
        self.assertTrue(any("Exported Scoreboard HTML" in line for line in output))
