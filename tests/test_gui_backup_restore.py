from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QDialog

from engine.archive import ArchiveError, ArchiveIssueCode
from engine.domain import BenchmarkRun, ReviewScore
from gui.context import GuiApplicationContext
from gui.dialogs.backup_restore import ATTACHMENT_WORDING, BackupRestoreDialog
from gui.main_window import MainWindow


class BackupRestoreGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-backup-restore-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.context = GuiApplicationContext.create(database_path=self.root / "data" / "benchmark.db")
        self.extra_contexts: list[GuiApplicationContext] = []
        self.source_run, _score = self.context.benchmarks.save_run(
            BenchmarkRun(
                raw_model_output="Archive GUI output",
                model_snapshot={"model_name": "Archive model", "backend": "LM Studio"},
                benchmark_snapshot={"name": "Archive benchmark", "benchmark_type": "code_review"},
                prompt_snapshot={"name": "Archive prompt", "prompt_text": "Review."},
                hardware_snapshot={"name": "Archive hardware"},
            ),
            ReviewScore(run_id=1, accuracy_score=4.0, overall_score=4.0),
        )
        self.archive_path = self.root / "source-backup.json"
        self.context.archives.export_typed(
            self.archive_path,
            self.context.version,
            create_parent=False,
        )

    def tearDown(self) -> None:
        for context in self.extra_contexts:
            context.close()
        self.context.close()
        self.directory.cleanup()

    def _new_context(self, name: str) -> GuiApplicationContext:
        context = GuiApplicationContext.create(
            database_path=self.root / name / "data" / "benchmark.db"
        )
        self.extra_contexts.append(context)
        return context

    def _dialog(
        self,
        *,
        confirm_backup=None,
        confirm_overwrite=None,
        confirm_merge=None,
        confirm_replace=None,
        context: GuiApplicationContext | None = None,
    ) -> BackupRestoreDialog:
        dialog = BackupRestoreDialog(
            context or self.context,
            confirm_backup=confirm_backup,
            confirm_overwrite=confirm_overwrite,
            confirm_merge=confirm_merge,
            confirm_replace=confirm_replace,
        )
        dialog.show()
        self.application.processEvents()
        return dialog

    def test_reports_page_launches_backup_restore_once_and_active_dialog_is_reused(self) -> None:
        window = MainWindow(self.context)
        try:
            window.navigate_to("reports")
            fake_dialog = MagicMock()
            fake_dialog.exec.return_value = QDialog.DialogCode.Rejected
            with patch("gui.main_window.BackupRestoreDialog", return_value=fake_dialog) as dialog_type:
                window.exports.backup_restore_button.click()
            self.assertEqual(dialog_type.call_count, 1)
            self.assertEqual(fake_dialog.backup_succeeded.connect.call_count, 1)
            self.assertEqual(fake_dialog.restore_succeeded.connect.call_count, 1)

            window._active_backup_restore = fake_dialog
            with patch("gui.main_window.BackupRestoreDialog") as duplicate_type:
                window.open_backup_restore()
            duplicate_type.assert_not_called()
            fake_dialog.raise_.assert_called_once_with()
            fake_dialog.activateWindow.assert_called_once_with()
            window._active_backup_restore = None
        finally:
            window.close()

    def test_successful_backup_uses_typed_service_and_reports_path_counts_and_attachment_boundary(self) -> None:
        target = self.root / "created-backup.json"
        completed: list[str] = []
        dialog = self._dialog(confirm_backup=lambda _path: True)
        dialog.backup_succeeded.connect(completed.append)
        try:
            dialog.backup_destination_edit.setText(str(target))
            dialog.backup_button.click()
            self.application.processEvents()
            self.assertTrue(target.exists())
            self.assertEqual(completed, [str(target.resolve())])
            self.assertIn("Backup completed successfully", dialog.backup_result_label.text())
            self.assertIn("benchmark_runs=1", dialog.backup_result_label.text())
            self.assertTrue(
                any(ATTACHMENT_WORDING in label.text() for label in dialog.findChildren(type(dialog.status_label)))
            )
            self.assertTrue(dialog.open_file_button.isVisible())
            self.assertTrue(dialog.open_folder_button.isVisible())
            self.assertFalse(self.context.settings.path.exists())
        finally:
            dialog.reject()

    def test_backup_cancellation_and_overwrite_decline_write_nothing(self) -> None:
        target = self.root / "cancelled-backup.json"
        cancelled = self._dialog(confirm_backup=lambda _path: False)
        try:
            with patch.object(self.context.archives, "export_typed") as export:
                cancelled.backup_destination_edit.setText(str(target))
                cancelled.backup_button.click()
            export.assert_not_called()
            self.assertFalse(target.exists())
            self.assertFalse(self.context.settings.path.exists())
        finally:
            cancelled.reject()

        existing = self.root / "existing-backup.json"
        existing.write_text("keep", encoding="utf-8")
        declined = self._dialog(confirm_overwrite=lambda: False)
        try:
            with patch.object(self.context.archives, "export_typed") as export:
                declined.backup_destination_edit.setText(str(existing))
                declined.backup_button.click()
            export.assert_not_called()
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
            self.assertIn("cancelled", declined.status_label.text().lower())
        finally:
            declined.reject()

    def test_backup_failure_restores_controls_and_hides_traceback(self) -> None:
        dialog = self._dialog(confirm_backup=lambda _path: True)
        try:
            failure = ArchiveError("permission denied", code=ArchiveIssueCode.PERMISSION_DENIED)
            with patch.object(self.context.archives, "export_typed", side_effect=failure):
                dialog.backup_button.click()
            self.assertFalse(dialog._busy)
            self.assertTrue(dialog.backup_button.isEnabled())
            self.assertIn("permission_denied", dialog.status_label.text())
            self.assertNotIn("Traceback", dialog.status_label.text())
            self.assertFalse(self.context.settings.path.exists())
        finally:
            dialog.reject()

    def test_backup_operation_guard_ignores_reentrant_activation(self) -> None:
        dialog = self._dialog(confirm_backup=lambda _path: True)
        try:
            real_export = self.context.archives.export_typed

            def reentrant_export(*args, **kwargs):
                dialog.create_backup()
                return real_export(*args, **kwargs)

            with patch.object(self.context.archives, "export_typed", side_effect=reentrant_export) as export:
                dialog.backup_destination_edit.setText(str(self.root / "guarded-backup.json"))
                dialog.backup_button.click()
            export.assert_called_once()
            self.assertTrue((self.root / "guarded-backup.json").exists())
        finally:
            dialog.reject()

    def test_open_file_and_folder_failures_are_non_fatal_after_backup(self) -> None:
        dialog = self._dialog(confirm_backup=lambda _path: True)
        try:
            dialog.backup_destination_edit.setText(str(self.root / "open-failure.json"))
            dialog.backup_button.click()
            with patch("gui.dialogs.backup_restore.QDesktopServices.openUrl", return_value=False), patch(
                "gui.dialogs.backup_restore.QMessageBox.warning"
            ) as warning:
                dialog.open_file_button.click()
                dialog.open_folder_button.click()
            self.assertEqual(warning.call_count, 2)
            self.assertTrue(dialog.backup_result_label.isVisible())
        finally:
            dialog.reject()

    def test_valid_preview_is_read_only_displays_typed_metadata_and_enables_restore_actions(self) -> None:
        target = self._new_context("preview-target")
        dialog = self._dialog(context=target)
        try:
            dialog.restore_archive_edit.setText(str(self.archive_path))
            dialog.preview_button.click()
            self.application.processEvents()
            self.assertIsNotNone(dialog._preview)
            self.assertTrue(dialog._preview.valid)  # type: ignore[union-attr]
            self.assertEqual(dialog._archive_fingerprint, self.context.archives.fingerprint(self.archive_path))
            self.assertTrue(dialog.merge_button.isEnabled())
            self.assertTrue(dialog.replace_button.isEnabled())
            details = dialog.archive_details_label.text()
            self.assertIn("Archive format:", details)
            self.assertIn("Actual counts:", details)
            self.assertIn("Attachment metadata records:", details)
            self.assertIn(ATTACHMENT_WORDING, details)
            self.assertEqual(
                target.benchmarks.runs.list(),
                [],
            )
            self.assertFalse(target.settings.path.exists())
        finally:
            dialog.reject()

    def test_invalid_preview_and_path_change_clear_stale_state_and_disable_restore(self) -> None:
        invalid = self.root / "invalid.json"
        invalid.write_text("{", encoding="utf-8")
        dialog = self._dialog()
        try:
            dialog.restore_archive_edit.setText(str(invalid))
            dialog.preview_button.click()
            self.assertIsNotNone(dialog._preview)
            self.assertFalse(dialog._preview.valid)  # type: ignore[union-attr]
            self.assertFalse(dialog.merge_button.isEnabled())
            self.assertFalse(dialog.replace_button.isEnabled())
            self.assertIn("malformed_json", dialog.error_label.text())

            dialog.restore_archive_edit.setText(str(self.archive_path))
            self.assertIsNone(dialog._preview)
            self.assertIsNone(dialog._archive_fingerprint)
            self.assertFalse(dialog.merge_button.isEnabled())
            self.assertFalse(dialog.replace_button.isEnabled())
            self.assertIn("Preview again", dialog.status_label.text())
        finally:
            dialog.reject()

    def test_unsupported_preview_is_displayed_without_enabling_restore(self) -> None:
        unsupported = self.root / "unsupported.json"
        document = json.loads(self.archive_path.read_text(encoding="utf-8"))
        document["archive_version"] = 999
        unsupported.write_text(json.dumps(document), encoding="utf-8")
        dialog = self._dialog()
        try:
            dialog.restore_archive_edit.setText(str(unsupported))
            dialog.preview_button.click()
            self.assertFalse(dialog.merge_button.isEnabled())
            self.assertFalse(dialog.replace_button.isEnabled())
            self.assertIn("unsupported_archive_version", dialog.error_label.text())
        finally:
            dialog.reject()

    def test_merge_handler_is_transactional_duplicate_safe_and_preserves_snapshots(self) -> None:
        target = self._new_context("merge-target")
        dialog = self._dialog(context=target, confirm_merge=lambda: True)
        completed: list[str] = []
        dialog.restore_succeeded.connect(completed.append)
        original_snapshot = dict(self.source_run.model_snapshot)
        try:
            dialog.restore_archive_edit.setText(str(self.archive_path))
            dialog.preview_button.click()
            dialog.merge_button.click()
            self.assertEqual(completed, ["merge"])
            self.assertIn("Created:", dialog.restore_result_label.text())
            self.assertFalse(dialog.merge_button.isEnabled())
            restored = target.benchmarks.runs.list()
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0].model_snapshot, original_snapshot)
            self.assertEqual(self.source_run.model_snapshot, original_snapshot)

            duplicate = self._dialog(context=target, confirm_merge=lambda: True)
            try:
                duplicate.restore_archive_edit.setText(str(self.archive_path))
                duplicate.preview_button.click()
                duplicate.merge_button.click()
                self.assertIn("Skipped:", duplicate.restore_result_label.text())
                self.assertEqual(len(target.benchmarks.runs.list()), 1)
            finally:
                duplicate.reject()
        finally:
            dialog.reject()

    def test_merge_cancellation_and_stale_archive_failure_write_nothing(self) -> None:
        target = self._new_context("stale-target")
        cancelled = self._dialog(context=target, confirm_merge=lambda: False)
        try:
            cancelled.restore_archive_edit.setText(str(self.archive_path))
            cancelled.preview_button.click()
            before = len(target.benchmarks.runs.list())
            with patch.object(target.archives, "merge_file") as merge:
                cancelled.merge_button.click()
            merge.assert_not_called()
            self.assertEqual(len(target.benchmarks.runs.list()), before)
        finally:
            cancelled.reject()

        stale = self._dialog(context=target, confirm_merge=lambda: True)
        try:
            stale.restore_archive_edit.setText(str(self.archive_path))
            stale.preview_button.click()
            self.archive_path.write_bytes(self.archive_path.read_bytes() + b"\n")
            stale.merge_button.click()
            self.assertIn("stale_archive", stale.status_label.text())
            self.assertIsNone(stale._archive_fingerprint)
            self.assertFalse(stale.merge_button.isEnabled())
            self.assertEqual(len(target.benchmarks.runs.list()), 0)
        finally:
            stale.reject()

    def test_replace_handler_shows_safety_backup_recovery_and_restart_state(self) -> None:
        target = self._new_context("replace-target")
        target.benchmarks.save_run(
            BenchmarkRun(raw_model_output="Old database", model_snapshot={"model_name": "Old"}),
            ReviewScore(run_id=1, overall_score=2.0),
        )
        dialog = self._dialog(context=target, confirm_replace=lambda: True)
        completed: list[str] = []
        dialog.restore_succeeded.connect(completed.append)
        try:
            dialog.restore_archive_edit.setText(str(self.archive_path))
            dialog.preview_button.click()
            dialog.replace_button.click()
            self.assertEqual(completed, ["replace"])
            result = dialog.restore_result_label.text()
            self.assertIn("Safety backup:", result)
            self.assertIn("Recovery status:", result)
            self.assertIn("Restart required:", result)
            safety_text = next(line for line in result.splitlines() if line.startswith("Safety backup:"))
            self.assertTrue(Path(safety_text.split(":", 1)[1].strip()).exists())
            restored = target.benchmarks.runs.list()
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0].model_snapshot, self.source_run.model_snapshot)
            self.assertFalse(dialog.replace_button.isEnabled())
        finally:
            dialog.reject()

    def test_replace_failure_preserves_controls_as_safe_disabled_state_without_retrying(self) -> None:
        target = self._new_context("replace-failure-target")
        dialog = self._dialog(context=target, confirm_replace=lambda: True)
        try:
            dialog.restore_archive_edit.setText(str(self.archive_path))
            dialog.preview_button.click()
            failure = ArchiveError("replacement failed", code=ArchiveIssueCode.REOPEN_FAILED)
            with patch.object(target.archives, "replace_file", side_effect=failure) as replace:
                dialog.replace_button.click()
            replace.assert_called_once()
            self.assertFalse(dialog._busy)
            self.assertFalse(dialog.replace_button.isEnabled())
            self.assertIn("reopen_failed", dialog.status_label.text())
            self.assertNotIn("Traceback", dialog.status_label.text())
        finally:
            dialog.reject()

    def test_successful_restore_signal_refreshes_loaded_pages_after_operation(self) -> None:
        window = MainWindow(self.context)
        real_dialog = BackupRestoreDialog(self.context, window)
        try:
            for page in (
                window.dashboard,
                window.runs,
                window.sessions,
                window.models,
                window.benchmarks,
                window.prompt_templates,
                window.hardware_profiles,
            ):
                page._has_loaded = True  # type: ignore[attr-defined]
            def emit_success() -> QDialog.DialogCode:
                real_dialog.restore_succeeded.emit("merge")
                return QDialog.DialogCode.Rejected

            with patch.object(window.dashboard, "refresh") as dashboard_refresh, patch.object(
                window.runs, "refresh"
            ) as runs_refresh, patch.object(window.sessions, "refresh") as sessions_refresh, patch.object(
                window.models, "refresh"
            ) as models_refresh, patch.object(window.benchmarks, "refresh") as benchmarks_refresh, patch.object(
                window.prompt_templates, "refresh"
            ) as prompts_refresh, patch.object(window.hardware_profiles, "refresh") as hardware_refresh, patch.object(
                window.dataset_builder, "refresh_catalog_choices"
            ) as dataset_refresh, patch.object(real_dialog, "exec", side_effect=emit_success), patch(
                "gui.main_window.BackupRestoreDialog", return_value=real_dialog
            ):
                window.open_backup_restore()
            dashboard_refresh.assert_called_once_with()
            runs_refresh.assert_called_once_with()
            sessions_refresh.assert_called_once_with()
            models_refresh.assert_called_once_with()
            benchmarks_refresh.assert_called_once_with()
            prompts_refresh.assert_called_once_with()
            hardware_refresh.assert_called_once_with()
            dataset_refresh.assert_called_once_with()
        finally:
            real_dialog.reject()
            window.close()


if __name__ == "__main__":
    unittest.main()
