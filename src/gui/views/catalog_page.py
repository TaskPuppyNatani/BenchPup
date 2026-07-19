"""Narrow shared presentation behavior for catalog-management pages."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..context import GuiApplicationContext
from ..models.catalog_table_model import CatalogTableModel, CatalogTableRow


class CatalogPage(QWidget):
    """Shared search, visibility, table, and action behavior for one catalog."""

    catalog_changed = Signal(str)
    status_message = Signal(str)
    refreshed = Signal()

    def __init__(
        self,
        context: GuiApplicationContext,
        *,
        title: str,
        description: str,
        record_label: str,
        headers: tuple[str, ...],
        lifecycle_options: tuple[tuple[str, str], ...] = (),
        confirm_action: Callable[[str, str], bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.record_label = record_label
        self._has_loaded = False
        self._last_error: BaseException | None = None
        self._all_rows: tuple[CatalogTableRow[Any], ...] = ()
        self.confirm_action = confirm_action or self._ask_confirmation
        slug = record_label.lower().replace(" ", "_")

        self.setObjectName(f"{slug}CatalogPage")
        self.setAccessibleName(f"{title} page")

        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        description_label = QLabel(description)
        description_label.setObjectName("pageDescription")
        description_label.setWordWrap(True)

        self.add_button = QPushButton(f"Add {record_label}")
        self.add_button.setObjectName("primaryButton")
        self.add_button.setAccessibleName(f"Add {record_label}")
        self.add_button.clicked.connect(self.add_record)

        self.edit_button = QPushButton(f"Edit {record_label}")
        self.edit_button.setObjectName("secondaryButton")
        self.edit_button.setAccessibleName(f"Edit selected {record_label}")
        self.edit_button.setEnabled(False)
        self.edit_button.clicked.connect(self.edit_selected)

        self.lifecycle_button = QPushButton()
        self.lifecycle_button.setObjectName("secondaryButton")
        self.lifecycle_button.setAccessibleName(f"Change lifecycle for selected {record_label}")
        self.lifecycle_button.setVisible(bool(lifecycle_options))
        self.lifecycle_button.setEnabled(False)
        self.lifecycle_button.clicked.connect(self.lifecycle_selected)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("secondaryButton")
        self.refresh_button.setAccessibleName(f"Refresh {title.lower()}")
        self.refresh_button.clicked.connect(self.refresh)

        self.refresh_status = QLabel("Ready")
        self.refresh_status.setObjectName("refreshStatus")
        self.refresh_status.setAccessibleName(f"{title} refresh status")

        self.action_layout = QHBoxLayout()
        self.action_layout.setSpacing(8)
        self.action_layout.addWidget(self.refresh_status)
        self.action_layout.addStretch(1)
        self.action_layout.addWidget(self.add_button)
        self.action_layout.addWidget(self.edit_button)
        self.action_layout.addWidget(self.lifecycle_button)
        self.action_layout.addWidget(self.refresh_button)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName(f"{slug}Search")
        self.search_edit.setAccessibleName(f"Search {title.lower()}")
        self.search_edit.setPlaceholderText(f"Search {record_label.lower()} records")
        self.search_edit.textChanged.connect(self._apply_search)

        self.lifecycle_filter: QComboBox | None = None
        if lifecycle_options:
            self.lifecycle_filter = QComboBox()
            self.lifecycle_filter.setObjectName(f"{slug}LifecycleFilter")
            self.lifecycle_filter.setAccessibleName(f"{title} visibility")
            for label, value in lifecycle_options:
                self.lifecycle_filter.addItem(label, value)
            self.lifecycle_filter.currentIndexChanged.connect(self.refresh)

        filter_frame = QFrame()
        filter_frame.setObjectName("catalogToolbar")
        filter_form = QFormLayout(filter_frame)
        filter_form.setContentsMargins(12, 10, 12, 10)
        filter_form.setHorizontalSpacing(12)
        filter_form.setVerticalSpacing(8)
        filter_form.addRow("Search", self.search_edit)
        if self.lifecycle_filter is not None:
            filter_form.addRow("Visibility", self.lifecycle_filter)

        self.result_count = QLabel("0 visible of 0 records")
        self.result_count.setObjectName("resultCount")
        self.result_count.setAccessibleName(f"Visible {title.lower()} count")

        self.empty_state = QLabel()
        self.empty_state.setObjectName("emptyState")
        self.empty_state.setWordWrap(True)

        self.error_state = QLabel()
        self.error_state.setObjectName("errorState")
        self.error_state.setWordWrap(True)
        self.error_state.setVisible(False)

        self.table_model: CatalogTableModel[Any] = CatalogTableModel(headers, self)
        self.catalog_table = QTableView()
        self.catalog_table.setObjectName(f"{slug}CatalogTable")
        self.catalog_table.setAccessibleName(f"{title} records")
        self.catalog_table.setModel(self.table_model)
        self.catalog_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.catalog_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.catalog_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.catalog_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.catalog_table.setTabKeyNavigation(True)
        self.catalog_table.setWordWrap(False)
        self.catalog_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.catalog_table.setAlternatingRowColors(True)
        self.catalog_table.setSortingEnabled(True)
        self.catalog_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.catalog_table.verticalHeader().setVisible(False)
        self.catalog_table.horizontalHeader().setStretchLastSection(True)
        self.catalog_table.horizontalHeader().setMinimumSectionSize(92)
        self.catalog_table.setMinimumHeight(260)
        self.catalog_table.activated.connect(self._handle_activation)
        self.catalog_table.doubleClicked.connect(self._handle_activation)
        self.catalog_table.selectionModel().selectionChanged.connect(self._selection_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 30)
        layout.setSpacing(12)
        layout.addLayout(self.action_layout)
        layout.addWidget(heading)
        layout.addWidget(description_label)
        layout.addWidget(filter_frame)
        results_row = QHBoxLayout()
        results_row.addWidget(QLabel("Results"))
        results_row.addStretch(1)
        results_row.addWidget(self.result_count)
        layout.addLayout(results_row)
        layout.addWidget(self.error_state)
        layout.addWidget(self.empty_state)
        layout.addWidget(self.catalog_table, 1)

        self.refresh()

    @property
    def has_loaded(self) -> bool:
        return self._has_loaded

    @property
    def rows(self) -> tuple[CatalogTableRow[Any], ...]:
        return self.table_model.rows()

    def visibility_value(self) -> str:
        if self.lifecycle_filter is None:
            return "active"
        return str(self.lifecycle_filter.currentData() or "active")

    def load_catalog_rows(self) -> tuple[CatalogTableRow[Any], ...]:
        raise NotImplementedError

    def add_record(self) -> None:
        raise NotImplementedError

    def open_editor(self, record: Any) -> None:
        raise NotImplementedError

    def apply_lifecycle(self, record: Any) -> None:
        raise NotImplementedError

    def lifecycle_label(self, record: Any) -> str:
        return "Change status"

    def record_id(self, record: Any) -> int | None:
        value = getattr(record, "id", None)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None

    def refresh(self) -> None:
        previous_id = self.record_id(self.selected_record()) if self.selected_record() is not None else None
        self.refresh_button.setEnabled(False)
        if self.lifecycle_filter is not None:
            self.lifecycle_filter.setEnabled(False)
        self.refresh_status.setText("Refreshing...")
        self.error_state.setVisible(False)
        try:
            self._all_rows = tuple(self.load_catalog_rows())
            self._apply_search()
            self._has_loaded = True
            self._last_error = None
            self.refresh_status.setText("Updated")
            if previous_id is not None:
                self.select_record(previous_id)
            self.refreshed.emit()
        except Exception as error:
            self.context.logger.error(
                "Catalog refresh failed for %s",
                self.record_label,
                exc_info=(type(error), error, error.__traceback__),
            )
            self._last_error = error
            self.error_state.setText("Records could not be refreshed. See logs/error.log for details.")
            self.error_state.setVisible(True)
            self.empty_state.setVisible(False)
            self.refresh_status.setText("Refresh failed")
            self._has_loaded = False
        finally:
            self.refresh_button.setEnabled(True)
            if self.lifecycle_filter is not None:
                self.lifecycle_filter.setEnabled(True)
            self._update_actions()

    def _apply_search(self, *_args: object) -> None:
        query = self.search_edit.text().strip().casefold()
        visible = tuple(row for row in self._all_rows if not query or query in row.search_text.casefold())
        self.table_model.set_rows(visible)
        self.result_count.setText(f"{len(visible)} visible of {len(self._all_rows)} records")
        if not self._all_rows:
            self.empty_state.setText(f"No {self.record_label.lower()} records have been created yet.")
            self.empty_state.setVisible(True)
        elif not visible:
            self.empty_state.setText("No records match the current search or visibility filter.")
            self.empty_state.setVisible(True)
        else:
            self.empty_state.setVisible(False)
        self._update_actions()

    def _selection_changed(self, *_args: object) -> None:
        self._update_actions()

    def _update_actions(self) -> None:
        record = self.selected_record()
        selected = record is not None
        self.edit_button.setEnabled(selected)
        if self.lifecycle_filter is not None:
            self.lifecycle_button.setEnabled(selected)
            self.lifecycle_button.setText(self.lifecycle_label(record) if selected else "Change status")
        self.update_entity_actions(record)

    def update_entity_actions(self, record: Any | None) -> None:
        """Hook for entity-specific toolbar actions."""

    def selected_record(self) -> Any | None:
        index = self.catalog_table.currentIndex()
        if not index.isValid():
            selected = self.catalog_table.selectionModel().selectedRows()
            index = selected[0] if selected else QModelIndex()
        return self.table_model.record_at(index.row()) if index.isValid() else None

    def select_record(self, record_id: int) -> bool:
        for row in range(self.table_model.rowCount()):
            record = self.table_model.record_at(row)
            if self.record_id(record) == record_id:
                index = self.table_model.index(row, 0)
                self.catalog_table.setCurrentIndex(index)
                self.catalog_table.selectRow(row)
                self.catalog_table.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
                self._update_actions()
                return True
        self._update_actions()
        return False

    def edit_selected(self) -> None:
        record = self.selected_record()
        if record is not None:
            self.open_editor(record)

    def lifecycle_selected(self) -> None:
        record = self.selected_record()
        if record is not None:
            self.apply_lifecycle(record)

    def _handle_activation(self, index: QModelIndex | QPersistentModelIndex) -> None:
        if index.isValid():
            self.catalog_table.setCurrentIndex(index)
            self.edit_selected()

    def show_operation_error(self, action: str, error: BaseException) -> None:
        self.context.logger.error(
            "Catalog %s failed for %s",
            action,
            self.record_label,
            exc_info=(type(error), error, error.__traceback__),
        )
        if isinstance(error, KeyError):
            message = "The selected record is no longer available. Refresh and try again."
        elif isinstance(error, ValueError):
            message = f"The record could not be {action}: {error}"
        else:
            message = f"The record could not be {action}. See logs/error.log for details."
        self.error_state.setText(message)
        self.error_state.setVisible(True)

    def notify_changed(self, message: str) -> None:
        self.refresh()
        self.catalog_changed.emit(self.record_label.casefold())
        self.status_message.emit(message)

    def _ask_confirmation(self, title: str, message: str) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes


__all__ = ("CatalogPage",)
