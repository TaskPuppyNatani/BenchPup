from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QLineEdit

from engine.archive import ArchiveService
from engine.domain import (
    BenchmarkDefinition,
    BenchmarkRun,
    BenchmarkSession,
    HardwareProfile,
    ModelProfile,
    PromptTemplate,
    ReviewScore,
    RunAttachment,
    prompt_hash_for,
)
from engine.services import (
    HardwareProfileDeleteError,
    HardwareProfileDeletePreview,
    HardwareProfileDeleteResult,
)
from gui.context import GuiApplicationContext
from gui.main_window import MainWindow
from gui.views.hardware_profiles import HardwareProfilesView
from gui.views.run_details import RunDetailsDialog


class HardwareProfileDeleteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-hardware-profile-delete-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.context = GuiApplicationContext.create(
            database_path=Path(self.directory.name) / "data" / "benchmark.db"
        )
        self.widgets: list[object] = []
        self.session = self.context.catalog.create_session(BenchmarkSession(title="Delete test session"))
        self.model = self.context.catalog.create_model_profile(
            ModelProfile(name="Delete test model", model_name="delete-test-model")
        )
        self.benchmark = self.context.catalog.create_benchmark_definition(
            BenchmarkDefinition(
                name="Delete test benchmark",
                file_path="delete-test.py",
                benchmark_type="code_review",
            )
        )
        prompt_text = "Review the captured output."
        self.prompt = self.context.catalog.create_prompt_template(
            PromptTemplate(
                name="Delete test prompt",
                version="1",
                prompt_text=prompt_text,
                prompt_hash=prompt_hash_for(prompt_text),
                benchmark_type="code_review",
            )
        )

    def tearDown(self) -> None:
        for widget in self.widgets:
            widget.close()  # type: ignore[attr-defined]
        self.context.close()
        self.directory.cleanup()

    def _profile(
        self,
        name: str,
        *,
        imported: bool = False,
    ) -> HardwareProfile:
        return self.context.catalog.create_hardware_profile(
            HardwareProfile(
                name=name,
                computer_name=f"{name}-computer",
                cpu="Delete test CPU",
                gpu="Delete test GPU",
                vram_gb=12.0,
                ram_gb=32.0,
                operating_system="Windows",
                backend_versions={"Backend": "1.0"},
                import_source="DXDiag" if imported else "",
                imported_at="2026-07-20T12:00:00+00:00" if imported else None,
            )
        )

    def _run_for_profile(
        self,
        profile: HardwareProfile,
        label: str,
        *,
        deleted: bool = False,
    ) -> BenchmarkRun:
        run, _ = self.context.benchmarks.save_run(
            BenchmarkRun(
                raw_model_output=f"captured output {label}",
                prompt_name=self.prompt.name,
                session_id=self.session.id,
                model_profile_id=self.model.id,
                benchmark_definition_id=self.benchmark.id,
                prompt_template_id=self.prompt.id,
                hardware_profile_id=profile.id,
            ),
            ReviewScore(run_id=-1, overall_score=4.0, verdict=f"Verdict {label}"),
        )
        if deleted:
            assert run.id is not None
            self.context.benchmarks.delete_run(run.id)
        return run

    def _page(
        self,
        *,
        confirm_action=None,
    ) -> HardwareProfilesView:
        page = HardwareProfilesView(self.context, confirm_action=confirm_action)
        self.widgets.append(page)
        return page

    def test_preview_is_typed_read_only_and_excludes_soft_deleted_runs(self) -> None:
        profile = self._profile("Preview profile")
        active = self._run_for_profile(profile, "active")
        hidden = self._run_for_profile(profile, "hidden", deleted=True)

        before_profiles = self.context.catalog.list_hardware_profiles()
        preview = self.context.catalog.preview_hardware_profile_delete(profile.id)  # type: ignore[arg-type]

        self.assertIsInstance(preview, HardwareProfileDeletePreview)
        self.assertEqual(
            (preview.profile_id, preview.profile_name, preview.active_run_count),
            (profile.id, profile.name, 1),
        )
        self.assertEqual(self.context.catalog.list_hardware_profiles(), before_profiles)
        active_after, _, _ = self.context.benchmarks.get_run(active.id)  # type: ignore[arg-type]
        hidden_after, _, _ = self.context.benchmarks.get_run(hidden.id)  # type: ignore[arg-type]
        self.assertEqual(active_after.hardware_profile_id, profile.id)  # type: ignore[union-attr]
        self.assertEqual(hidden_after.hardware_profile_id, profile.id)  # type: ignore[union-attr]

    def test_delete_referenced_profile_preserves_runs_reviews_attachments_and_snapshots(self) -> None:
        profile = self._profile("Referenced profile")
        run = self._run_for_profile(profile, "preserved")
        self.assertIsNotNone(run.id)
        attachment = self.context.benchmarks.add_attachment(
            RunAttachment(
                run_id=run.id,  # type: ignore[arg-type]
                attachment_type="log",
                file_path="C:/logs/preserved.log",
                original_filename="preserved.log",
            )
        )
        before_run, before_score, before_attachments = self.context.benchmarks.get_run(run.id)  # type: ignore[arg-type]
        self.assertIsNotNone(before_run)
        self.assertIsNotNone(before_score)
        self.assertEqual(before_attachments, [attachment])
        assert before_run is not None
        assert before_score is not None

        result = self.context.catalog.delete_hardware_profile(profile.id)  # type: ignore[arg-type]

        self.assertIsInstance(result, HardwareProfileDeleteResult)
        self.assertEqual(
            (result.profile_id, result.profile_name, result.detached_active_run_count),
            (profile.id, profile.name, 1),
        )
        after_run, after_score, after_attachments = self.context.benchmarks.get_run(run.id)  # type: ignore[arg-type]
        self.assertEqual(after_run, replace(before_run, hardware_profile_id=None))
        self.assertEqual(after_score, before_score)
        self.assertEqual(after_attachments, before_attachments)
        self.assertEqual(self.context.catalog.get_hardware_profile(profile.id), None)  # type: ignore[arg-type]
        self.assertNotIn(profile.id, [item.hardware_profile_id for item in self.context.benchmarks.runs.list()])

    def test_delete_multiple_active_runs_detaches_hidden_runs_without_deleting_them(self) -> None:
        profile = self._profile("Multiple runs profile")
        active_runs = [self._run_for_profile(profile, f"active-{index}") for index in range(2)]
        hidden = self._run_for_profile(profile, "hidden", deleted=True)
        before = {
            run.id: self.context.benchmarks.get_run(run.id)[0]  # type: ignore[arg-type]
            for run in [*active_runs, hidden]
        }

        result = self.context.catalog.delete_hardware_profile(profile.id)  # type: ignore[arg-type]

        self.assertEqual(result.detached_active_run_count, 2)
        for run in [*active_runs, hidden]:
            after, _, _ = self.context.benchmarks.get_run(run.id)  # type: ignore[arg-type]
            self.assertEqual(after, replace(before[run.id], hardware_profile_id=None))  # type: ignore[arg-type]
        self.assertEqual(len(self.context.benchmarks.runs.list(include_deleted=True)), 3)

    def test_missing_and_repeated_delete_use_missing_record_behavior(self) -> None:
        with self.assertRaises(KeyError):
            self.context.catalog.preview_hardware_profile_delete(999999)
        with self.assertRaises(KeyError):
            self.context.catalog.delete_hardware_profile(999999)

        profile = self._profile("Repeated delete profile")
        self.context.catalog.delete_hardware_profile(profile.id)  # type: ignore[arg-type]
        with self.assertRaises(KeyError):
            self.context.catalog.delete_hardware_profile(profile.id)  # type: ignore[arg-type]

    def test_delete_transaction_rolls_back_after_simulated_database_failure(self) -> None:
        profile = self._profile("Rollback profile")
        run = self._run_for_profile(profile, "rollback")
        with self.context.database.connection() as connection:
            connection.execute(
                "CREATE TRIGGER fail_hardware_profile_delete "
                "AFTER DELETE ON hardware_profiles "
                "BEGIN SELECT RAISE(ABORT, 'simulated delete failure'); END;"
            )
        try:
            with self.assertRaises(HardwareProfileDeleteError):
                self.context.catalog.delete_hardware_profile(profile.id)  # type: ignore[arg-type]
        finally:
            with self.context.database.connection() as connection:
                connection.execute("DROP TRIGGER fail_hardware_profile_delete")

        self.assertIsNotNone(self.context.catalog.get_hardware_profile(profile.id))  # type: ignore[arg-type]
        remaining, _, _ = self.context.benchmarks.get_run(run.id)  # type: ignore[arg-type]
        self.assertEqual(remaining.hardware_profile_id, profile.id)  # type: ignore[union-attr]

    def test_manual_and_imported_profiles_have_identical_delete_semantics(self) -> None:
        manual = self._profile("Manual profile")
        imported = self._profile("Imported profile", imported=True)
        self._run_for_profile(manual, "manual")
        self._run_for_profile(imported, "imported")

        self.context.catalog.delete_hardware_profile(manual.id)  # type: ignore[arg-type]
        self.context.catalog.delete_hardware_profile(imported.id)  # type: ignore[arg-type]

        self.assertEqual(self.context.catalog.list_hardware_profiles(), [])
        self.assertEqual(len(self.context.benchmarks.runs.list()), 2)
        self.assertTrue(all(run.hardware_profile_id is None for run in self.context.benchmarks.runs.list(include_deleted=True)))

    def test_archive_after_delete_contains_null_relationship_and_restores_without_profile(self) -> None:
        profile = self._profile("Archived profile")
        run = self._run_for_profile(profile, "archived")
        snapshot = dict(run.hardware_snapshot)
        self.context.catalog.delete_hardware_profile(profile.id)  # type: ignore[arg-type]

        archive = ArchiveService(self.context.database).build_archive(self.context.version)

        self.assertEqual(archive["data"]["hardware_profiles"], [])
        archived_run = next(item for item in archive["data"]["benchmark_runs"] if item["id"] == run.id)
        self.assertIsNone(archived_run["hardware_profile_id"])
        self.assertEqual(json.loads(archived_run["hardware_snapshot"]), snapshot)

        target_database_path = Path(self.directory.name) / "target" / "benchmark.db"
        target_context = GuiApplicationContext.create(database_path=target_database_path)
        try:
            report = ArchiveService(target_context.database).merge(archive)
            self.assertEqual(report.created["benchmark_runs"], 1)
            with target_context.database.connection() as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            target_context.close()

    def test_run_details_still_displays_historical_hardware_snapshot_after_delete(self) -> None:
        profile = self._profile("Historical display profile")
        run = self._run_for_profile(profile, "details")
        self.context.catalog.delete_hardware_profile(profile.id)  # type: ignore[arg-type]

        dialog = RunDetailsDialog(self.context, run.id)  # type: ignore[arg-type]
        self.widgets.append(dialog)
        displayed_values = [field.text() for field in dialog.findChildren(QLineEdit)]

        self.assertIn(profile.name, displayed_values)

    def test_hardware_profiles_delete_button_state_confirmation_and_cancel(self) -> None:
        profile = self._profile("Cancel profile")
        confirmations: list[tuple[str, str]] = []
        page = self._page(confirm_action=lambda title, message: confirmations.append((title, message)) or False)

        self.assertEqual(page.delete_button.text(), "Delete Hardware Profile")
        self.assertFalse(page.delete_button.isEnabled())
        self.assertTrue(page.select_record(profile.id))  # type: ignore[arg-type]
        self.assertTrue(page.delete_button.isEnabled())
        with patch.object(
            self.context.catalog,
            "delete_hardware_profile",
            wraps=self.context.catalog.delete_hardware_profile,
        ) as delete:
            page.delete_button.click()
            delete.assert_not_called()

        self.assertEqual(len(confirmations), 1)
        self.assertIn(profile.name, confirmations[0][1])
        self.assertIn("No active benchmark runs reference", confirmations[0][1])
        self.assertIn("No in-app undo", confirmations[0][1])
        self.assertIsNotNone(self.context.catalog.get_hardware_profile(profile.id))  # type: ignore[arg-type]
        self.assertTrue(page.delete_button.isEnabled())

    def test_referenced_confirmation_uses_singular_and_plural_active_run_wording(self) -> None:
        singular = self._profile("Singular profile")
        self._run_for_profile(singular, "singular")
        singular_messages: list[str] = []
        singular_page = self._page(confirm_action=lambda _title, message: singular_messages.append(message) or False)
        singular_page.select_record(singular.id)  # type: ignore[arg-type]
        singular_page.delete_button.click()
        self.assertIn("1 active benchmark run will remain", singular_messages[0])
        self.assertIn("snapshots will not change", singular_messages[0])
        self.assertIn("reusable hardware-profile links", singular_messages[0])

        plural = self._profile("Plural profile")
        self._run_for_profile(plural, "plural-1")
        self._run_for_profile(plural, "plural-2")
        plural_messages: list[str] = []
        plural_page = self._page(confirm_action=lambda _title, message: plural_messages.append(message) or False)
        plural_page.select_record(plural.id)  # type: ignore[arg-type]
        plural_page.delete_button.click()
        self.assertIn("2 active benchmark runs will remain", plural_messages[0])

    def test_confirm_delete_calls_service_once_refreshes_and_selects_neighbor(self) -> None:
        first = self._profile("A profile")
        middle = self._profile("B profile")
        last = self._profile("C profile")
        page = self._page(confirm_action=lambda _title, _message: True)
        self.assertTrue(page.select_record(middle.id))  # type: ignore[arg-type]

        with patch.object(
            self.context.catalog,
            "delete_hardware_profile",
            wraps=self.context.catalog.delete_hardware_profile,
        ) as delete:
            page.delete_button.click()
            self.assertEqual(delete.call_count, 1)

        self.assertEqual([row.record.name for row in page.rows], [first.name, last.name])
        self.assertEqual(page.selected_record().id, last.id)  # type: ignore[union-attr]
        self.assertTrue(page.delete_button.isEnabled())
        self.assertEqual(self.context.catalog.get_hardware_profile(middle.id), None)  # type: ignore[arg-type]

    def test_double_activation_and_last_delete_are_safe(self) -> None:
        profile = self._profile("Only profile")
        reentrant_page: HardwareProfilesView | None = None
        confirmations = 0

        def confirm(_title: str, _message: str) -> bool:
            nonlocal confirmations
            confirmations += 1
            assert reentrant_page is not None
            reentrant_page.delete_button.click()
            return True

        reentrant_page = self._page(confirm_action=confirm)
        reentrant_page.select_record(profile.id)  # type: ignore[arg-type]
        with patch.object(
            self.context.catalog,
            "delete_hardware_profile",
            wraps=self.context.catalog.delete_hardware_profile,
        ) as delete:
            reentrant_page.delete_button.click()
            self.assertEqual(delete.call_count, 1)

        self.assertEqual(confirmations, 1)
        self.assertEqual(reentrant_page.catalog_table.model().rowCount(), 0)
        self.assertFalse(reentrant_page.delete_button.isEnabled())
        self.assertFalse(reentrant_page.empty_state.isHidden())

    def test_stale_selection_and_service_failure_keep_page_usable(self) -> None:
        stale = self._profile("Stale profile")
        stale_page = self._page(confirm_action=lambda _title, _message: True)
        stale_page.select_record(stale.id)  # type: ignore[arg-type]
        self.context.catalog.hardware_profiles.delete(stale.id)  # type: ignore[arg-type]
        stale_page.delete_button.click()
        self.assertEqual(stale_page.catalog_table.model().rowCount(), 0)
        self.assertFalse(stale_page.error_state.isHidden())

        failing = self._profile("Failure profile")
        failing_page = self._page(confirm_action=lambda _title, _message: True)
        failing_page.select_record(failing.id)  # type: ignore[arg-type]
        with patch.object(
            self.context.catalog,
            "delete_hardware_profile",
            side_effect=HardwareProfileDeleteError("simulated failure"),
        ) as delete:
            failing_page.delete_button.click()
            self.assertEqual(delete.call_count, 1)
        self.assertIsNotNone(self.context.catalog.get_hardware_profile(failing.id))  # type: ignore[arg-type]
        self.assertFalse(failing_page.error_state.isHidden())
        self.assertTrue(failing_page.delete_button.isEnabled())

    def test_main_window_catalog_change_refreshes_add_run_hardware_choices(self) -> None:
        profile = self._profile("Active Add Run profile")
        window = MainWindow(self.context)
        wizard = None
        self.widgets.append(window)
        try:
            from gui.views.add_run import AddRunWizard

            wizard = AddRunWizard(self.context, window)
            self.widgets.append(wizard)
            window._active_add_run = wizard
            self.assertGreaterEqual(wizard.hardware_combo.findData(profile.id), 0)  # type: ignore[arg-type]
            self.assertGreaterEqual(window.dataset_builder.hardware_combo.findData(profile.id), 0)  # type: ignore[arg-type]
            window.hardware_profiles.confirm_action = lambda _title, _message: True
            self.assertTrue(window.hardware_profiles.select_record(profile.id))  # type: ignore[arg-type]
            window.hardware_profiles.delete_button.click()
            self.application.processEvents()
            self.assertEqual(wizard.hardware_combo.findData(profile.id), -1)  # type: ignore[arg-type]
            self.assertEqual(window.dataset_builder.hardware_combo.findData(profile.id), -1)  # type: ignore[arg-type]
        finally:
            window._active_add_run = None


if __name__ == "__main__":
    unittest.main()
