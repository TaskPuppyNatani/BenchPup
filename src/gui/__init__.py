"""PySide6 desktop interface for BenchPup.

The GUI is a presentation layer over the UI-independent ``engine`` package.
It never imports the CLI entry point.
"""

from .application import APP_VERSION, main
from .context import GuiApplicationContext, GuiPaths
from .main_window import MainWindow

__all__ = (
    "APP_VERSION",
    "GuiApplicationContext",
    "GuiPaths",
    "MainWindow",
    "main",
)
