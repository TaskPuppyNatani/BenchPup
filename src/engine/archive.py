"""Versioned, transactional BenchPup JSON backup and restore support."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .database import EngineDatabase, MIGRATIONS
from .domain import now

ARCHIVE_FORMAT = "benchpup_archive"
ARCHIVE_VERSION = 1
TABLES = (
    "benchmark_sessions", "model_profiles", "hardware_profiles", "benchmark_definitions",
    "prompt_templates", "benchmark_runs", "review_scores", "run_attachments",
    "scoreboard_import_batches", "scoreboard_entries", "export_profiles",
)
DEPENDENCIES = {
    "benchmark_runs": {"session_id": "benchmark_sessions", "model_profile_id": "model_profiles",
                       "benchmark_definition_id": "benchmark_definitions", "prompt_template_id": "prompt_templates",
                       "hardware_profile_id": "hardware_profiles"},
    "review_scores": {"run_id": "benchmark_runs"},
    "run_attachments": {"run_id": "benchmark_runs"},
    "scoreboard_entries": {"import_batch_id": "scoreboard_import_batches"},
}
UNIQUE = {
    "benchmark_sessions": ("title",), "model_profiles": ("name",), "hardware_profiles": ("name",),
    "benchmark_definitions": ("name",), "prompt_templates": ("name", "version"),
    "benchmark_runs": ("fingerprint",), "scoreboard_import_batches": ("name", "source_file"),
    "export_profiles": ("name",),
}


class ArchiveError(ValueError):
    """A friendly archive error suitable for CLI display."""


@dataclass
class RestoreReport:
    created: dict[str, int] = field(default_factory=lambda: {table: 0 for table in TABLES})
    skipped: dict[str, int] = field(default_factory=lambda: {table: 0 for table in TABLES})
    updated: dict[str, int] = field(default_factory=lambda: {table: 0 for table in TABLES})
    failed: dict[str, int] = field(default_factory=lambda: {table: 0 for table in TABLES})


class ArchiveService:
    def __init__(self, database: EngineDatabase):
        self.database = database
        self.last_restore_report: RestoreReport | None = None

    def build_archive(self, benchpup_version: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            data = {table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")] for table in TABLES}
            schema_row = connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        return {
            "format": ARCHIVE_FORMAT, "archive_version": ARCHIVE_VERSION, "created_at": now(),
            "benchpup_version": benchpup_version, "schema_version": schema_row["version"] if schema_row else 0,
            "counts": {table: len(records) for table, records in data.items()}, "data": data,
        }

    def export(self, path: str | Path, benchpup_version: str) -> Path:
        archive = self.build_archive(benchpup_version)
        self.validate(archive)
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                json.dump(archive, output, ensure_ascii=False, indent=2, sort_keys=True)
                output.write("\n")
                output.flush(); os.fsync(output.fileno())
            with temporary.open("r", encoding="utf-8") as source:
                self.validate(json.load(source))
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def load(self, path: str | Path) -> dict[str, Any]:
        try:
            with Path(path).open("r", encoding="utf-8") as source:
                archive = json.load(source)
        except (OSError, json.JSONDecodeError) as error:
            raise ArchiveError(f"Could not read archive: {error}") from None
        self.validate(archive)
        return archive

    def validate(self, archive: Any) -> None:
        if not isinstance(archive, dict):
            raise ArchiveError("Archive must be a JSON object")
        if archive.get("format") != ARCHIVE_FORMAT:
            raise ArchiveError("Archive format is not a BenchPup archive")
        if archive.get("archive_version") != ARCHIVE_VERSION:
            raise ArchiveError(f"Unsupported archive version: {archive.get('archive_version')!r}")
        data = archive.get("data")
        if not isinstance(data, dict):
            raise ArchiveError("Archive data section is missing")
        for table in TABLES:
            records = data.get(table, [])
            if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
                raise ArchiveError(f"Archive field {table!r} must be a list of records")
        counts = archive.get("counts", {})
        if counts and not isinstance(counts, dict):
            raise ArchiveError("Archive counts must be an object")

    def preview(self, archive: dict[str, Any]) -> dict[str, Any]:
        self.validate(archive)
        metadata_counts = archive.get("counts", {})
        return {
            "created_at": archive.get("created_at", ""), "benchpup_version": archive.get("benchpup_version", ""),
            "archive_version": archive["archive_version"], "schema_version": archive.get("schema_version", ""),
            "counts": {
                table: metadata_counts[table] if isinstance(metadata_counts.get(table), int)
                else len(archive["data"].get(table, []))
                for table in TABLES
            },
            "warnings": [f"{table} missing; treating as empty" for table in TABLES if table not in archive["data"]],
        }

    @staticmethod
    def _columns(connection, table: str) -> set[str]:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _find_existing(connection, table: str, record: dict[str, Any]) -> int | None:
        if table == "review_scores" and record.get("run_id") is not None:
            row = connection.execute("SELECT id FROM review_scores WHERE run_id = ?", (record["run_id"],)).fetchone()
            return row["id"] if row else None
        if table == "run_attachments" and record.get("run_id") is not None:
            row = connection.execute("SELECT id FROM run_attachments WHERE run_id = ? AND file_path = ? AND original_filename = ?",
                                     (record["run_id"], record.get("file_path", ""), record.get("original_filename", ""))).fetchone()
            return row["id"] if row else None
        if table == "scoreboard_entries":
            row = connection.execute("SELECT id FROM scoreboard_entries WHERE model_name = ? AND imported_at = ? AND import_batch_id IS ?",
                                     (record.get("model_name", ""), record.get("imported_at", ""), record.get("import_batch_id"))).fetchone()
            return row["id"] if row else None
        unique = UNIQUE.get(table)
        if not unique or any(record.get(field) in (None, "") for field in unique):
            return None
        where = " AND ".join(f"{field} = ?" for field in unique)
        row = connection.execute(f"SELECT id FROM {table} WHERE {where}", [record[field] for field in unique]).fetchone()
        return row["id"] if row else None

    def merge(self, archive: dict[str, Any]) -> RestoreReport:
        self.validate(archive)
        report, id_maps = RestoreReport(), {table: {} for table in TABLES}
        with self.database.connection() as connection:
            available = {table: self._columns(connection, table) for table in TABLES}
            for table in TABLES:
                for source in archive["data"].get(table, []):
                    source_id = source.get("id")
                    record = {key: value for key, value in source.items() if key in available[table] and key != "id"}
                    for field, parent in DEPENDENCIES.get(table, {}).items():
                        old_id = source.get(field)
                        if old_id is None:
                            record[field] = None
                        elif old_id in id_maps[parent]:
                            record[field] = id_maps[parent][old_id]
                        else:
                            raise ArchiveError(f"{table} references missing {parent} record {old_id}")
                    existing_id = self._find_existing(connection, table, record)
                    if existing_id is not None:
                        if source_id is not None: id_maps[table][source_id] = existing_id
                        report.skipped[table] += 1
                        continue
                    if not record:
                        raise ArchiveError(f"{table} contains no supported fields")
                    columns = list(record)
                    cursor = connection.execute(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                        [record[column] for column in columns],
                    )
                    if source_id is not None: id_maps[table][source_id] = cursor.lastrowid
                    report.created[table] += 1
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ArchiveError("Restored archive contains invalid relationships")
        self.last_restore_report = report
        return report

    def replace(self, archive: dict[str, Any], benchpup_version: str) -> Path:
        self.validate(archive)
        original = self.database.path
        safety = original.with_name(f"{original.stem}.pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}{original.suffix}")
        shutil.copy2(original, safety)
        temporary = original.with_name(f".{original.stem}.restore-{os.getpid()}{original.suffix}")
        temporary.unlink(missing_ok=True)
        try:
            restored_database = EngineDatabase(temporary)
            restored_database.migrate()
            temporary_service = ArchiveService(restored_database)
            self.last_restore_report = temporary_service.merge(archive)
            restored_database.migrate()
            with restored_database.connection() as connection:
                if connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise ArchiveError("Temporary restored database failed relationship checks")
            os.replace(temporary, original)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return safety
