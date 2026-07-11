"""Small persistent CLI settings stored outside the local database."""
from __future__ import annotations

import json
import os
from pathlib import Path


class DefaultWorkingDirectorySettings:
    """Persist one optional default directory without touching benchmark data."""

    def __init__(self, database_path: str | Path):
        database = Path(database_path)
        config_root = database.parent.parent if database.parent.name.lower() == "data" else database.parent
        self.path = config_root / "config" / "settings.json"
        self.legacy_path = database.with_suffix(".settings.json")

    def get_default_working_directory(self) -> Path | None:
        data = self._read(self.path) or self._read(self.legacy_path)
        value = data.get("default_working_directory") if isinstance(data, dict) else None
        if value is None and isinstance(data, dict):
            value = data.get("default_file_directory")
        return Path(value) if isinstance(value, str) and value else None

    @staticmethod
    def _read(path: Path) -> dict[str, object] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def set_default_working_directory(self, directory: str | Path) -> None:
        self._write({"default_working_directory": str(Path(directory))})

    def clear_default_working_directory(self) -> None:
        self._write({})

    def _write(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)
