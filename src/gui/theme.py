"""Centralized dark-mode tokens and palette for the BenchPup shell."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


@dataclass(frozen=True)
class ThemeTokens:
    background: str = "#10151b"
    surface: str = "#171f28"
    surface_elevated: str = "#202b37"
    surface_selected: str = "#263b52"
    border: str = "#344353"
    text: str = "#f1f5f8"
    muted_text: str = "#a9b7c4"
    accent: str = "#79b8ff"
    accent_hover: str = "#9dceff"
    focus: str = "#f4c978"
    disabled_text: str = "#667484"
    disabled_surface: str = "#1a2129"
    success: str = "#8fd6a8"
    warning: str = "#f4c978"


DEFAULT_THEME = ThemeTokens()


def build_stylesheet(tokens: ThemeTokens = DEFAULT_THEME) -> str:
    return f"""
    * {{
        font-family: "Segoe UI", "Noto Sans", sans-serif;
        font-size: 10pt;
        color: {tokens.text};
    }}
    QMainWindow, QWidget#mainShell, QWidget#dashboardPage, QWidget#runsPage,
    QWidget#placeholderPage, QDialog, QWizard {{
        background: {tokens.background};
    }}
    QFrame#appHeader, QWidget#navigationSidebar, QFrame#summaryCard,
    QFrame#placeholderCard {{
        background: {tokens.surface};
        border: 1px solid {tokens.border};
        border-radius: 8px;
    }}
    QFrame#runFilterPanel, QFrame#catalogToolbar, QGroupBox {{
        background: {tokens.surface};
        border: 1px solid {tokens.border};
        border-radius: 6px;
        margin-top: 8px;
        padding: 8px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 5px;
        color: {tokens.muted_text};
        font-weight: 700;
    }}
    QFrame#appHeader {{
        border: 1px solid {tokens.border};
    }}
    QLabel#brandTitle {{
        font-size: 22pt;
        font-weight: 700;
        color: {tokens.text};
    }}
    QLabel#brandVersion {{
        color: {tokens.accent};
        font-weight: 600;
    }}
    QLabel#brandSubtitle, QLabel#pageDescription, QLabel#refreshStatus,
    QLabel#sidebarHeading, QLabel#summaryCardTitle {{
        color: {tokens.muted_text};
    }}
    QLabel#sidebarHeading {{
        font-size: 8pt;
        font-weight: 700;
        letter-spacing: 1px;
    }}
    QLabel#pageTitle {{
        font-size: 19pt;
        font-weight: 700;
    }}
    QLabel#sectionTitle {{
        font-size: 13pt;
        font-weight: 700;
    }}
    QLabel#summaryCardTitle {{
        font-size: 9pt;
        font-weight: 600;
    }}
    QLabel#summaryCardValue {{
        font-size: 18pt;
        font-weight: 700;
        color: {tokens.accent};
    }}
    QLabel#emptyState, QLabel#placeholderState, QLabel#validationError,
    QLabel#errorState, QLabel#fieldHint, QLabel#catalogStatus, QLabel#copyStatus,
    QLabel#validationSummary, QLabel#resultCount {{
        color: {tokens.muted_text};
        padding: 8px;
    }}
    QLabel#validationError, QLabel#errorState {{
        color: {tokens.warning};
        font-weight: 600;
    }}
    QLabel#placeholderState {{
        font-size: 12pt;
        font-weight: 600;
    }}
    QPushButton, QToolButton {{
        background: {tokens.surface_elevated};
        border: 1px solid {tokens.border};
        border-radius: 6px;
        padding: 7px 12px;
        min-height: 20px;
    }}
    QPushButton:hover, QToolButton:hover {{
        background: {tokens.surface_selected};
        border-color: {tokens.accent};
    }}
    QPushButton:pressed, QToolButton:pressed {{
        background: {tokens.surface_selected};
    }}
    QPushButton:focus, QToolButton:focus, QListWidget:focus,
    QTableView:focus {{
        border: 2px solid {tokens.focus};
    }}
    QPushButton:disabled, QToolButton:disabled {{
        color: {tokens.disabled_text};
        background: {tokens.disabled_surface};
        border-color: {tokens.border};
    }}
    QPushButton#secondaryButton {{
        color: {tokens.accent};
    }}
    QLineEdit, QComboBox, QTextEdit, QPlainTextEdit, QDoubleSpinBox, QSpinBox {{
        background: {tokens.surface};
        color: {tokens.text};
        border: 1px solid {tokens.border};
        border-radius: 5px;
        padding: 6px 8px;
        selection-background-color: {tokens.surface_selected};
    }}
    QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus,
    QDoubleSpinBox:focus, QSpinBox:focus, QCheckBox:focus, QTabWidget:focus {{
        border: 2px solid {tokens.focus};
    }}
    QLineEdit:disabled, QComboBox:disabled, QTextEdit:disabled, QPlainTextEdit:disabled,
    QDoubleSpinBox:disabled, QSpinBox:disabled, QCheckBox:disabled {{
        color: {tokens.disabled_text};
        background: {tokens.disabled_surface};
    }}
    QComboBox QAbstractItemView {{
        background: {tokens.surface_elevated};
        color: {tokens.text};
        selection-background-color: {tokens.surface_selected};
        selection-color: {tokens.text};
    }}
    QCheckBox {{
        spacing: 7px;
    }}
    QCheckBox::indicator {{
        width: 15px;
        height: 15px;
    }}
    QTabWidget::pane {{
        border: 1px solid {tokens.border};
        background: {tokens.surface};
    }}
    QTabBar::tab {{
        background: {tokens.surface_elevated};
        color: {tokens.muted_text};
        padding: 8px 12px;
        border: 1px solid {tokens.border};
    }}
    QTabBar::tab:selected {{
        color: {tokens.text};
        background: {tokens.surface_selected};
        border-bottom-color: {tokens.accent};
    }}
    QListWidget {{
        background: transparent;
        border: none;
        outline: none;
        padding: 2px;
    }}
    QListWidget::item {{
        border-radius: 6px;
        padding: 9px 10px;
        margin: 2px 0;
    }}
    QListWidget::item:hover {{
        background: {tokens.surface_elevated};
    }}
    QListWidget::item:selected {{
        background: {tokens.surface_selected};
        color: {tokens.text};
        border-left: 3px solid {tokens.accent};
        padding-left: 7px;
        font-weight: 700;
    }}
    QTableView {{
        background: {tokens.surface};
        alternate-background-color: {tokens.surface_elevated};
        border: 1px solid {tokens.border};
        border-radius: 6px;
        gridline-color: {tokens.border};
        selection-background-color: {tokens.surface_selected};
        selection-color: {tokens.text};
        outline: none;
    }}
    QTableView::item {{
        padding: 5px;
    }}
    QHeaderView::section {{
        background: {tokens.surface_elevated};
        color: {tokens.muted_text};
        border: none;
        border-bottom: 1px solid {tokens.border};
        padding: 7px;
        font-weight: 700;
    }}
    QScrollBar:vertical, QScrollBar:horizontal {{
        background: {tokens.background};
        margin: 0;
    }}
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
        background: {tokens.border};
        border-radius: 4px;
        min-height: 24px;
        min-width: 24px;
    }}
    QScrollBar::handle:hover {{
        background: {tokens.accent};
    }}
    QToolTip {{
        background: {tokens.surface_elevated};
        color: {tokens.text};
        border: 1px solid {tokens.border};
        padding: 5px;
    }}
    QStatusBar {{
        background: {tokens.surface};
        color: {tokens.muted_text};
        border-top: 1px solid {tokens.border};
    }}
    """


def apply_dark_theme(application: QApplication, tokens: ThemeTokens = DEFAULT_THEME) -> None:
    """Apply the BenchPup palette and stylesheet to one QApplication."""

    application.setStyle("Fusion")
    palette = QPalette()
    role_values = {
        QPalette.ColorRole.Window: tokens.background,
        QPalette.ColorRole.WindowText: tokens.text,
        QPalette.ColorRole.Base: tokens.surface,
        QPalette.ColorRole.AlternateBase: tokens.surface_elevated,
        QPalette.ColorRole.ToolTipBase: tokens.surface_elevated,
        QPalette.ColorRole.ToolTipText: tokens.text,
        QPalette.ColorRole.Text: tokens.text,
        QPalette.ColorRole.Button: tokens.surface_elevated,
        QPalette.ColorRole.ButtonText: tokens.text,
        QPalette.ColorRole.Highlight: tokens.surface_selected,
        QPalette.ColorRole.HighlightedText: tokens.text,
    }
    for role, value in role_values.items():
        palette.setColor(role, QColor(value))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(tokens.disabled_text))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(tokens.disabled_text))
    application.setPalette(palette)
    application.setStyleSheet(build_stylesheet(tokens))


load_theme = apply_dark_theme

__all__ = ("DEFAULT_THEME", "ThemeTokens", "apply_dark_theme", "build_stylesheet", "load_theme")
