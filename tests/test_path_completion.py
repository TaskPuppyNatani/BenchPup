import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from engine.path_completion import install_path_completion, normalize_path, path_candidates, resolve_export_destination


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
