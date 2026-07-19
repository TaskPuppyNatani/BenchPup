from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtWidgets import QApplication

from gui.application import APP_NAME, APP_VERSION, configure_application, handle_startup_failure
from gui.context import GuiApplicationContext
from gui.theme import DEFAULT_THEME


class GuiApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-gui-tests"])

    def test_gui_package_and_module_entry_import(self) -> None:
        import gui
        import gui.__main__ as gui_main

        self.assertTrue(callable(gui.main))
        self.assertTrue(callable(gui_main.main))
        self.assertEqual(APP_NAME, "BenchPup")
        self.assertEqual(APP_VERSION, "0.4.1-Alpha")

    def test_centralized_dark_theme_loads(self) -> None:
        configure_application(self.application)
        self.assertIn(DEFAULT_THEME.background, self.application.styleSheet())
        self.assertEqual(self.application.applicationName(), "BenchPup")
        self.assertEqual(self.application.applicationVersion(), APP_VERSION)

    def test_context_initializes_each_service_once_and_uses_established_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "data" / "benchmark.db"
            context = GuiApplicationContext.create(database_path=database_path)
            try:
                self.assertEqual(context.paths.database_path, database_path.resolve())
                self.assertEqual(context.settings.path, Path(directory).resolve() / "config" / "settings.json")
                self.assertIs(context.benchmarks.catalog, context.catalog)
                self.assertIs(context.statistics.service, context.benchmarks)
                self.assertIs(context.statistics.catalog, context.catalog)
                self.assertIs(context.comparisons.service, context.benchmarks)
                self.assertIs(context.trends.service, context.benchmarks)
                with context.database.connection() as connection:
                    version = connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0]
                self.assertEqual(version, 5)
            finally:
                context.close()

    def test_context_close_is_idempotent_and_closes_database_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = GuiApplicationContext.create(database_path=Path(directory) / "data" / "benchmark.db")
            context.database.close = Mock(wraps=context.database.close)  # type: ignore[method-assign]
            context.close()
            context.close()
            context.database.close.assert_called_once_with()

    def test_cli_and_engine_import_without_gui_startup_or_pyside_import(self) -> None:
        source_path = str(Path(__file__).parents[1] / "src")
        script = (
            f"import sys; sys.path.insert(0, {source_path!r}); "
            "import engine; import cli; "
            "print('PySide6' in sys.modules)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "False")

    def test_gui_boundary_does_not_construct_repositories_or_execute_sql(self) -> None:
        source_root = Path(__file__).parents[1] / "src"
        gui_text = "\n".join(path.read_text(encoding="utf-8") for path in (source_root / "gui").rglob("*.py"))
        engine_text = "\n".join(path.read_text(encoding="utf-8") for path in (source_root / "engine").rglob("*.py"))
        self.assertNotIn("from cli", gui_text)
        self.assertNotIn("import cli", gui_text)
        self.assertIsNone(re.search(r"(?im)^\s*(SELECT|INSERT|UPDATE|DELETE)\s", gui_text))
        self.assertNotIn("Repository(", gui_text)
        self.assertNotIn("PySide6", engine_text)

    def test_startup_error_uses_friendly_gui_error_boundary(self) -> None:
        logger = Mock()
        with patch("gui.application.QMessageBox.critical") as critical:
            handle_startup_failure(RuntimeError("database is unavailable"), logger=logger)
        logger.error.assert_called_once()
        critical.assert_called_once()
        message = critical.call_args.args[2]
        self.assertIn("BenchPup could not start", message)
        self.assertIn("database is unavailable", message)


if __name__ == "__main__":
    unittest.main()
