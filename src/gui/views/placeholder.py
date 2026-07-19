"""Honest navigation placeholders for future Phase 5 slices."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


class PlaceholderPage(QWidget):
    """A polished, non-interactive page that makes future scope explicit."""

    def __init__(self, title: str, description: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("placeholderPage")
        self.setAccessibleName(f"{title} page")

        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        heading.setTextFormat(Qt.TextFormat.PlainText)

        summary = QLabel(description)
        summary.setObjectName("pageDescription")
        summary.setWordWrap(True)

        card = QFrame()
        card.setObjectName("placeholderCard")
        card.setAccessibleName(f"{title} coming soon state")
        state = QLabel("Coming in a later Phase 5 slice")
        state.setObjectName("placeholderState")
        state.setWordWrap(True)
        state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 34, 28, 34)
        card_layout.addWidget(state)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 30)
        layout.setSpacing(10)
        layout.addWidget(heading)
        layout.addWidget(summary)
        layout.addSpacing(16)
        layout.addWidget(card)
        layout.addStretch(1)
