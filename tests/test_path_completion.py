import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine.path_completion import install_path_completion, normalize_path, path_candidates, resolve_export_destination
from cli import MAIN, QuitApplication, TerminalApp


class PathCompletionTests(unittest.TestCase):
    def test_normalize_path_strips_quotes_and_handles_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "my file.csv"
            self.assertEqual(normalize_path(f'"{expected}"'), str(expected.resolve()))

    def test_normalize_path_expands_home_and_relative_paths(self):
        self.assertEqual(normalize_path("~"), str(Path.home().resolve()))
        self.assertEqual(normalize_path("./exports/out.csv", base_dir="/tmp"), str(Path("/tmp/exports/out.csv").resolve()))

    def test_normalize_path_preserves_windows_style_path_off_windows(self):
        value = r"C:\Users\natan\Documents\file.csv"
        expected = value if os.name != "nt" else str(Path(value).resolve())
        self.assertEqual(normalize_path(value), expected)

    def test_candidates_filter_extensions_and_complete_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "import.csv").write_text("", encoding="utf-8")
            (root / "ignore.txt").write_text("", encoding="utf-8")
            (root / "exports").mkdir()
            candidates = path_candidates(str(root / "i"), extensions=(".csv",))
            self.assertEqual(candidates, [str(root / "import.csv")])
            self.assertIn(str(root / "exports") + os.sep, path_candidates(str(root / "e"), extensions=(".csv",)))

    def test_completion_setup_is_safe_when_no_terminal_backend_is_available(self):
        restore = install_path_completion()
        restore()

    def test_path_prompt_uses_prompt_toolkit_when_interactive(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            def output_fn(message):
                events.append(("output", message))
            def input_fn(_prompt):
                raise AssertionError("interactive path prompts must use prompt_toolkit")
            app = TerminalApp(Path(directory) / "benchmarks.db", input_fn=input_fn, output_fn=output_fn)
            app.interactive_input = True
            from unittest.mock import patch
            with patch("cli.toolkit_prompt", return_value="~/archive.json") as prompt, patch("cli.PathCompleter") as completer:
                app.prompt_path("Archive file", default=str(Path(directory) / "backup.json"))
            completer.assert_called_once_with(expanduser=True)
            self.assertEqual(prompt.call_args.kwargs["complete_while_typing"], False)
            self.assertEqual(prompt.call_args.args[0], f"Archive file [{Path(directory) / 'backup.json'}]: ")

    def test_path_prompt_uses_injected_input_without_prompt_toolkit(self):
        with tempfile.TemporaryDirectory() as directory:
            output, prompts = [], []
            def input_fn(prompt):
                prompts.append(prompt)
                return ""
            app = TerminalApp(Path(directory) / "benchmarks.db", input_fn=input_fn, output_fn=output.append)
            from unittest.mock import patch
            with patch("cli.toolkit_prompt") as prompt:
                app.prompt_path("Archive file", default=str(Path(directory) / "backup.json"))
            prompt.assert_not_called()
            self.assertEqual(prompts, [f"Archive file [{Path(directory) / 'backup.json'}]: "])
            self.assertIn("Tip: press Tab to autocomplete paths.", output)

    def test_keyboard_interrupt_in_path_prompt_cancels_and_allows_later_input(self):
        with tempfile.TemporaryDirectory() as directory:
            output = []
            app = TerminalApp(Path(directory) / "benchmarks.db", input_fn=lambda _: "q", output_fn=output.append)
            app.interactive_input = True
            from unittest.mock import patch
            with patch("cli.toolkit_prompt", side_effect=KeyboardInterrupt):
                self.assertIs(app.prompt_path("Archive file"), MAIN)
            self.assertEqual(app.ask("Choose an option"), "q")
            self.assertTrue(any("Operation cancelled." in line for line in output))

    def test_quit_all_path_prompt_flushes_before_propagating_global_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            app = TerminalApp(Path(directory) / "benchmarks.db", input_fn=lambda _: "", output_fn=lambda _: None)
            app.interactive_input = True
            from unittest.mock import patch
            with patch("cli.toolkit_prompt", return_value="Qa"), patch("cli.sys.stdout.flush") as flush:
                with self.assertRaises(QuitApplication):
                    app.prompt_path("Archive file")
            self.assertTrue(flush.called)

    def test_local_quit_still_returns_main_signal_from_interactive_path_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            app = TerminalApp(Path(directory) / "benchmarks.db", input_fn=lambda _: "", output_fn=lambda _: None)
            app.interactive_input = True
            from unittest.mock import patch
            with patch("cli.toolkit_prompt", return_value="q"):
                self.assertIs(app.prompt_path("Archive file"), MAIN)

    def test_export_destination_uses_default_filename_for_directories_and_trailing_slashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                resolve_export_destination(root, default_filename="scoreboard.csv"),
                root / "scoreboard.csv",
            )
            self.assertEqual(
                resolve_export_destination(str(root / "new-folder") + os.sep, default_filename="benchmark_runs.csv"),
                root / "new-folder" / "benchmark_runs.csv",
            )

    def test_export_destination_adds_missing_html_extension(self):
        self.assertEqual(
            resolve_export_destination("/tmp/scoreboard", default_filename="scoreboard.html", extension=".html"),
            Path("/tmp/scoreboard.html"),
        )
