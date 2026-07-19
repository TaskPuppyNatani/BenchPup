"""PySide6 application entry point and startup error boundary."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence
from typing import cast

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from .context import DEFAULT_APP_VERSION, GuiApplicationContext, create_gui_logger, resolve_paths
from .main_window import MainWindow
from .theme import apply_dark_theme

APP_NAME = "BenchPup"
APP_VERSION = DEFAULT_APP_VERSION
APP_SUBTITLE = "Local benchmark intelligence for your models"


def create_application(argv: Sequence[str] | None = None) -> QApplication:
    """Return the one QApplication allowed for this process."""

    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    arguments = list(sys.argv if argv is None else argv)
    return QApplication(arguments)


def configure_application(application: QApplication) -> None:
    application.setApplicationName(APP_NAME)
    application.setApplicationDisplayName(APP_NAME)
    application.setOrganizationName(APP_NAME)
    application.setApplicationVersion(APP_VERSION)
    apply_dark_theme(application)


def _friendly_startup_detail(error: BaseException) -> str:
    detail = str(error).strip().splitlines()[0] if str(error).strip() else "Unknown startup error."
    return detail[:240]


def handle_startup_failure(
    error: BaseException,
    *,
    logger: logging.Logger,
    parent: QWidget | None = None,
) -> None:
    """Log a startup failure and show a concise, testable QMessageBox."""

    logger.error("BenchPup GUI startup failed", exc_info=(type(error), error, error.__traceback__))
    QMessageBox.critical(
        parent,
        "BenchPup could not start",
        "BenchPup could not start.\n\n"
        f"{_friendly_startup_detail(error)}\n\n"
        "See logs/error.log for details.",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    context_factory: Callable[[], GuiApplicationContext] | None = None,
) -> int:
    """Create the shell, run Qt, and close the shared context on exit."""

    application = create_application(argv)
    configure_application(application)
    logger = create_gui_logger(resolve_paths().log_path)
    context: GuiApplicationContext | None = None
    try:
        context = (context_factory or GuiApplicationContext.create)()
        window = MainWindow(context)
        window.show()
    except Exception as error:
        if context is not None:
            context.close()
        handle_startup_failure(error, logger=logger)
        return 1

    try:
        return int(application.exec())
    finally:
        context.close()
