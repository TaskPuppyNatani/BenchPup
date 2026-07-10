import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cli import BACK, CANCEL, TerminalApp
from engine.domain import BenchmarkDefinition, ModelProfile, PromptTemplate


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
