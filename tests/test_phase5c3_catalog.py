from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QWizard

from engine.domain import BENCHMARK_TYPES, BenchmarkDefinition, BenchmarkRun, PromptTemplate, resolve_prompt_text
from gui.context import GuiApplicationContext
from gui.dialogs.benchmark_editor import BenchmarkEditorDialog
from gui.main_window import MainWindow
from gui.views.add_run import AddRunWizard
from gui.views.benchmarks import BENCHMARK_HEADERS, BenchmarksView


class Phase5C3GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication(["benchpup-phase5c3-tests"])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.context = GuiApplicationContext.create(
            database_path=Path(self.directory.name) / "data" / "benchmark.db"
        )
        self.widgets: list[object] = []

    def tearDown(self) -> None:
        for widget in self.widgets:
            widget.close()  # type: ignore[attr-defined]
        self.context.close()
        self.directory.cleanup()

    def _save_definition(
        self,
        *,
        name: str = "Review benchmark",
        file_path: str = "review.py",
        benchmark_type: str = "code_review",
        default_prompt: str = "",
        tags: str = "",
        is_active: bool = True,
    ) -> BenchmarkDefinition:
        dialog = BenchmarkEditorDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(dialog)
        dialog.name_edit.setText(name)
        dialog.file_path_edit.setText(file_path)
        dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(benchmark_type))
        dialog.default_prompt_edit.setPlainText(default_prompt)
        dialog.tags_edit.setText(tags)
        dialog.active_checkbox.setChecked(is_active)
        self.assertTrue(dialog._save(), dialog.validation_summary.text())
        self.assertIsNotNone(dialog.saved_record)
        saved = self.context.catalog.get_benchmark_definition(dialog.saved_record.id)
        self.assertIsNotNone(saved)
        assert saved is not None
        return saved

    def _finish(self, wizard: AddRunWizard) -> None:
        wizard.show()
        self.application.processEvents()
        while wizard.currentId() < 3:
            wizard.next()
            self.application.processEvents()
        finish = wizard.button(QWizard.WizardButton.FinishButton)
        assert finish is not None
        finish.click()
        self.application.processEvents()

    def test_benchmark_catalog_empty_state_columns_and_typed_service_listing(self) -> None:
        page = BenchmarksView(self.context)
        self.widgets.append(page)
        self.assertEqual(page.rows, ())
        self.assertFalse(page.empty_state.isHidden())
        self.assertFalse(page.edit_button.isEnabled())
        self.assertFalse(page.lifecycle_button.isEnabled())
        self.assertEqual(
            tuple(page.catalog_table.model().headerData(index, Qt.Orientation.Horizontal) for index in range(len(BENCHMARK_HEADERS))),
            BENCHMARK_HEADERS,
        )

        definition = self._save_definition(name="Listed benchmark", tags="one,two")
        with patch.object(
            self.context.catalog,
            "list_benchmark_definitions",
            wraps=self.context.catalog.list_benchmark_definitions,
        ) as list_definitions:
            page.refresh()
        list_definitions.assert_called_once_with(include_inactive=True)
        self.assertEqual(page.rows[0].record.id, definition.id)
        self.assertEqual(page.rows[0].values[:5], ("Listed benchmark", "Code review", "review.py", "one,two", "Active"))

    def test_benchmark_editor_round_trips_all_fields_without_gui_normalization(self) -> None:
        exact_path = r"C:\fixtures\..\review file.py"
        exact_prompt = "Review exactly.\n\n  Preserve indentation.\n"
        for index, tags in enumerate(("", "one", "one,two", " one,one , two ")):
            with self.subTest(tags=tags):
                saved = self._save_definition(
                    name=f"Definition {index}",
                    file_path=exact_path,
                    benchmark_type=BENCHMARK_TYPES[index],
                    default_prompt=exact_prompt,
                    tags=tags,
                )
                self.assertEqual(saved.file_path, exact_path)
                self.assertEqual(saved.default_prompt, exact_prompt)
                self.assertEqual(saved.tags, tags)
                self.assertEqual(saved.benchmark_type, BENCHMARK_TYPES[index])

    def test_benchmark_editor_uses_one_authoritative_type_source_and_keeps_failed_edit_open(self) -> None:
        dialog = BenchmarkEditorDialog(self.context, confirm_close=lambda: True)
        self.widgets.append(dialog)
        self.assertEqual(
            [dialog.type_combo.itemData(index) for index in range(dialog.type_combo.count())],
            list(BENCHMARK_TYPES),
        )
        dialog.name_edit.setText("Invalid type")
        dialog.file_path_edit.setText("target.txt")
        dialog.type_combo.setCurrentIndex(-1)
        dialog.default_prompt_edit.setPlainText("Keep this text")
        self.assertFalse(dialog._save())
        self.assertIn("invalid benchmark_type", dialog.validation_summary.text())
        self.assertEqual(dialog.default_prompt_edit.toPlainText(), "Keep this text")
        self.assertEqual(self.context.catalog.list_benchmark_definitions(include_inactive=True), [])

    def test_benchmark_catalog_save_selects_created_and_edited_records_and_refresh_preserves_selection(self) -> None:
        first = self._save_definition(name="Zulu")
        second = self._save_definition(name="Alpha")
        page = BenchmarksView(self.context, confirm_action=lambda _title, _message: True)
        self.widgets.append(page)
        self.assertTrue(page.select_record(first.id))

        third = self._save_definition(name="Middle")
        page.refresh()
        self.assertEqual(page.selected_record().id, first.id)  # type: ignore[union-attr]

        with patch("gui.views.benchmarks.BenchmarkEditorDialog") as dialog_type:
            dialog_type.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog_type.return_value.saved_record = third
            page.add_record()
        self.assertEqual(page.selected_record().id, third.id)  # type: ignore[union-attr]

        edited = self.context.catalog.update_benchmark_definition(
            BenchmarkDefinition(
                name="Edited Alpha",
                file_path=second.file_path,
                benchmark_type=second.benchmark_type,
                id=second.id,
                default_prompt=second.default_prompt,
                tags=second.tags,
                is_active=second.is_active,
                created_at=second.created_at,
                updated_at=second.updated_at,
            )
        )
        with patch("gui.views.benchmarks.BenchmarkEditorDialog") as dialog_type:
            dialog_type.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog_type.return_value.saved_record = edited
            page.open_editor(second)
        self.assertEqual(page.selected_record().id, second.id)  # type: ignore[union-attr]

    def test_benchmark_lifecycle_has_no_delete_action_and_keeps_snapshot_readable(self) -> None:
        definition = self._save_definition(
            name="Historical benchmark",
            file_path="before.py",
            default_prompt="before",
            tags="old",
        )
        run, _ = self.context.benchmarks.save_run(
            BenchmarkRun(raw_model_output="captured", benchmark_definition_id=definition.id)
        )
        original_snapshot = dict(run.benchmark_snapshot)

        editor = BenchmarkEditorDialog(self.context, definition, confirm_close=lambda: True)
        self.widgets.append(editor)
        editor.file_path_edit.setText("after.py")
        editor.default_prompt_edit.setPlainText("after")
        editor.tags_edit.setText("new")
        self.assertTrue(editor._save(), editor.validation_summary.text())

        page = BenchmarksView(self.context, confirm_action=lambda _title, _message: True)
        self.widgets.append(page)
        page.refresh()
        self.assertFalse(hasattr(page, "delete_button"))
        self.assertTrue(page.select_record(definition.id))
        page.apply_lifecycle(page.selected_record())
        self.assertFalse(self.context.catalog.get_benchmark_definition(definition.id).is_active)  # type: ignore[union-attr]

        stored, _, _ = self.context.benchmarks.get_run(run.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.benchmark_snapshot, original_snapshot)
        self.assertEqual(stored.benchmark_snapshot["file_path"], "before.py")

    def test_add_run_exposes_definition_context_and_keeps_active_selection_rules(self) -> None:
        definition = self._save_definition(
            name="GUI benchmark",
            file_path="benchmarks/gui.py",
            benchmark_type="revision",
            default_prompt="Use the default only when no prompt source is selected.",
        )
        inactive = self._save_definition(name="Hidden benchmark", is_active=False)
        wizard = AddRunWizard(self.context, confirm_close=lambda: True)
        self.widgets.append(wizard)
        self.assertGreaterEqual(wizard.benchmark_combo.findData(definition.id), 0)
        self.assertEqual(wizard.benchmark_combo.findData(inactive.id), -1)

        wizard.benchmark_combo.setCurrentIndex(wizard.benchmark_combo.findData(definition.id))
        self.assertEqual(wizard.benchmark_file_value.text(), "benchmarks/gui.py")
        self.assertEqual(wizard.benchmark_type_value.text(), "revision")
        self.assertEqual(
            wizard.benchmark_default_prompt_value.toPlainText(),
            "Use the default only when no prompt source is selected.",
        )
        self.assertEqual(wizard.prompt_text_edit.toPlainText(), "")
        self.assertIn("default prompt", wizard.prompt_resolution_hint.text())

    def test_add_run_prompt_precedence_is_explicit_template_then_definition_default(self) -> None:
        definition = self._save_definition(
            name="Prompt precedence benchmark",
            default_prompt="Definition default",
        )
        template_text = "Selected template\n  exact"
        template = self.context.catalog.create_prompt_template(
            PromptTemplate(
                name="Selected template",
                version="1",
                prompt_text=template_text,
                prompt_hash="",
                benchmark_type="code_review",
            )
        )

        default_wizard = AddRunWizard(self.context, confirm_close=lambda: True)
        self.widgets.append(default_wizard)
        default_wizard.benchmark_combo.setCurrentIndex(default_wizard.benchmark_combo.findData(definition.id))
        default_wizard.raw_output_edit.setPlainText("default output")
        self.assertEqual(default_wizard._effective_prompt_text(default_wizard._build_run()), "Definition default")
        self._finish(default_wizard)
        default_run = default_wizard.saved_run
        self.assertIsNotNone(default_run)
        assert default_run is not None
        self.assertEqual(default_run.prompt_text, "Definition default")
        self.assertEqual(default_run.prompt_snapshot, {})

        template_wizard = AddRunWizard(self.context, confirm_close=lambda: True)
        self.widgets.append(template_wizard)
        template_wizard.benchmark_combo.setCurrentIndex(template_wizard.benchmark_combo.findData(definition.id))
        template_wizard.prompt_template_combo.setCurrentIndex(template_wizard.prompt_template_combo.findData(template.id))
        self.assertEqual(template_wizard.prompt_text_edit.toPlainText(), "")
        self.assertEqual(template_wizard._effective_prompt_text(template_wizard._build_run()), template_text)
        template_wizard.raw_output_edit.setPlainText("template output")
        self._finish(template_wizard)
        template_run = template_wizard.saved_run
        self.assertIsNotNone(template_run)
        assert template_run is not None
        self.assertEqual(template_run.prompt_text, template_text)
        self.assertEqual(template_run.prompt_snapshot["prompt_text"], template_text)

        explicit_wizard = AddRunWizard(self.context, confirm_close=lambda: True)
        self.widgets.append(explicit_wizard)
        explicit_wizard.prompt_text_edit.setPlainText("Explicit run prompt")
        explicit_wizard.benchmark_combo.setCurrentIndex(explicit_wizard.benchmark_combo.findData(definition.id))
        explicit_wizard.prompt_template_combo.setCurrentIndex(explicit_wizard.prompt_template_combo.findData(template.id))
        self.assertEqual(explicit_wizard.prompt_text_edit.toPlainText(), "Explicit run prompt")
        self.assertEqual(explicit_wizard._effective_prompt_text(explicit_wizard._build_run()), "Explicit run prompt")

    def test_add_run_definition_selection_does_not_overwrite_explicit_prompt_and_review_shows_effective_text(self) -> None:
        definition = self._save_definition(default_prompt="Definition suggestion")
        template = self.context.catalog.create_prompt_template(
            PromptTemplate(
                name="Template",
                version="1",
                prompt_text="Template suggestion",
                prompt_hash="",
                benchmark_type="code_review",
            )
        )
        wizard = AddRunWizard(self.context, confirm_close=lambda: True)
        self.widgets.append(wizard)
        wizard.prompt_text_edit.setPlainText("Explicit user value")
        wizard.benchmark_combo.setCurrentIndex(wizard.benchmark_combo.findData(definition.id))
        wizard.prompt_template_combo.setCurrentIndex(wizard.prompt_template_combo.findData(template.id))
        wizard._update_review_summary()
        self.assertEqual(wizard.prompt_text_edit.toPlainText(), "Explicit user value")
        self.assertEqual(wizard.review_prompt_text.toPlainText(), "Explicit user value")

    def test_prompt_resolution_helper_preserves_whitespace_and_uses_authoritative_order(self) -> None:
        self.assertEqual(resolve_prompt_text("explicit", "template", "default"), "explicit")
        self.assertEqual(resolve_prompt_text("", "template", "default"), "template")
        self.assertEqual(resolve_prompt_text("", "", "default"), "default")
        self.assertEqual(resolve_prompt_text("  ", "template", "default"), "  ")

    def test_main_window_reaches_benchmark_definitions_page(self) -> None:
        window = MainWindow(self.context)
        self.widgets.append(window)
        self.assertIs(window.benchmarks, window.pages["benchmarks"])
        window.navigate_to("benchmarks")
        self.assertEqual(window.current_page_key(), "benchmarks")
        self.assertIs(window.page_stack.currentWidget(), window.benchmarks)


if __name__ == "__main__":
    unittest.main()
