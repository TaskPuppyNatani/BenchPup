"""Main QMainWindow shell and page ownership for BenchPup."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .context import GuiApplicationContext
from .navigation import NAVIGATION_DESTINATIONS, NavigationSidebar
from .views.add_run import AddRunWizard
from .views.benchmarks import BenchmarksView
from .views.catalog_page import CatalogPage
from .views.dashboard import DashboardView
from .views.hardware_profiles import HardwareProfilesView
from .views.models import ModelsView
from .views.placeholder import PlaceholderPage
from .views.prompt_templates import PromptTemplatesView
from .views.runs import RunsView
from .views.sessions import SessionsView


class MainWindow(QMainWindow):
    """The Phase 5 desktop shell with one stable page instance per destination."""

    def __init__(self, context: GuiApplicationContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.setObjectName("mainWindow")
        self.setWindowTitle(f"BenchPup - {context.version}")
        self.setMinimumSize(960, 640)
        self.resize(1280, 800)

        self.navigation = NavigationSidebar()
        self.page_stack = QStackedWidget()
        self.page_stack.setObjectName("pageStack")
        self.page_stack.setAccessibleName("BenchPup page content")
        self.pages: dict[str, QWidget] = {}
        self.page_indices: dict[str, int] = {}
        self._active_add_run: AddRunWizard | None = None
        self._build_shell()
        self.navigation.page_changed.connect(self._show_page)
        self.navigation.set_current_page("dashboard")

    def _build_shell(self) -> None:
        shell = QWidget()
        shell.setObjectName("mainShell")
        root_layout = QVBoxLayout(shell)
        root_layout.setContentsMargins(18, 16, 18, 0)
        root_layout.setSpacing(14)
        root_layout.addWidget(self._build_header())

        content_layout = QHBoxLayout()
        content_layout.setSpacing(14)
        self.navigation.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        content_layout.addWidget(self.navigation)
        content_layout.addWidget(self.page_stack, 1)
        root_layout.addLayout(content_layout, 1)
        self.setCentralWidget(shell)

        self.status_bar = QStatusBar()
        self.status_bar.setObjectName("applicationStatusBar")
        self.status_bar.setSizeGripEnabled(False)
        self.database_status_label = QLabel()
        self.database_status_label.setObjectName("databaseStatus")
        self.database_status_label.setToolTip(str(self.context.paths.database_path))
        self.database_status_label.setText(f"Database: {self.context.paths.database_path}")
        self.page_status_label = QLabel("Page: Dashboard")
        self.page_status_label.setObjectName("pageStatus")
        self.status_bar.showMessage("Ready")
        self.status_bar.addPermanentWidget(self.page_status_label)
        self.status_bar.addPermanentWidget(self.database_status_label)
        self.setStatusBar(self.status_bar)

        for destination in NAVIGATION_DESTINATIONS:
            page = (
                DashboardView(self.context)
                if destination.key == "dashboard"
                else RunsView(self.context)
                if destination.key == "runs"
                else SessionsView(self.context)
                if destination.key == "sessions"
                else ModelsView(self.context)
                if destination.key == "models"
                else BenchmarksView(self.context)
                if destination.key == "benchmarks"
                else PromptTemplatesView(self.context)
                if destination.key == "prompt_templates"
                else HardwareProfilesView(self.context)
                if destination.key == "hardware_profiles"
                else PlaceholderPage(destination.label, destination.description)
            )
            if isinstance(page, RunsView):
                page.add_run_requested.connect(self.open_add_run)
            if isinstance(page, CatalogPage):
                page.catalog_changed.connect(self._handle_catalog_changed)
                page.status_message.connect(lambda message: self.status_bar.showMessage(message, 6000))
            self.pages[destination.key] = page
            self.page_indices[destination.key] = self.page_stack.addWidget(page)

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("appHeader")
        header.setAccessibleName("BenchPup application header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 15, 20, 15)
        layout.setSpacing(14)

        brand = QVBoxLayout()
        brand.setSpacing(2)
        title = QLabel("BenchPup")
        title.setObjectName("brandTitle")
        title.setAccessibleName("BenchPup name")
        version = QLabel(f"Version {self.context.version}")
        version.setObjectName("brandVersion")
        version.setAccessibleName(f"BenchPup version {self.context.version}")
        subtitle = QLabel("Local benchmark intelligence for your models")
        subtitle.setObjectName("brandSubtitle")
        subtitle.setWordWrap(True)
        brand.addWidget(title)
        brand.addWidget(version)
        brand.addWidget(subtitle)
        layout.addLayout(brand, 1)

        self.add_run_button = QPushButton("Add Run")
        self.add_run_button.setAccessibleName("Add benchmark run")
        self.add_run_button.setToolTip("Open the review-before-save Add Run workflow")
        self.add_run_button.clicked.connect(self.open_add_run)
        self.export_button = QPushButton("Export")
        self.export_button.setEnabled(False)
        self.export_button.setAccessibleName("Export, planned")
        self.export_button.setToolTip("Export dialogs are coming in a later Phase 5 slice")
        layout.addWidget(self.add_run_button)
        layout.addWidget(self.export_button)
        return header

    def _show_page(self, key: str) -> None:
        if key not in self.page_indices:
            return
        self.page_stack.setCurrentIndex(self.page_indices[key])
        label = next(destination.label for destination in NAVIGATION_DESTINATIONS if destination.key == key)
        self.page_status_label.setText(f"Page: {label}")
        self.status_bar.showMessage(f"{label} ready", 3000)
        page = self.pages[key]
        if key == "dashboard" and isinstance(page, DashboardView) and not page.has_loaded:
            page.refresh()
        if key == "runs" and isinstance(page, RunsView) and not page.has_loaded:
            page.refresh()
        if key in {"sessions", "models", "benchmarks", "prompt_templates", "hardware_profiles"} and isinstance(page, CatalogPage) and not page.has_loaded:
            page.refresh()

    def navigate_to(self, key: str) -> None:
        """Select a destination without constructing a second page."""

        self.navigation.set_current_page(key)

    def current_page_key(self) -> str | None:
        return self.navigation.current_page()

    def open_add_run(self) -> None:
        """Open the shared Add Run workflow from either shell entry point."""

        wizard = AddRunWizard(self.context, self)
        wizard.run_created.connect(self._handle_run_created)
        self._active_add_run = wizard
        try:
            wizard.exec()
        finally:
            self._active_add_run = None

    def _handle_catalog_changed(self, _entity: str) -> None:
        """Keep catalog-dependent pages and an open Add Run wizard current."""

        if self._active_add_run is not None:
            self._active_add_run.refresh_catalog_choices()
        if self.dashboard.has_loaded:
            self.dashboard.refresh()
        if self.runs.has_loaded:
            self.runs.refresh()

    def _handle_run_created(self, run_id: int) -> None:
        runs = self.runs
        runs.refresh()
        self.dashboard.refresh()
        self.navigate_to("runs")
        runs.select_run(run_id, reveal=True)
        self.status_bar.showMessage(f"Run #{run_id} saved successfully.", 6000)

    @property
    def dashboard(self) -> DashboardView:
        page = self.pages["dashboard"]
        assert isinstance(page, DashboardView)
        return page

    @property
    def runs(self) -> RunsView:
        page = self.pages["runs"]
        assert isinstance(page, RunsView)
        return page

    @property
    def sessions(self) -> SessionsView:
        page = self.pages["sessions"]
        assert isinstance(page, SessionsView)
        return page

    @property
    def models(self) -> ModelsView:
        page = self.pages["models"]
        assert isinstance(page, ModelsView)
        return page

    @property
    def benchmarks(self) -> BenchmarksView:
        page = self.pages["benchmarks"]
        assert isinstance(page, BenchmarksView)
        return page

    @property
    def prompt_templates(self) -> PromptTemplatesView:
        page = self.pages["prompt_templates"]
        assert isinstance(page, PromptTemplatesView)
        return page

    @property
    def hardware_profiles(self) -> HardwareProfilesView:
        page = self.pages["hardware_profiles"]
        assert isinstance(page, HardwareProfilesView)
        return page

    def closeEvent(self, event: object) -> None:
        try:
            self.context.close()
        except Exception:
            self.context.logger.exception("BenchPup GUI shutdown failed")
        super().closeEvent(event)  # type: ignore[arg-type]
