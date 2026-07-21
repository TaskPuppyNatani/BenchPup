from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from gui.theme import DEFAULT_THEME, build_stylesheet


class GuiThemeTests(unittest.TestCase):
    def test_shared_stylesheet_defines_visible_checkbox_indicator_state_matrix(self) -> None:
        stylesheet = build_stylesheet(DEFAULT_THEME)
        selectors = (
            "QCheckBox::indicator:unchecked",
            "QCheckBox::indicator:unchecked:hover",
            "QCheckBox::indicator:unchecked:focus",
            "QCheckBox::indicator:unchecked:pressed",
            "QCheckBox::indicator:checked",
            "QCheckBox::indicator:checked:hover",
            "QCheckBox::indicator:checked:focus",
            "QCheckBox::indicator:checked:pressed",
            "QCheckBox::indicator:unchecked:disabled",
            "QCheckBox::indicator:checked:disabled",
        )
        for selector in selectors:
            with self.subTest(selector=selector):
                start = stylesheet.index(f"{selector} {{")
                body = stylesheet[start : stylesheet.index("}", start)]
                self.assertIn("border:", body)
                if ":focus" not in selector:
                    self.assertIn("background-color:", body)

        self.assertRegex(
            stylesheet,
            r"QCheckBox::indicator\s*\{\s*width:\s*17px;\s*height:\s*17px;",
        )
        self.assertIn(DEFAULT_THEME.surface_elevated, stylesheet)
        self.assertIn(DEFAULT_THEME.disabled_surface, stylesheet)
        self.assertIn(DEFAULT_THEME.accent, stylesheet)
        self.assertIn(DEFAULT_THEME.disabled_text, stylesheet)

    def test_checkbox_indicator_contrast_is_owned_by_shared_theme_only(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "gui"
        for path in source_root.rglob("*.py"):
            if path.name == "theme.py":
                continue
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("QCheckBox::indicator", source, str(path))
            self.assertNotIn("setStyleSheet", source, str(path))


if __name__ == "__main__":
    unittest.main()
