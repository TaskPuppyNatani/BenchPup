"""Explicit Add/Edit dialog for ModelProfile records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from PySide6.QtWidgets import QCheckBox, QDoubleSpinBox, QHBoxLayout, QLineEdit, QSpinBox, QWidget

from ..context import GuiApplicationContext

try:
    from ...engine.domain import ModelProfile
except ImportError:  # pragma: no cover - exercised by top-level test imports.
    from engine.domain import ModelProfile  # type: ignore[no-redef]

from .base import CatalogEditorDialog


class OptionalNumericField(QWidget):
    """A numeric editor that keeps None separate from an entered zero."""

    def __init__(
        self,
        label: str,
        *,
        integer: bool,
        minimum: float,
        maximum: float,
        decimals: int = 3,
        value: int | float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(f"optional{label.replace(' ', '')}Field")
        self.record_checkbox = QCheckBox("Set value")
        self.record_checkbox.setAccessibleName(f"Record {label}")
        if integer:
            spin: QSpinBox | QDoubleSpinBox = QSpinBox()
            spin.setRange(int(minimum), int(maximum))
        else:
            double_spin = QDoubleSpinBox()
            double_spin.setRange(minimum, maximum)
            double_spin.setDecimals(decimals)
            double_spin.setSingleStep(0.1 if decimals <= 1 else 0.01)
            spin = double_spin
        spin.setAccessibleName(label)
        spin.setEnabled(value is not None)
        if value is not None:
            if isinstance(spin, QSpinBox):
                spin.setValue(int(value))
            else:
                spin.setValue(float(value))
        self.spin_box = spin
        self.record_checkbox.setChecked(value is not None)
        self.record_checkbox.toggled.connect(self.spin_box.setEnabled)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.record_checkbox)
        layout.addWidget(self.spin_box)
        layout.addStretch(1)

    def value(self) -> int | float | None:
        if not self.record_checkbox.isChecked():
            return None
        return int(self.spin_box.value()) if isinstance(self.spin_box, QSpinBox) else float(self.spin_box.value())


class ModelEditorDialog(CatalogEditorDialog):
    def __init__(
        self,
        context: GuiApplicationContext,
        profile: ModelProfile | None = None,
        parent: QWidget | None = None,
        *,
        confirm_close: Callable[[], bool] | None = None,
    ) -> None:
        self.profile = profile
        super().__init__(
            context,
            title="Edit Model Profile" if profile is not None else "Add Model Profile",
            parent=parent,
            confirm_close=confirm_close,
        )
        self.setAccessibleName("Edit Model Profile" if profile is not None else "Add Model Profile")

        self.name_edit = self._line(profile.name if profile else "", "Profile name", "Required")
        self.model_name_edit = self._line(profile.model_name if profile else "", "Model name", "Required")
        self.model_family_edit = self._line(profile.model_family if profile else "", "Model family")
        self.model_size_edit = self._line(profile.model_size if profile else "", "Model size")
        self.quantization_edit = self._line(profile.quantization if profile else "", "Quantization")
        self.backend_edit = self._line(profile.backend if profile else "Other", "Backend")
        self.moe_experts_edit = self._line(profile.moe_experts if profile else "", "MoE experts")

        self.temperature_field = OptionalNumericField(
            "Temperature (0-1)", integer=False, minimum=0.0, maximum=1.0,
            value=profile.temperature if profile else None,
        )
        self.top_p_field = OptionalNumericField(
            "Top-p (0-1)", integer=False, minimum=0.0, maximum=1.0,
            value=profile.top_p if profile else None,
        )
        self.top_k_field = OptionalNumericField(
            "Top-k", integer=True, minimum=0, maximum=1_000_000,
            value=profile.top_k if profile else None,
        )
        self.min_p_field = OptionalNumericField(
            "Min-p (0-1)", integer=False, minimum=0.0, maximum=1.0,
            value=profile.min_p if profile else None,
        )
        self.context_length_field = OptionalNumericField(
            "Context length", integer=True, minimum=1, maximum=10_000_000,
            value=profile.context_length if profile else None,
        )
        self.tokens_per_second_field = OptionalNumericField(
            "Tokens per second", integer=False, minimum=0.0, maximum=1_000_000.0,
            value=profile.tokens_per_second if profile else None,
        )

        self.thinking_checkbox = QCheckBox("Thinking enabled")
        self.thinking_checkbox.setAccessibleName("Thinking enabled")
        self.thinking_checkbox.setChecked(profile.thinking_enabled if profile else False)
        self.flash_attention_checkbox = QCheckBox("Flash attention enabled")
        self.flash_attention_checkbox.setAccessibleName("Flash attention enabled")
        self.flash_attention_checkbox.setChecked(profile.flash_attention if profile else False)
        self.default_checkbox = QCheckBox("Use as the default Model Profile")
        self.default_checkbox.setAccessibleName("Default Model Profile")
        self.default_checkbox.setToolTip("The engine keeps default-profile exclusivity; selecting this clears another default.")
        self.default_checkbox.setChecked(profile.is_default if profile else False)

        fields = (
            ("Profile name *", self.name_edit, "name"),
            ("Model name *", self.model_name_edit, "model_name"),
            ("Model family", self.model_family_edit, "model_family"),
            ("Model size", self.model_size_edit, "model_size"),
            ("Quantization", self.quantization_edit, "quantization"),
            ("Backend", self.backend_edit, "backend"),
            ("MoE experts", self.moe_experts_edit, "moe_experts"),
            ("Temperature", self.temperature_field, "temperature"),
            ("Top-p", self.top_p_field, "top_p"),
            ("Top-k", self.top_k_field, "top_k"),
            ("Min-p", self.min_p_field, "min_p"),
            ("Context length", self.context_length_field, "context_length"),
            ("Tokens per second", self.tokens_per_second_field, "tokens_per_second"),
        )
        for label, widget, field_name in fields:
            self.form.addRow(label, widget)
            self.register_field(field_name, widget)
        self.form.addRow("Runtime flags", self.thinking_checkbox)
        self.form.addRow("", self.flash_attention_checkbox)
        self.form.addRow("Profile behavior", self.default_checkbox)

        for field in (
            self.name_edit,
            self.model_name_edit,
            self.model_family_edit,
            self.model_size_edit,
            self.quantization_edit,
            self.backend_edit,
            self.moe_experts_edit,
        ):
            field.textChanged.connect(self.mark_dirty)
        for field in (
            self.temperature_field,
            self.top_p_field,
            self.top_k_field,
            self.min_p_field,
            self.context_length_field,
            self.tokens_per_second_field,
        ):
            field.record_checkbox.toggled.connect(self.mark_dirty)
            field.spin_box.valueChanged.connect(self.mark_dirty)
        for field in (self.thinking_checkbox, self.flash_attention_checkbox, self.default_checkbox):
            field.toggled.connect(self.mark_dirty)

    @staticmethod
    def _line(value: str, accessible_name: str, placeholder: str = "") -> QLineEdit:
        field = QLineEdit(value)
        field.setAccessibleName(accessible_name)
        if placeholder:
            field.setPlaceholderText(placeholder)
        return field

    def build_draft(self) -> ModelProfile:
        values: dict[str, Any] = dict(
            name=self.name_edit.text(),
            model_name=self.model_name_edit.text(),
            model_family=self.model_family_edit.text(),
            model_size=self.model_size_edit.text(),
            quantization=self.quantization_edit.text(),
            backend=self.backend_edit.text(),
            temperature=self.temperature_field.value(),
            top_p=self.top_p_field.value(),
            top_k=self.top_k_field.value(),
            min_p=self.min_p_field.value(),
            thinking_enabled=self.thinking_checkbox.isChecked(),
            flash_attention=self.flash_attention_checkbox.isChecked(),
            moe_experts=self.moe_experts_edit.text(),
            context_length=self.context_length_field.value(),
            tokens_per_second=self.tokens_per_second_field.value(),
            is_default=self.default_checkbox.isChecked(),
        )
        if self.profile is not None:
            return replace(self.profile, **values)
        return ModelProfile(**values)

    def save_draft(self, draft: ModelProfile) -> ModelProfile:
        return self.context.catalog.save_model_profile(draft, make_default=draft.is_default)


__all__ = ("ModelEditorDialog", "OptionalNumericField")
