import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cli import QuitApplication, TerminalApp


class DefaultWorkingDirectoryTests(unittest.TestCase):
    def app_with(self, directory: str, answers: list[str]):
        output: list[str] = []
        values = iter(answers)
        app = TerminalApp(Path(directory) / "benchmarks.db", input_fn=lambda _: next(values), output_fn=output.append)
        return app, output

    def test_existing_directory_is_saved_and_persists_across_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "files"; selected.mkdir()
            app, _ = self.app_with(directory, [str(selected)])
            app.set_default_working_directory()
            self.assertEqual(app.settings.get_default_working_directory(), selected)
            reopened, _ = self.app_with(directory, [])
            self.assertEqual(reopened.settings.get_default_working_directory(), selected)

    def test_legacy_setting_key_loads_and_next_save_uses_only_new_key(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "files"; selected.mkdir()
            app, _ = self.app_with(directory, [])
            app.settings.legacy_path.write_text(json.dumps({"default_file_directory": str(selected)}), encoding="utf-8")
            self.assertEqual(app.settings.get_default_working_directory(), selected)
            app.settings.set_default_working_directory(selected)
            saved = json.loads(app.settings.path.read_text(encoding="utf-8"))
            self.assertEqual(saved, {"default_working_directory": str(selected)})
            self.assertEqual(app.settings.path, Path(directory) / "config" / "settings.json")

    def test_missing_directory_is_created_only_after_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "new directory"
            app, _ = self.app_with(directory, [str(selected), "y"])
            app.set_default_working_directory()
            self.assertTrue(selected.is_dir())
            self.assertEqual(app.settings.get_default_working_directory(), selected)

    def test_declining_creation_and_blank_input_make_no_filesystem_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "not created"
            app, _ = self.app_with(directory, [str(selected), "n"])
            app.set_default_working_directory()
            self.assertFalse(selected.exists())
            self.assertIsNone(app.settings.get_default_working_directory())
            app, _ = self.app_with(directory, [""])
            app.set_default_working_directory()
            self.assertIsNone(app.settings.get_default_working_directory())

    def test_yes_no_words_and_files_are_not_accepted_as_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            app, output = self.app_with(directory, ["Yes"])
            app.set_default_working_directory()
            self.assertIsNone(app.settings.get_default_working_directory())
            self.assertTrue(any("not a yes/no response" in line for line in output))
            file_path = Path(directory) / "file.txt"; file_path.write_text("x", encoding="utf-8")
            app, output = self.app_with(directory, [str(file_path)])
            app.set_default_working_directory()
            self.assertIsNone(app.settings.get_default_working_directory())
            self.assertTrue(any("Expected a directory" in line for line in output))

    def test_clearing_setting_restores_unconfigured_state(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "files"; selected.mkdir()
            app, _ = self.app_with(directory, [str(selected)])
            app.set_default_working_directory()
            app.settings.clear_default_working_directory()
            self.assertIsNone(app.settings.get_default_working_directory())

    def test_configured_directory_starts_prompt_toolkit_and_other_paths_remain_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "files"; selected.mkdir()
            other = Path(directory) / "elsewhere" / "report.html"
            app, _ = self.app_with(directory, [])
            app.settings.set_default_working_directory(selected)
            app.interactive_input = True
            with patch("cli.toolkit_prompt", return_value=str(other)) as prompt, patch("cli.PathCompleter") as completer, patch("cli.sys.stdout.flush") as flush:
                self.assertEqual(app.prompt_path("Destination"), str(other.resolve()))
            self.assertEqual(completer.call_args.kwargs["get_paths"](), [str(selected)])
            self.assertTrue(flush.called)
            self.assertEqual(prompt.call_args.kwargs["complete_while_typing"], False)
            self.assertEqual(app.settings.get_default_working_directory(), selected)

    def test_relative_paths_use_configured_directory_without_restricting_absolute_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "files"; selected.mkdir()
            other = Path(directory) / "other" / "source.csv"
            app, _ = self.app_with(directory, ["relative.csv", str(other)])
            app.settings.set_default_working_directory(selected)
            self.assertEqual(app.prompt_path("First"), str(selected / "relative.csv"))
            self.assertEqual(app.prompt_path("Second"), str(other.resolve()))
            self.assertEqual(app.settings.get_default_working_directory(), selected)

    def test_quit_all_propagates_from_default_directory_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            app, _ = self.app_with(directory, ["QA"])
            with self.assertRaises(QuitApplication):
                app.set_default_working_directory()
