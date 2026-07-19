"""Keyboard-reachable sidebar navigation for the desktop shell."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget


@dataclass(frozen=True)
class NavigationDestination:
    key: str
    label: str
    description: str


NAVIGATION_DESTINATIONS: tuple[NavigationDestination, ...] = (
    NavigationDestination("dashboard", "Dashboard", "Read-only BenchPup overview"),
    NavigationDestination("runs", "Runs", "Browse and manage benchmark runs"),
    NavigationDestination("sessions", "Sessions", "Organize related benchmark work"),
    NavigationDestination("models", "Models", "Manage model profiles"),
    NavigationDestination("benchmarks", "Benchmarks", "Manage benchmark definitions"),
    NavigationDestination("scoreboards", "Scoreboards", "Review historical scoreboard data"),
    NavigationDestination("reports", "Reports", "Build structured reports"),
    NavigationDestination("dataset_builder", "Dataset Builder", "Curate training datasets"),
    NavigationDestination("comparisons", "Comparisons", "Compare models and sessions"),
    NavigationDestination("trends", "Trends", "Explore historical trends"),
    NavigationDestination("settings", "Settings", "Configure BenchPup preferences"),
)


class NavigationSidebar(QWidget):
    """A text-labelled, focusable list that emits stable page keys."""

    page_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("navigationSidebar")
        self.setAccessibleName("BenchPup navigation")
        self.setMinimumWidth(196)
        self.setMaximumWidth(264)

        title = QLabel("NAVIGATION")
        title.setObjectName("sidebarHeading")

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("navigationList")
        self.list_widget.setAccessibleName("BenchPup pages")
        self.list_widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.list_widget.setTabKeyNavigation(True)
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list_widget.currentRowChanged.connect(self._handle_row_changed)

        self._items: dict[str, QListWidgetItem] = {}
        for destination in NAVIGATION_DESTINATIONS:
            item = QListWidgetItem(destination.label)
            item.setData(Qt.ItemDataRole.UserRole, destination.key)
            item.setToolTip(destination.description)
            self.list_widget.addItem(item)
            self._items[destination.key] = item

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 14, 10, 14)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(self.list_widget, 1)

    def _handle_row_changed(self, row: int) -> None:
        if row < 0:
            return
        item = self.list_widget.item(row)
        if item is not None:
            self.page_changed.emit(str(item.data(Qt.ItemDataRole.UserRole)))

    def set_current_page(self, key: str) -> None:
        item = self._items.get(key)
        if item is None:
            raise KeyError(f"Unknown navigation page: {key}")
        self.list_widget.setCurrentItem(item)

    def current_page(self) -> str | None:
        item = self.list_widget.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None
