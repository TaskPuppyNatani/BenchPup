"""Small persistent CLI settings stored outside the local database."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AttachmentPreferences:
    """Last GUI attachment locations and the UI-only storage mode."""

    source_directory: Path | None = None
    destination_directory: Path | None = None
    storage_mode: str = "reference"


@dataclass(frozen=True)
class ImportPreferences:
    """Last GUI CSV import choices, kept outside benchmark data."""

    source_directory: Path | None = None
    import_type: str = "auto"
    duplicate_policy: str = "skip"


class DefaultWorkingDirectorySettings:
    """Persist one optional default directory without touching benchmark data."""

    def __init__(self, database_path: str | Path):
        database = Path(database_path)
        config_root = database.parent.parent if database.parent.name.lower() == "data" else database.parent
        self.path = config_root / "config" / "settings.json"
        self.legacy_path = database.with_suffix(".settings.json")

    def get_default_working_directory(self) -> Path | None:
        data = self._settings_data()
        value = data.get("default_working_directory") if isinstance(data, dict) else None
        if value is None and isinstance(data, dict):
            value = data.get("default_file_directory")
        return Path(value) if isinstance(value, str) and value else None

    def get_attachment_preferences(self) -> AttachmentPreferences:
        data = self._settings_data().get("attachment_preferences")
        if not isinstance(data, dict):
            return AttachmentPreferences()

        source = data.get("source_directory")
        destination = data.get("destination_directory")
        mode = data.get("storage_mode")
        return AttachmentPreferences(
            source_directory=Path(source) if isinstance(source, str) and source else None,
            destination_directory=Path(destination) if isinstance(destination, str) and destination else None,
            storage_mode=mode if isinstance(mode, str) and mode else "reference",
        )

    def get_import_preferences(self) -> ImportPreferences:
        data = self._settings_data().get("import_preferences")
        if not isinstance(data, dict):
            return ImportPreferences()

        source = data.get("source_directory")
        import_type = data.get("import_type")
        duplicate_policy = data.get("duplicate_policy")
        return ImportPreferences(
            source_directory=Path(source) if isinstance(source, str) and source else None,
            import_type=import_type if isinstance(import_type, str) and import_type in {"auto", "benchmark_run", "scoreboard"} else "auto",
            duplicate_policy=duplicate_policy if isinstance(duplicate_policy, str) and duplicate_policy in {"skip", "replace", "keep"} else "skip",
        )

    @staticmethod
    def _read(path: Path) -> dict[str, object] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def set_default_working_directory(self, directory: str | Path) -> None:
        data = self._settings_data()
        data.pop("default_file_directory", None)
        data["default_working_directory"] = str(Path(directory))
        self._write(data)

    def clear_default_working_directory(self) -> None:
        data = self._settings_data()
        data.pop("default_working_directory", None)
        data.pop("default_file_directory", None)
        self._write(data)

    def set_attachment_preferences(self, preferences: AttachmentPreferences) -> None:
        data = self._settings_data()
        data["attachment_preferences"] = {
            "source_directory": str(preferences.source_directory) if preferences.source_directory else "",
            "destination_directory": str(preferences.destination_directory) if preferences.destination_directory else "",
            "storage_mode": preferences.storage_mode,
        }
        self._write(data)

    def set_import_preferences(self, preferences: ImportPreferences) -> None:
        data = self._settings_data()
        data["import_preferences"] = {
            "source_directory": str(preferences.source_directory) if preferences.source_directory else "",
            "import_type": preferences.import_type if isinstance(preferences.import_type, str) and preferences.import_type in {"auto", "benchmark_run", "scoreboard"} else "auto",
            "duplicate_policy": preferences.duplicate_policy if isinstance(preferences.duplicate_policy, str) and preferences.duplicate_policy in {"skip", "replace", "keep"} else "skip",
        }
        self._write(data)

    def _settings_data(self) -> dict[str, object]:
        return self._read(self.path) or self._read(self.legacy_path) or {}

    def _write(self, data: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)
