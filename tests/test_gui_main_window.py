from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from gui.context import GuiApplicationContext
from gui.main_window import MainWindow
from gui.navigation import NAVIGATION_DESTINATIONS


class GuiMainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-main-window-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.context = GuiApplicationContext.create(
            database_path=Path(self.directory.name) / "data" / "benchmark.db"
        )
        self.window = MainWindow(self.context)

    def tearDown(self) -> None:
        self.window.close()
        self.context.close()
        self.directory.cleanup()

    def test_dashboard_is_initial_page_and_shell_has_expected_metadata(self) -> None:
        self.assertEqual(self.window.current_page_key(), "dashboard")
        self.assertIs(self.window.page_stack.currentWidget(), self.window.pages["dashboard"])
        self.assertIn("BenchPup", self.window.windowTitle())
        self.assertIn(self.context.version, self.window.windowTitle())
        brand = self.window.findChild(QLabel, "brandTitle")
        version = self.window.findChild(QLabel, "brandVersion")
        self.assertIsNotNone(brand)
        self.assertIsNotNone(version)
        self.assertEqual(brand.text(), "BenchPup")  # type: ignore[union-attr]
        self.assertIn("Version", version.text())  # type: ignore[union-attr]
        self.assertIsNotNone(self.window.statusBar())
        self.assertIn(str(self.context.paths.database_path), self.window.database_status_label.text())

    def test_all_navigation_destinations_exist_and_pages_are_reused(self) -> None:
        expected = [destination.key for destination in NAVIGATION_DESTINATIONS]
        self.assertEqual(list(self.window.pages), expected)
        self.assertEqual(self.window.page_stack.count(), len(expected))
        original_pages = dict(self.window.pages)
        for key in expected:
            self.window.navigate_to(key)
            self.assertEqual(self.window.current_page_key(), key)
            self.assertIs(self.window.page_stack.currentWidget(), original_pages[key])
            self.assertEqual(self.window.pages[key], original_pages[key])
        self.window.navigate_to("dashboard")
        self.assertEqual(self.window.navigation.list_widget.currentItem().text(), "Dashboard")

    def test_navigation_and_controls_have_keyboard_accessibility_foundation(self) -> None:
        self.assertNotEqual(self.window.navigation.list_widget.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertNotEqual(self.window.dashboard.recent_table.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertTrue(self.window.add_run_button.isEnabled())
        self.assertTrue(self.window.export_button.isEnabled())
        self.assertNotIn("planned", self.window.add_run_button.accessibleName().lower())
        self.assertIn("standard export", self.window.export_button.accessibleName().lower())
        self.assertEqual(
            self.window.dashboard.recent_table.model().headerData(0, Qt.Orientation.Horizontal),
            "Recorded",
        )

    def test_close_closes_shared_context_cleanly(self) -> None:
        self.window.close()
        self.assertTrue(self.context._closed)


if __name__ == "__main__":
    unittest.main()
