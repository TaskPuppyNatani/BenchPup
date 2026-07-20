from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QPlainTextEdit, QPushButton

from engine.domain import BenchmarkRun, RunAttachment
from engine.settings import AttachmentPreferences
from gui.context import GuiApplicationContext
from gui.dialogs.attachment_editor import (
    MANAGED_MODE,
    REFERENCE_MODE,
    AttachmentEditorDialog,
)
from gui.views.run_details import RunDetailsDialog


class _SavingEditor:
    def __init__(self, editor: AttachmentEditorDialog) -> None:
        self.editor = editor

    def exec(self) -> QDialog.DialogCode:
        return QDialog.DialogCode.Accepted if self.editor._save() else QDialog.DialogCode.Rejected


class Phase5C4BAttachmentGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-phase5c4b-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.context = GuiApplicationContext.create(database_path=self.root / "data" / "benchmark.db")

    def tearDown(self) -> None:
        self.context.close()
        self.directory.cleanup()

    def _save_run(self) -> BenchmarkRun:
        run, _ = self.context.benchmarks.save_run(
            BenchmarkRun(
                raw_model_output="output",
                prompt_text="prompt",
                model_snapshot={"model_name": "Snapshot model"},
                benchmark_snapshot={"name": "Snapshot benchmark"},
                prompt_snapshot={"name": "Snapshot prompt"},
                hardware_snapshot={"name": "Snapshot hardware"},
            )
        )
        return run

    def _add_reference_attachment(self, run: BenchmarkRun, path: Path) -> RunAttachment:
        return self.context.benchmarks.add_attachment(
            RunAttachment(
                run_id=run.id or 0,
                attachment_type="log",
                file_path=str(path),
                original_filename=path.name,
                notes="initial",
            )
        )

    @staticmethod
    def _attachments_tab_index(dialog: RunDetailsDialog) -> int:
        return next(index for index in range(dialog.tabs.count()) if dialog.tabs.tabText(index) == "Attachments")

    def test_reference_mode_stores_path_without_copying(self) -> None:
        run = self._save_run()
        source = self.root / "source" / "result.log"
        source.parent.mkdir()
        source.write_text("reference content", encoding="utf-8")
        destination = self.root / "managed"
        destination.mkdir()

        editor = AttachmentEditorDialog(self.context, run.id or 0, confirm_close=lambda: True)
        editor.source_file_edit.setText(str(source))
        editor.filename_edit.setText(source.name)
        editor.storage_mode_combo.setCurrentIndex(editor.storage_mode_combo.findData(REFERENCE_MODE))
        editor.attachment_type_combo.setCurrentIndex(editor.attachment_type_combo.findData("log"))
        editor.notes_edit.setPlainText("Reference only")

        self.assertTrue(editor._save())
        self.assertEqual(editor.result(), QDialog.DialogCode.Accepted)
        _, _, attachments = self.context.benchmarks.get_run(run.id or 0)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].file_path, str(source))
        self.assertFalse((destination / source.name).exists())
        preferences = self.context.settings.get_attachment_preferences()
        self.assertEqual(preferences.source_directory, source.parent)
        self.assertEqual(preferences.storage_mode, REFERENCE_MODE)

    def test_managed_copy_preserves_source_and_original_filename(self) -> None:
        run = self._save_run()
        source = self.root / "source" / "capture.txt"
        source.parent.mkdir()
        source.write_text("managed content", encoding="utf-8")
        destination = self.root / "managed"
        destination.mkdir()

        editor = AttachmentEditorDialog(self.context, run.id or 0, confirm_close=lambda: True)
        editor.source_file_edit.setText(str(source))
        editor.filename_edit.setText(source.name)
        editor.destination_folder_edit.setText(str(destination))
        editor.storage_mode_combo.setCurrentIndex(editor.storage_mode_combo.findData(MANAGED_MODE))
        self.assertTrue(editor._save())

        copied = destination / source.name
        self.assertTrue(source.is_file())
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_text(encoding="utf-8"), "managed content")
        _, _, attachments = self.context.benchmarks.get_run(run.id or 0)
        self.assertEqual(attachments[0].file_path, str(copied))
        self.assertEqual(attachments[0].original_filename, source.name)
        preferences = self.context.settings.get_attachment_preferences()
        self.assertEqual(preferences.destination_directory, destination)
        self.assertEqual(preferences.storage_mode, MANAGED_MODE)

    def test_relative_paths_are_absolute_and_survive_working_directory_change(self) -> None:
        run = self._save_run()
        source_directory = self.root / "source"
        source_directory.mkdir()
        reference_source = source_directory / "reference.log"
        reference_source.write_text("reference", encoding="utf-8")
        managed_source = source_directory / "managed.log"
        managed_source.write_text("managed", encoding="utf-8")
        managed_directory = self.root / "managed"
        managed_directory.mkdir()
        other_directory = self.root / "other"
        other_directory.mkdir()

        original_cwd = Path.cwd()
        try:
            os.chdir(self.root)

            reference_editor = AttachmentEditorDialog(self.context, run.id or 0, confirm_close=lambda: True)
            reference_editor.source_file_edit.setText("source/reference.log")
            reference_editor.storage_mode_combo.setCurrentIndex(reference_editor.storage_mode_combo.findData(REFERENCE_MODE))
            self.assertTrue(reference_editor._save())

            managed_editor = AttachmentEditorDialog(self.context, run.id or 0, confirm_close=lambda: True)
            managed_editor.source_file_edit.setText("source/managed.log")
            managed_editor.destination_folder_edit.setText("managed")
            managed_editor.storage_mode_combo.setCurrentIndex(managed_editor.storage_mode_combo.findData(MANAGED_MODE))
            self.assertTrue(managed_editor._save())

            _, _, attachments = self.context.benchmarks.get_run(run.id or 0)
            self.assertEqual(len(attachments), 2)
            self.assertEqual({item.original_filename for item in attachments}, {"reference.log", "managed.log"})
            self.assertTrue(all(Path(item.file_path).is_absolute() for item in attachments))

            os.chdir(other_directory)
            self.assertTrue(all(Path(item.file_path).is_file() for item in attachments))
        finally:
            os.chdir(original_cwd)

    def test_managed_copy_rejects_existing_destination_without_overwrite(self) -> None:
        run = self._save_run()
        source = self.root / "source" / "duplicate.txt"
        source.parent.mkdir()
        source.write_text("new content", encoding="utf-8")
        destination = self.root / "managed"
        destination.mkdir()
        existing = destination / source.name
        existing.write_text("keep this content", encoding="utf-8")

        editor = AttachmentEditorDialog(self.context, run.id or 0, confirm_close=lambda: True)
        editor.source_file_edit.setText(str(source))
        editor.filename_edit.setText(source.name)
        editor.destination_folder_edit.setText(str(destination))
        editor.storage_mode_combo.setCurrentIndex(editor.storage_mode_combo.findData(MANAGED_MODE))
        with patch("gui.dialogs.attachment_editor.QMessageBox.warning") as warning:
            self.assertFalse(editor._save())
        self.assertTrue(warning.called)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep this content")
        self.assertEqual(self.context.benchmarks.get_run(run.id or 0)[2], [])

    def test_missing_managed_destination_is_friendly_and_writes_no_record(self) -> None:
        run = self._save_run()
        source = self.root / "source" / "missing-destination.txt"
        source.parent.mkdir()
        source.write_text("source", encoding="utf-8")
        missing_destination = self.root / "does-not-exist"

        editor = AttachmentEditorDialog(self.context, run.id or 0, confirm_close=lambda: True)
        editor.source_file_edit.setText(str(source))
        editor.filename_edit.setText(source.name)
        editor.destination_folder_edit.setText(str(missing_destination))
        editor.storage_mode_combo.setCurrentIndex(editor.storage_mode_combo.findData(MANAGED_MODE))
        with patch("gui.dialogs.attachment_editor.QMessageBox.warning") as warning:
            self.assertFalse(editor._save())
        self.assertTrue(warning.called)
        self.assertEqual(self.context.benchmarks.get_run(run.id or 0)[2], [])

    def test_failed_managed_copy_cleans_partial_file_and_writes_no_record(self) -> None:
        run = self._save_run()
        source = self.root / "source" / "copy-failure.txt"
        source.parent.mkdir()
        source.write_text("source", encoding="utf-8")
        destination = self.root / "managed"
        destination.mkdir()

        editor = AttachmentEditorDialog(self.context, run.id or 0, confirm_close=lambda: True)
        editor.source_file_edit.setText(str(source))
        editor.filename_edit.setText(source.name)
        editor.destination_folder_edit.setText(str(destination))
        editor.storage_mode_combo.setCurrentIndex(editor.storage_mode_combo.findData(MANAGED_MODE))
        with patch("engine.services.shutil.copyfileobj", side_effect=OSError("copy failed")), patch(
            "gui.dialogs.attachment_editor.QMessageBox.warning"
        ) as warning:
            self.assertFalse(editor._save())
        self.assertTrue(warning.called)
        self.assertFalse((destination / source.name).exists())
        self.assertEqual(self.context.benchmarks.get_run(run.id or 0)[2], [])

    def test_invalid_remembered_storage_mode_falls_back_to_reference(self) -> None:
        run = self._save_run()
        self.context.settings.set_attachment_preferences(AttachmentPreferences(storage_mode="legacy-managed"))

        editor = AttachmentEditorDialog(self.context, run.id or 0, confirm_close=lambda: True)

        self.assertEqual(editor.storage_mode_combo.currentData(), REFERENCE_MODE)
        editor.reject()

    def test_metadata_edit_does_not_copy_or_move_file(self) -> None:
        run = self._save_run()
        source = self.root / "source" / "metadata.log"
        source.parent.mkdir()
        source.write_text("unchanged", encoding="utf-8")
        attachment = self._add_reference_attachment(run, source)
        managed = self.root / "managed"
        managed.mkdir()
        self.context.settings.set_attachment_preferences(AttachmentPreferences(storage_mode=MANAGED_MODE))

        editor = AttachmentEditorDialog(self.context, run.id or 0, attachment, confirm_close=lambda: True)
        editor.notes_edit.setPlainText("edited metadata")
        self.assertEqual(editor.storage_mode_combo.currentData(), REFERENCE_MODE)
        self.assertEqual(self.context.settings.get_attachment_preferences().storage_mode, MANAGED_MODE)
        self.assertTrue(editor._save())

        _, _, attachments = self.context.benchmarks.get_run(run.id or 0)
        self.assertEqual(attachments[0].file_path, str(source))
        self.assertEqual(attachments[0].notes, "edited metadata")
        self.assertEqual(source.read_text(encoding="utf-8"), "unchanged")
        self.assertFalse((managed / source.name).exists())
        self.assertEqual(self.context.settings.get_attachment_preferences().storage_mode, MANAGED_MODE)

    def test_missing_file_is_obvious_and_metadata_edit_remains_possible(self) -> None:
        run = self._save_run()
        missing = self.root / "missing" / "gone.log"
        attachment = self._add_reference_attachment(run, missing)
        dialog = RunDetailsDialog(self.context, run.id or 0)

        self.assertTrue(any(label.text() == "File Missing" for label in dialog.findChildren(QLabel)))
        editor = AttachmentEditorDialog(self.context, run.id or 0, attachment, confirm_close=lambda: True)
        editor.notes_edit.setPlainText("metadata for missing file")
        self.assertTrue(editor._save())
        _, _, attachments = self.context.benchmarks.get_run(run.id or 0)
        self.assertEqual(attachments[0].file_path, str(missing))
        self.assertEqual(attachments[0].notes, "metadata for missing file")

    def test_missing_file_open_actions_show_friendly_error(self) -> None:
        run = self._save_run()
        attachment = self._add_reference_attachment(run, self.root / "not-there.log")
        dialog = RunDetailsDialog(self.context, run.id or 0)
        with patch("gui.views.run_details.QMessageBox.warning") as warning:
            dialog.open_attachment_file(attachment)
            dialog.open_attachment_folder(attachment)
        self.assertEqual(warning.call_count, 2)
        self.assertIn("missing or inaccessible", warning.call_args_list[0].args[2])
        self.assertIn("operating system could not open", warning.call_args_list[1].args[2])

    def test_remove_attachment_removes_only_record_and_requires_confirmation(self) -> None:
        run = self._save_run()
        source = self.root / "remove-me.log"
        source.write_text("keep on disk", encoding="utf-8")
        attachment = self._add_reference_attachment(run, source)
        dialog = RunDetailsDialog(self.context, run.id or 0)

        with patch(
            "gui.views.run_details.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ) as question:
            dialog.remove_attachment(attachment)
        self.assertIn("The file on disk will NOT be deleted.", question.call_args.args[2])
        self.assertTrue(source.is_file())
        self.assertEqual(self.context.benchmarks.get_run(run.id or 0)[2], [])

    def test_cancelled_editor_writes_no_record_file_or_preferences(self) -> None:
        run = self._save_run()
        source = self.root / "source" / "cancel.txt"
        source.parent.mkdir()
        source.write_text("cancel", encoding="utf-8")
        destination = self.root / "managed"
        destination.mkdir()
        editor = AttachmentEditorDialog(self.context, run.id or 0, confirm_close=lambda: True)
        editor.source_file_edit.setText(str(source))
        editor.destination_folder_edit.setText(str(destination))
        editor.storage_mode_combo.setCurrentIndex(editor.storage_mode_combo.findData(MANAGED_MODE))

        editor.reject()

        self.assertEqual(editor.result(), QDialog.DialogCode.Rejected)
        self.assertEqual(self.context.benchmarks.get_run(run.id or 0)[2], [])
        self.assertFalse((destination / source.name).exists())
        preferences = self.context.settings.get_attachment_preferences()
        self.assertIsNone(preferences.source_directory)
        self.assertIsNone(preferences.destination_directory)
        self.assertEqual(preferences.storage_mode, REFERENCE_MODE)

    def test_run_details_add_handler_saves_and_preserves_attachments_tab(self) -> None:
        run = self._save_run()
        source = self.root / "handler-add.log"
        source.write_text("handler add", encoding="utf-8")
        dialog = RunDetailsDialog(self.context, run.id or 0)
        dialog.tabs.setCurrentIndex(self._attachments_tab_index(dialog))

        editor = AttachmentEditorDialog(self.context, run.id or 0, parent=dialog, confirm_close=lambda: True)
        editor.source_file_edit.setText(str(source))
        editor.filename_edit.clear()
        editor.storage_mode_combo.setCurrentIndex(editor.storage_mode_combo.findData(REFERENCE_MODE))
        with patch("gui.views.run_details.AttachmentEditorDialog", return_value=_SavingEditor(editor)):
            dialog.add_attachment()

        self.assertEqual(len(dialog.aggregate.attachments), 1)
        self.assertEqual(dialog.aggregate.attachments[0].original_filename, source.name)
        self.assertEqual(dialog.tabs.tabText(dialog.tabs.currentIndex()), "Attachments")

    def test_run_details_edit_and_remove_handlers_refresh_and_preserve_file(self) -> None:
        run = self._save_run()
        source = self.root / "handler-edit-remove.log"
        source.write_text("keep on disk", encoding="utf-8")
        attachment = self._add_reference_attachment(run, source)
        dialog = RunDetailsDialog(self.context, run.id or 0)
        dialog.tabs.setCurrentIndex(self._attachments_tab_index(dialog))

        edit_editor = AttachmentEditorDialog(self.context, run.id or 0, attachment, parent=dialog, confirm_close=lambda: True)
        edit_editor.notes_edit.setPlainText("updated through handler")
        with patch("gui.views.run_details.AttachmentEditorDialog", return_value=_SavingEditor(edit_editor)):
            dialog.edit_attachment(attachment)

        self.assertEqual(dialog.aggregate.attachments[0].notes, "updated through handler")
        self.assertTrue(any("updated through handler" in field.toPlainText() for field in dialog.tabs.findChildren(QPlainTextEdit)))
        self.assertEqual(dialog.tabs.tabText(dialog.tabs.currentIndex()), "Attachments")

        with patch(
            "gui.views.run_details.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            dialog.remove_attachment(dialog.aggregate.attachments[0])

        self.assertEqual(dialog.aggregate.attachments, ())
        self.assertTrue(source.is_file())
        self.assertEqual(dialog.tabs.tabText(dialog.tabs.currentIndex()), "Attachments")

    def test_run_details_actions_and_attachment_edits_preserve_snapshots(self) -> None:
        run = self._save_run()
        before = {
            "model": copy.deepcopy(run.model_snapshot),
            "benchmark": copy.deepcopy(run.benchmark_snapshot),
            "prompt": copy.deepcopy(run.prompt_snapshot),
            "hardware": copy.deepcopy(run.hardware_snapshot),
        }
        source = self.root / "details.log"
        source.write_text("details", encoding="utf-8")
        attachment = self._add_reference_attachment(run, source)
        dialog = RunDetailsDialog(self.context, run.id or 0)

        dialog.tabs.setCurrentIndex(self._attachments_tab_index(dialog))
        buttons = {button.text() for button in dialog.findChildren(QPushButton)}
        self.assertTrue({"Add Attachment", "Edit Attachment", "Remove Attachment", "Open File", "Open Containing Folder"} <= buttons)

        editor = AttachmentEditorDialog(self.context, run.id or 0, attachment, parent=dialog, confirm_close=lambda: True)
        editor.notes_edit.setPlainText("changed")
        with patch("gui.views.run_details.AttachmentEditorDialog", return_value=_SavingEditor(editor)):
            dialog.edit_attachment(attachment)
        self.assertTrue(any("changed" in field.toPlainText() for field in dialog.tabs.findChildren(QPlainTextEdit)))
        self.assertEqual(dialog.tabs.tabText(dialog.tabs.currentIndex()), "Attachments")

        after, _, _ = self.context.benchmarks.get_run(run.id or 0)
        assert after is not None
        self.assertEqual(after.model_snapshot, before["model"])
        self.assertEqual(after.benchmark_snapshot, before["benchmark"])
        self.assertEqual(after.prompt_snapshot, before["prompt"])
        self.assertEqual(after.hardware_snapshot, before["hardware"])


if __name__ == "__main__":
    unittest.main()
