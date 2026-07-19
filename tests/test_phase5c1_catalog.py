from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtCore import QDate, QDateTime, QLocale, QTime, QTimeZone, Qt
from PySide6.QtWidgets import QApplication

from engine.domain import BenchmarkDefinition, BenchmarkRun, BenchmarkSession, ModelProfile, serialize_utc_timestamp
from engine.database import EngineDatabase
from engine.services import BenchmarkService, CatalogService
from gui.context import GuiApplicationContext
from gui.dialogs.benchmark_editor import BenchmarkEditorDialog
from gui.dialogs.model_editor import ModelEditorDialog
from gui.dialogs.session_editor import SessionEditorDialog
from gui.main_window import MainWindow
from gui.models.catalog_table_model import CatalogTableModel, CatalogTableRow
from gui.views.add_run import AddRunWizard
from gui.views.benchmarks import BenchmarksView
from gui.views.models import ModelsView
from gui.views.sessions import SessionsView


class CatalogServiceFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = EngineDatabase(Path(self.directory.name) / "benchmark.db")
        self.database.migrate()
        self.catalog = CatalogService(self.database)
        self.benchmarks = BenchmarkService(self.database, self.catalog)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_sessions_validate_stored_timestamps_and_support_lifecycle_and_run_counts(self) -> None:
        first = self.catalog.create_session(
            BenchmarkSession(
                title="Zulu session",
                started_at="2026-07-18T10:00:00",
                completed_at="2026-07-18T11:00:00",
            )
        )
        second = self.catalog.create_session(BenchmarkSession(title="Alpha session"))
        self.assertEqual([item.title for item in self.catalog.list_sessions()], ["Alpha session", "Zulu session"])
        self.assertEqual(first.started_at, "2026-07-18T10:00:00")

        run, _ = self.benchmarks.save_run(BenchmarkRun(raw_model_output="session-counted", session_id=first.id))
        self.benchmarks.runs.delete(run.id, soft=True)
        self.benchmarks.save_run(BenchmarkRun(raw_model_output="session-counted-live", session_id=first.id))
        self.assertEqual(self.catalog.session_run_counts(), {first.id: 1})

        archived = self.catalog.archive_session(first.id)
        self.assertTrue(archived.is_deleted)
        self.assertEqual([item.title for item in self.catalog.list_sessions()], ["Alpha session"])
        self.assertEqual({item.title for item in self.catalog.list_sessions(include_deleted=True)}, {"Alpha session", "Zulu session"})
        restored = self.catalog.restore_session(first.id)
        self.assertFalse(restored.is_deleted)
        self.assertEqual({item.title for item in self.catalog.list_sessions()}, {"Alpha session", "Zulu session"})
        self.assertIsNotNone(self.catalog.get_session(second.id))

        with self.assertRaises(ValueError):
            self.catalog.create_session(BenchmarkSession(title="Bad date", started_at="not-a-timestamp"))
        with self.assertRaises(ValueError):
            self.catalog.create_session(
                BenchmarkSession(
                    title="Bad order",
                    started_at="2026-07-18T12:00:00+00:00",
                    completed_at="2026-07-18T11:00:00+00:00",
                )
            )

    def test_model_default_exclusivity_and_optional_zero_values_are_engine_owned(self) -> None:
        first = self.catalog.create_model_profile(ModelProfile(name="First", model_name="model-1"), make_default=True)
        second = self.catalog.create_model_profile(
            ModelProfile(
                name="Second",
                model_name="model-2",
                temperature=0.0,
                top_p=0.0,
                top_k=0,
                min_p=0.0,
                context_length=1,
                tokens_per_second=0.0,
            )
        )
        self.catalog.set_default_model_profile(second.id)
        records = {record.name: record for record in self.catalog.list_model_profiles()}
        self.assertFalse(records[first.name].is_default)
        self.assertTrue(records[second.name].is_default)
        self.assertEqual(records[second.name].temperature, 0.0)
        self.assertEqual(records[second.name].top_p, 0.0)
        self.assertEqual(records[second.name].top_k, 0)
        self.assertEqual(records[second.name].min_p, 0.0)
        self.assertEqual(records[second.name].tokens_per_second, 0.0)

        with self.assertRaises(ValueError):
            self.catalog.create_model_profile(ModelProfile(name="Negative speed", model_name="bad", tokens_per_second=-1))

    def test_benchmark_visibility_lifecycle_and_snapshot_preservation(self) -> None:
        session = self.catalog.create_session(BenchmarkSession(title="Snapshot session"))
        model = self.catalog.create_model_profile(ModelProfile(name="Snapshot model", model_name="model-before"))
        definition = self.catalog.create_benchmark_definition(
            BenchmarkDefinition(
                name="Snapshot benchmark",
                file_path="bench-before.py",
                benchmark_type="code_review",
                default_prompt="before",
                tags="initial",
            )
        )
        run, _ = self.benchmarks.save_run(
            BenchmarkRun(
                raw_model_output="snapshot output",
                session_id=session.id,
                model_profile_id=model.id,
                benchmark_definition_id=definition.id,
            )
        )
        model_snapshot = dict(run.model_snapshot)
        benchmark_snapshot = dict(run.benchmark_snapshot)

        self.catalog.update_model_profile(replace(model, model_name="model-after"))
        self.catalog.update_benchmark_definition(replace(definition, file_path="bench-after.py", default_prompt="after"))
        self.catalog.deactivate_benchmark_definition(definition.id)
        self.catalog.archive_session(session.id)

        active = self.catalog.list_benchmark_definitions()
        all_definitions = self.catalog.list_benchmark_definitions(include_inactive=True)
        self.assertEqual(active, [])
        self.assertEqual([item.name for item in all_definitions], ["Snapshot benchmark"])
        self.assertFalse(all_definitions[0].is_active)

        stored, _, _ = self.benchmarks.get_run(run.id)
        self.assertEqual(stored.model_snapshot, model_snapshot)
        self.assertEqual(stored.benchmark_snapshot, benchmark_snapshot)
        self.catalog.reactivate_benchmark_definition(definition.id)
        self.assertTrue(self.catalog.get_benchmark_definition(definition.id).is_active)

    def test_utc_timestamp_serializer_requires_awareness_and_preserves_the_instant(self) -> None:
        with self.assertRaises(ValueError):
            serialize_utc_timestamp(datetime(2026, 7, 18, 14, 30))
        self.assertEqual(
            serialize_utc_timestamp(datetime(2026, 7, 18, 14, 30, tzinfo=timezone.utc)),
            "2026-07-18T14:30:00+00:00",
        )


class CatalogTableModelTests(unittest.TestCase):
    def test_rows_are_selectable_and_sortable_but_not_editable(self) -> None:
        model: CatalogTableModel[str] = CatalogTableModel(("Name", "Count"))
        model.set_rows(
            (
                CatalogTableRow("Zulu", ("Zulu", "2"), ("Zulu", "2"), ("zulu", 2), "zulu"),
                CatalogTableRow("Alpha", ("Alpha", "1"), ("Alpha", "1"), ("alpha", 1), "alpha"),
            )
        )
        index = model.index(0, 0)
        flags = model.flags(index)
        self.assertTrue(flags & Qt.ItemFlag.ItemIsEnabled)
        self.assertTrue(flags & Qt.ItemFlag.ItemIsSelectable)
        self.assertFalse(flags & Qt.ItemFlag.ItemIsEditable)
        self.assertEqual(model.data(index, Qt.ItemDataRole.UserRole + 11), "Zulu")
        model.sort(0, Qt.SortOrder.AscendingOrder)
        self.assertEqual(model.record_at(0), "Alpha")
        model.sort(1, Qt.SortOrder.DescendingOrder)
        self.assertEqual(model.record_at(0), "Zulu")


class GuiCatalogFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-phase5c1-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.context = GuiApplicationContext.create(
            database_path=Path(self.directory.name) / "data" / "benchmark.db"
        )
        self.session = self.context.catalog.create_session(BenchmarkSession(title="Active session", description="July work"))
        self.archived_session = self.context.catalog.create_session(BenchmarkSession(title="Archived session"))
        self.context.catalog.archive_session(self.archived_session.id)
        self.default_model = self.context.catalog.create_model_profile(
            ModelProfile(name="Zed profile", model_name="zed", is_default=True)
        )
        self.other_model = self.context.catalog.create_model_profile(ModelProfile(name="Alpha profile", model_name="alpha"))
        self.benchmark = self.context.catalog.create_benchmark_definition(
            BenchmarkDefinition(
                name="Active benchmark",
                file_path="active.py",
                benchmark_type="code_review",
                default_prompt="inspect code",
                tags="python,review",
            )
        )
        self.inactive_benchmark = self.context.catalog.create_benchmark_definition(
            BenchmarkDefinition(name="Inactive benchmark", file_path="inactive.py", benchmark_type="revision", is_active=False)
        )
        self.context.benchmarks.save_run(
            BenchmarkRun(
                raw_model_output="catalog page run",
                session_id=self.session.id,
                model_profile_id=self.default_model.id,
                benchmark_definition_id=self.benchmark.id,
            )
        )
        self.widgets: list[object] = []

    def tearDown(self) -> None:
        for widget in self.widgets:
            widget.close()  # type: ignore[attr-defined]
        self.context.close()
        self.directory.cleanup()

    @staticmethod
    def _local_datetime(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> QDateTime:
        return QDateTime(QDate(year, month, day), QTime(hour, minute, second), QTimeZone.systemTimeZone())

    def _assert_canonical_utc(self, value: str | None) -> datetime:
        self.assertIsNotNone(value)
        assert value is not None
        self.assertIn("T", value)
        self.assertTrue(value.endswith("Z") or value.endswith("+00:00"))
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(parsed))
        return parsed

    def _save_session_dialog(
        self,
        title: str,
        started: QDateTime,
        completed: QDateTime | None = None,
    ) -> BenchmarkSession:
        dialog = SessionEditorDialog(self.context, confirm_close=lambda: True)
        try:
            dialog.title_edit.setText(title)
            dialog.started_edit.setDateTime(started)
            if completed is not None:
                dialog.completed_checkbox.setChecked(True)
                dialog.completed_edit.setDateTime(completed)
            self.assertTrue(dialog._save(), dialog.validation_summary.text())
            assert dialog.saved_record is not None
            saved = self.context.catalog.get_session(dialog.saved_record.id)
            assert saved is not None
            return saved
        finally:
            dialog.close()

    def test_pages_filter_search_and_apply_lifecycle_actions(self) -> None:
        sessions = SessionsView(self.context, confirm_action=lambda _title, _message: True)
        models = ModelsView(self.context, confirm_action=lambda _title, _message: True)
        benchmarks = BenchmarksView(self.context, confirm_action=lambda _title, _message: True)
        self.widgets.extend((sessions, models, benchmarks))

        self.assertEqual(sessions.rows[0].values[0], "Active session")
        self.assertEqual(sessions.rows[0].values[4], "1")
        sessions.search_edit.setText("july")
        self.assertEqual(sessions.catalog_table.model().rowCount(), 1)
        sessions.search_edit.clear()
        self.assertTrue(sessions.select_record(self.session.id))
        self.assertEqual(sessions.lifecycle_button.text(), "Archive")
        sessions.apply_lifecycle(self.session)
        self.assertEqual([item.title for item in self.context.catalog.list_sessions()], [])
        sessions.lifecycle_filter.setCurrentIndex(sessions.lifecycle_filter.findData("archived"))
        self.assertEqual(sessions.catalog_table.model().rowCount(), 2)
        self.assertTrue(sessions.select_record(self.session.id))
        sessions.apply_lifecycle(self.context.catalog.get_session(self.session.id))
        self.assertEqual([item.title for item in self.context.catalog.list_sessions()], ["Active session"])

        self.assertEqual([row.record.name for row in models.rows], ["Alpha profile", "Zed profile"])
        models.search_edit.setText("zed")
        self.assertEqual(models.catalog_table.model().rowCount(), 1)
        models.search_edit.clear()
        self.assertTrue(models.select_record(self.other_model.id))
        self.assertTrue(models.make_default_button.isEnabled())
        models.make_default_selected()
        selected_defaults = [record.name for record in self.context.catalog.list_model_profiles() if record.is_default]
        self.assertEqual(selected_defaults, ["Alpha profile"])

        benchmarks.lifecycle_filter.setCurrentIndex(benchmarks.lifecycle_filter.findData("inactive"))
        self.assertEqual(benchmarks.catalog_table.model().rowCount(), 1)
        self.assertTrue(benchmarks.select_record(self.inactive_benchmark.id))
        benchmarks.apply_lifecycle(self.inactive_benchmark)
        self.assertTrue(self.context.catalog.get_benchmark_definition(self.inactive_benchmark.id).is_active)
        self.assertEqual(benchmarks.catalog_table.model().rowCount(), 0)

    def test_editors_save_explicit_fields_and_keep_blank_or_zero_distinct(self) -> None:
        session_dialog = SessionEditorDialog(self.context, confirm_close=lambda: True)
        session_dialog.title_edit.setText("Dialog session")
        session_dialog.started_edit.setDateTime(self._local_datetime(2026, 7, 18, 8, 0))
        self.assertTrue(session_dialog._save())
        self.assertIsNone(session_dialog.saved_record.completed_at)
        self.assertEqual(self.context.catalog.get_session(session_dialog.saved_record.id).title, "Dialog session")

        model_dialog = ModelEditorDialog(self.context, confirm_close=lambda: True)
        model_dialog.name_edit.setText("Dialog model")
        model_dialog.model_name_edit.setText("dialog-model")
        model_dialog.temperature_field.record_checkbox.setChecked(True)
        model_dialog.temperature_field.spin_box.setValue(0.0)
        self.assertTrue(model_dialog._save())
        self.assertEqual(model_dialog.saved_record.temperature, 0.0)
        self.assertIsNone(model_dialog.saved_record.top_p)
        self.assertEqual(model_dialog.saved_record.tokens_per_second, None)

        benchmark_dialog = BenchmarkEditorDialog(self.context, confirm_close=lambda: True)
        benchmark_dialog.name_edit.setText("Dialog benchmark")
        benchmark_dialog.file_path_edit.setText("target.txt")
        benchmark_dialog.default_prompt_edit.setPlainText("Use the exact target.")
        benchmark_dialog.tags_edit.setText("dialog,exact")
        benchmark_dialog.type_combo.setCurrentIndex(benchmark_dialog.type_combo.findData("revision"))
        self.assertTrue(benchmark_dialog._save())
        self.assertEqual(benchmark_dialog.saved_record.benchmark_type, "revision")
        self.assertEqual(benchmark_dialog.saved_record.default_prompt, "Use the exact target.")
        self.assertEqual(benchmark_dialog.saved_record.tags, "dialog,exact")

        invalid = SessionEditorDialog(self.context, confirm_close=lambda: True)
        invalid.title_edit.setText("Invalid session")
        invalid.started_edit.setDateTime(self._local_datetime(2026, 7, 18, 9, 0))
        invalid.completed_checkbox.setChecked(True)
        invalid.completed_edit.setDateTime(self._local_datetime(2026, 7, 18, 8, 0))
        self.assertFalse(invalid._save())
        self.assertIn("at or after", invalid.validation_summary.text())
        self.assertIsNone(self.context.catalog.get_session(invalid.saved_record.id) if invalid.saved_record else None)
        for dialog in (session_dialog, model_dialog, benchmark_dialog, invalid):
            dialog.close()

    def test_session_editor_default_started_value_saves_canonical_utc_and_blank_completion(self) -> None:
        dialog = SessionEditorDialog(self.context, confirm_close=lambda: True)
        try:
            self.assertEqual(dialog.started_edit.displayFormat(), SessionEditorDialog.DATE_TIME_DISPLAY_FORMAT)
            self.assertEqual(dialog.completed_edit.displayFormat(), SessionEditorDialog.DATE_TIME_DISPLAY_FORMAT)
            self.assertFalse(dialog.completed_checkbox.isChecked())
            dialog.title_edit.setText("Default timestamp session")
            self.assertTrue(dialog._save(), dialog.validation_summary.text())
            saved = self.context.catalog.get_session(dialog.saved_record.id)
            self.assertIsNotNone(saved)
            assert saved is not None
            saved.validate()
            self._assert_canonical_utc(saved.started_at)
            self.assertIsNone(saved.completed_at)
        finally:
            dialog.close()

    def test_session_editor_uses_system_timezone_for_midnight_morning_afternoon_and_late_night(self) -> None:
        for hour, minute in ((0, 0), (9, 5), (14, 30), (23, 59)):
            with self.subTest(hour=hour, minute=minute):
                selected = self._local_datetime(2026, 7, 18, hour, minute)
                saved = self._save_session_dialog(f"Local {hour:02d}{minute:02d}", selected)
                stored = self._assert_canonical_utc(saved.started_at)
                self.assertEqual(int(stored.timestamp() * 1000), selected.toUTC().toMSecsSinceEpoch())

    def test_session_editor_populated_completion_is_canonical_and_earlier_completion_keeps_friendly_error(self) -> None:
        started = self._local_datetime(2026, 7, 18, 9, 5)
        completed = self._local_datetime(2026, 7, 18, 14, 30)
        saved = self._save_session_dialog("Completed timestamp session", started, completed)
        stored_started = self._assert_canonical_utc(saved.started_at)
        stored_completed = self._assert_canonical_utc(saved.completed_at)
        self.assertEqual(int(stored_started.timestamp() * 1000), started.toUTC().toMSecsSinceEpoch())
        self.assertEqual(int(stored_completed.timestamp() * 1000), completed.toUTC().toMSecsSinceEpoch())

        invalid = SessionEditorDialog(self.context, confirm_close=lambda: True)
        try:
            invalid.title_edit.setText("Earlier completion")
            invalid.started_edit.setDateTime(self._local_datetime(2026, 7, 18, 14, 30))
            invalid.completed_checkbox.setChecked(True)
            invalid.completed_edit.setDateTime(self._local_datetime(2026, 7, 18, 9, 5))
            self.assertFalse(invalid._save())
            self.assertIn("completed_at must be at or after started_at", invalid.validation_summary.text())
            self.assertFalse(any(item.title == "Earlier completion" for item in self.context.catalog.list_sessions(include_deleted=True)))
        finally:
            invalid.close()

    def test_session_editor_uses_explicit_24_hour_format_under_12_hour_locale(self) -> None:
        previous_locale = QLocale()
        try:
            stored_values = []
            for index, locale in enumerate((
                QLocale(QLocale.Language.English, QLocale.Country.UnitedStates),
                QLocale(QLocale.Language.German, QLocale.Country.Germany),
            )):
                QLocale.setDefault(locale)
                dialog = SessionEditorDialog(self.context, confirm_close=lambda: True)
                try:
                    selected = self._local_datetime(2026, 7, 18, 14, 30)
                    dialog.title_edit.setText(f"Locale-independent timestamp {index}")
                    dialog.started_edit.setDateTime(selected)
                    self.assertEqual(dialog.started_edit.text(), "2026-07-18 14:30")
                    self.assertNotIn("PM", dialog.started_edit.text())
                    self.assertTrue(dialog._save(), dialog.validation_summary.text())
                    saved = self.context.catalog.get_session(dialog.saved_record.id)
                    self._assert_canonical_utc(saved.started_at)
                    stored_values.append(saved.started_at)
                finally:
                    dialog.close()
            self.assertEqual(stored_values[0], stored_values[1])
        finally:
            QLocale.setDefault(previous_locale)

    def test_session_editor_edit_round_trip_preserves_existing_utc_instants_and_displays_local_time(self) -> None:
        started = self._local_datetime(2026, 7, 18, 14, 30, 37)
        completed = self._local_datetime(2026, 7, 18, 23, 59, 42)
        started_msecs = started.toUTC().toMSecsSinceEpoch()
        completed_msecs = completed.toUTC().toMSecsSinceEpoch()
        initial = self.context.catalog.create_session(
            BenchmarkSession(
                title="Round-trip session",
                started_at=serialize_utc_timestamp(datetime.fromtimestamp(started_msecs / 1000, tz=timezone.utc)),
                completed_at=serialize_utc_timestamp(datetime.fromtimestamp(completed_msecs / 1000, tz=timezone.utc)),
            )
        )
        dialog = SessionEditorDialog(self.context, initial, confirm_close=lambda: True)
        try:
            expected_started_display = QDateTime.fromMSecsSinceEpoch(started_msecs, QTimeZone.systemTimeZone()).toString(SessionEditorDialog.DATE_TIME_DISPLAY_FORMAT)
            expected_completed_display = QDateTime.fromMSecsSinceEpoch(completed_msecs, QTimeZone.systemTimeZone()).toString(SessionEditorDialog.DATE_TIME_DISPLAY_FORMAT)
            self.assertEqual(dialog.started_edit.text(), expected_started_display)
            self.assertEqual(dialog.completed_edit.text(), expected_completed_display)
            dialog.title_edit.setText("Round-trip renamed")
            self.assertTrue(dialog._save(), dialog.validation_summary.text())
        finally:
            dialog.close()
        saved = self.context.catalog.get_session(initial.id)
        self.assertIsNotNone(saved)
        assert saved is not None
        stored_started = self._assert_canonical_utc(saved.started_at)
        stored_completed = self._assert_canonical_utc(saved.completed_at)
        self.assertEqual(int(stored_started.timestamp() * 1000), started_msecs)
        self.assertEqual(int(stored_completed.timestamp() * 1000), completed_msecs)

    def test_session_editor_uses_system_timezone_rules_for_seasonal_dates(self) -> None:
        zone = QTimeZone.systemTimeZone()
        winter = self._local_datetime(2026, 1, 15, 12, 0)
        summer = self._local_datetime(2026, 7, 15, 12, 0)
        for label, selected in (("winter", winter), ("summer", summer)):
            with self.subTest(label=label):
                saved = self._save_session_dialog(f"DST {label}", selected)
                stored = self._assert_canonical_utc(saved.started_at)
                self.assertEqual(int(stored.timestamp() * 1000), selected.toUTC().toMSecsSinceEpoch())
                self.assertEqual(selected.timeZone().id(), zone.id())
        if zone.hasDaylightTime():
            self.assertNotEqual(winter.offsetFromUtc(), summer.offsetFromUtc())

    def test_session_editor_cancel_writes_nothing(self) -> None:
        before = {item.title for item in self.context.catalog.list_sessions(include_deleted=True)}
        dialog = SessionEditorDialog(self.context, confirm_close=lambda: True)
        dialog.title_edit.setText("Cancelled session")
        dialog.reject()
        after = {item.title for item in self.context.catalog.list_sessions(include_deleted=True)}
        self.assertEqual(before, after)

    def test_add_run_refresh_uses_new_active_catalog_records_and_preserves_selection(self) -> None:
        wizard = AddRunWizard(self.context, confirm_close=lambda: True)
        self.widgets.append(wizard)
        wizard.model_combo.setCurrentIndex(wizard.model_combo.findData(self.other_model.id))
        new_model = self.context.catalog.create_model_profile(ModelProfile(name="New profile", model_name="new"))
        new_session = self.context.catalog.create_session(BenchmarkSession(title="New session"))
        new_benchmark = self.context.catalog.create_benchmark_definition(
            BenchmarkDefinition(name="New benchmark", file_path="new.py", benchmark_type="code_generation")
        )
        hidden_benchmark = self.context.catalog.create_benchmark_definition(
            BenchmarkDefinition(name="Hidden benchmark", file_path="hidden.py", benchmark_type="revision")
        )
        self.context.catalog.deactivate_benchmark_definition(hidden_benchmark.id)
        wizard.refresh_catalog_choices()
        self.assertEqual(wizard.model_combo.currentData(), self.other_model.id)
        self.assertGreaterEqual(wizard.model_combo.findData(new_model.id), 0)
        self.assertGreaterEqual(wizard.session_combo.findData(new_session.id), 0)
        self.assertGreaterEqual(wizard.benchmark_combo.findData(new_benchmark.id), 0)
        self.assertEqual(wizard.benchmark_combo.findData(hidden_benchmark.id), -1)
        wizard.model_combo.setCurrentIndex(0)
        self.context.catalog.create_model_profile(ModelProfile(name="Another profile", model_name="another"))
        wizard.refresh_catalog_choices()
        self.assertIsNone(wizard.model_combo.currentData())

    def test_main_window_uses_functional_catalog_pages(self) -> None:
        window = MainWindow(self.context)
        self.widgets.append(window)
        self.assertIsInstance(window.sessions, SessionsView)
        self.assertIsInstance(window.models, ModelsView)
        self.assertIsInstance(window.benchmarks, BenchmarksView)
        window.navigate_to("models")
        self.assertIs(window.page_stack.currentWidget(), window.models)
        self.assertEqual(window.models.catalog_table.model().rowCount(), 2)


if __name__ == "__main__":
    unittest.main()
