"""Versioned, transactional BenchPup JSON backup and restore support."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, NoReturn, cast

from .database import DatabaseMaintenanceError, EngineDatabase, MIGRATIONS
from .domain import now


ARCHIVE_FORMAT = "benchpup_archive"
ARCHIVE_VERSION = 1
CURRENT_SCHEMA_VERSION = max(version for version, _script in MIGRATIONS)
TABLES = (
    "benchmark_sessions",
    "model_profiles",
    "hardware_profiles",
    "benchmark_definitions",
    "prompt_templates",
    "benchmark_runs",
    "review_scores",
    "run_attachments",
    "scoreboard_import_batches",
    "scoreboard_entries",
    "export_profiles",
)
DEPENDENCIES = {
    "benchmark_runs": {
        "session_id": "benchmark_sessions",
        "model_profile_id": "model_profiles",
        "benchmark_definition_id": "benchmark_definitions",
        "prompt_template_id": "prompt_templates",
        "hardware_profile_id": "hardware_profiles",
    },
    "review_scores": {"run_id": "benchmark_runs"},
    "run_attachments": {"run_id": "benchmark_runs"},
    "scoreboard_entries": {"import_batch_id": "scoreboard_import_batches"},
}
REQUIRED_DEPENDENCIES = {
    ("review_scores", "run_id"),
    ("run_attachments", "run_id"),
}
UNIQUE = {
    "benchmark_sessions": ("title",),
    "model_profiles": ("name",),
    "hardware_profiles": ("name",),
    "benchmark_definitions": ("name",),
    "prompt_templates": ("name", "version"),
    "benchmark_runs": ("fingerprint",),
    "scoreboard_import_batches": ("name", "source_file"),
    "export_profiles": ("name",),
}

# These are the fields that cannot be omitted from a current persisted row.
# Additive fields introduced by later migrations remain optional so supported
# older archives continue to restore through the existing schema defaults.
REQUIRED_RECORD_FIELDS: dict[str, tuple[str, ...]] = {
    "benchmark_sessions": ("title", "created_at", "updated_at"),
    "model_profiles": ("name", "model_name", "created_at", "updated_at"),
    "hardware_profiles": ("name", "created_at", "updated_at"),
    "benchmark_definitions": (
        "name",
        "file_path",
        "benchmark_type",
        "created_at",
        "updated_at",
    ),
    "prompt_templates": (
        "name",
        "version",
        "prompt_text",
        "prompt_hash",
        "benchmark_type",
        "created_at",
        "updated_at",
    ),
    "benchmark_runs": (
        "raw_model_output",
        "prompt_name",
        "prompt_text",
        "model_snapshot",
        "benchmark_snapshot",
        "prompt_snapshot",
        "hardware_snapshot",
        "fingerprint",
        "created_at",
        "updated_at",
    ),
    "review_scores": (
        "run_id",
        "hallucination_level",
        "reliability_level",
        "created_at",
        "updated_at",
    ),
    "run_attachments": (
        "run_id",
        "attachment_type",
        "file_path",
        "original_filename",
        "created_at",
    ),
    "scoreboard_import_batches": (
        "name",
        "source_file",
        "imported_at",
        "created_at",
        "updated_at",
    ),
    "scoreboard_entries": ("model_name", "imported_at", "created_at", "updated_at"),
    "export_profiles": ("name", "format", "created_at", "updated_at"),
}


class ArchiveIssueSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class ArchiveValidationState(str, Enum):
    VALID = "valid"
    INVALID = "invalid"


class ArchiveCompatibilityState(str, Enum):
    COMPATIBLE = "compatible"
    LEGACY = "legacy"
    UNSUPPORTED = "unsupported"


class ArchiveRecoveryStatus(str, Enum):
    NOT_ATTEMPTED = "not_attempted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ArchiveIssueCode(str, Enum):
    FILE_MISSING = "file_missing"
    PATH_IS_DIRECTORY = "path_is_directory"
    READ_FAILED = "read_failed"
    MALFORMED_JSON = "malformed_json"
    ROOT_NOT_OBJECT = "root_not_object"
    INVALID_FORMAT = "invalid_format"
    UNSUPPORTED_ARCHIVE_VERSION = "unsupported_archive_version"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    INVALID_METADATA = "invalid_metadata"
    MISSING_METADATA = "missing_metadata"
    INVALID_COUNTS = "invalid_counts"
    COUNT_MISMATCH = "count_mismatch"
    INVALID_COLLECTION = "invalid_collection"
    INVALID_RECORD = "invalid_record"
    INVALID_ID = "invalid_id"
    DUPLICATE_SOURCE_ID = "duplicate_source_id"
    INVALID_RELATIONSHIP = "invalid_relationship"
    MISSING_TABLE = "missing_table"
    LEGACY_SCHEMA = "legacy_schema"
    INVALID_DESTINATION = "invalid_destination"
    DESTINATION_LOCKED = "destination_locked"
    PERMISSION_DENIED = "permission_denied"
    SERIALIZATION_FAILED = "serialization_failed"
    TEMPORARY_VALIDATION_FAILED = "temporary_validation_failed"
    ATOMIC_REPLACE_FAILED = "atomic_replace_failed"
    DATABASE_BUSY = "database_busy"
    DATABASE_LOCKED = "database_locked"
    ACTIVE_DATABASE_BUSY = "active_database_busy"
    INTEGRITY_FAILED = "integrity_failed"
    FOREIGN_KEY_FAILED = "foreign_key_failed"
    MERGE_FAILED = "merge_failed"
    STALE_ARCHIVE = "stale_archive"
    SAFETY_BACKUP_FAILED = "safety_backup_failed"
    SAFETY_BACKUP_INVALID = "safety_backup_invalid"
    TEMPORARY_DATABASE_FAILED = "temporary_database_failed"
    MIGRATION_FAILED = "migration_failed"
    TEMPORARY_DATABASE_INVALID = "temporary_database_invalid"
    WAL_CHECKPOINT_FAILED = "wal_checkpoint_failed"
    ACTIVE_REPLACE_FAILED = "active_replace_failed"
    REOPEN_FAILED = "reopen_failed"
    RECOVERY_FAILED = "recovery_failed"
    CLEANUP_FAILED = "cleanup_failed"


@dataclass(frozen=True)
class ArchiveIssue:
    """Stable, UI-independent description of one archive problem or warning."""

    code: ArchiveIssueCode
    severity: ArchiveIssueSeverity
    message: str
    field: str | None = None
    section: str | None = None
    table: str | None = None
    expected: object | None = None
    actual: object | None = None


@dataclass(frozen=True)
class ArchiveFingerprint:
    """Engine-owned fingerprint used to reject stale selected archive files."""

    path: Path
    size: int
    modified_ns: int
    sha256: str


def _frozen_mapping(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], MappingProxyType(dict(values)))


@dataclass(frozen=True)
class ArchivePreview:
    """Typed archive inspection data suitable for a future GUI."""

    validation_state: ArchiveValidationState
    compatibility_state: ArchiveCompatibilityState
    archive_path: Path | None
    fingerprint: ArchiveFingerprint | None
    format_identifier: str | None
    archive_version: int | None
    created_at: str | None
    benchpup_version: str | None
    archive_schema_version: int | None
    current_schema_version: int
    declared_counts: Mapping[str, int | None]
    actual_counts: Mapping[str, int]
    total_record_count: int
    attachment_metadata_count: int
    issues: tuple[ArchiveIssue, ...]
    merge_eligible: bool
    replace_eligible: bool

    @property
    def valid(self) -> bool:
        return self.validation_state is ArchiveValidationState.VALID


@dataclass(frozen=True)
class ArchiveExportResult:
    path: Path
    counts: Mapping[str, int]
    warnings: tuple[ArchiveIssue, ...] = ()


@dataclass(frozen=True)
class ArchiveMergeResult:
    report: "RestoreReport"
    warnings: tuple[ArchiveIssue, ...] = ()


@dataclass(frozen=True)
class ArchiveReplaceResult:
    report: "RestoreReport"
    safety_backup_path: Path
    recovery_status: ArchiveRecoveryStatus
    recovery_path: Path | None = None
    restart_required: bool = False
    warnings: tuple[ArchiveIssue, ...] = ()


class ArchiveError(ValueError):
    """A friendly archive error with a stable engine-owned error code."""

    def __init__(
        self,
        message: str,
        *,
        code: ArchiveIssueCode | str = ArchiveIssueCode.MERGE_FAILED,
        issue: ArchiveIssue | None = None,
        recovery_status: ArchiveRecoveryStatus = ArchiveRecoveryStatus.NOT_ATTEMPTED,
        safety_backup_path: Path | None = None,
        recovery_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.issue = issue
        self.recovery_status = recovery_status
        self.safety_backup_path = safety_backup_path
        self.recovery_path = recovery_path


class ArchiveReadError(ArchiveError):
    """The archive file could not be read or decoded."""


class ArchiveValidationError(ArchiveError):
    """The archive structure or compatibility contract is invalid."""


class ArchivePersistenceError(ArchiveError):
    """Archive or database persistence failed."""


class ArchiveBusyError(ArchiveError):
    """The database or destination is busy or locked."""


@dataclass
class RestoreReport:
    created: dict[str, int] = field(default_factory=lambda: {table: 0 for table in TABLES})
    skipped: dict[str, int] = field(default_factory=lambda: {table: 0 for table in TABLES})
    updated: dict[str, int] = field(default_factory=lambda: {table: 0 for table in TABLES})
    failed: dict[str, int] = field(default_factory=lambda: {table: 0 for table in TABLES})
    remapped: dict[str, int] = field(default_factory=lambda: {table: 0 for table in TABLES})
    warnings: tuple[ArchiveIssue, ...] = ()


class ArchiveService:
    def __init__(self, database: EngineDatabase):
        self.database = database
        self.last_restore_report: RestoreReport | None = None

    def build_archive(
        self,
        benchpup_version: str,
        *,
        allow_during_maintenance: bool = False,
    ) -> dict[str, Any]:
        """Read every archived table from one consistent SQLite snapshot."""

        try:
            with self.database.connection(
                allow_during_maintenance=allow_during_maintenance
            ) as connection:
                connection.execute("BEGIN")
                data = {
                    table: [
                        dict(row)
                        for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")
                    ]
                    for table in TABLES
                }
                schema_row = connection.execute(
                    "SELECT version FROM schema_version WHERE id = 1"
                ).fetchone()
        except DatabaseMaintenanceError as error:
            self._raise_error(
                "The database is busy with another maintenance operation",
                ArchiveIssueCode.ACTIVE_DATABASE_BUSY,
                ArchiveBusyError,
                error,
            )
        except sqlite3.OperationalError as error:
            self._raise_sqlite_error("Could not read the database for backup", error)
        except sqlite3.DatabaseError as error:
            self._raise_error(
                f"Could not read the database for backup: {error}",
                ArchiveIssueCode.READ_FAILED,
                ArchivePersistenceError,
                error,
            )

        return {
            "format": ARCHIVE_FORMAT,
            "archive_version": ARCHIVE_VERSION,
            "created_at": now(),
            "benchpup_version": benchpup_version,
            "schema_version": schema_row["version"] if schema_row else 0,
            "counts": {table: len(records) for table, records in data.items()},
            "data": data,
        }

    def export(
        self,
        path: str | Path,
        benchpup_version: str,
        *,
        create_parent: bool = True,
    ) -> Path:
        """Preserve the legacy CLI return shape while using typed internals."""

        return self.export_typed(
            path,
            benchpup_version,
            create_parent=create_parent,
        ).path

    def export_typed(
        self,
        path: str | Path,
        benchpup_version: str,
        *,
        create_parent: bool = True,
    ) -> ArchiveExportResult:
        destination = self._validate_destination(path, create_parent=create_parent)
        archive = self.build_archive(benchpup_version)
        preview = self.inspect_archive(archive, archive_path=destination)
        self._require_valid(preview)
        self._write_archive(archive, destination, create_parent=create_parent)
        return ArchiveExportResult(
            path=destination,
            counts=cast(Mapping[str, int], preview.actual_counts),
            warnings=tuple(
                issue
                for issue in preview.issues
                if issue.severity is ArchiveIssueSeverity.WARNING
            ),
        )

    def load(self, path: str | Path) -> dict[str, Any]:
        archive = self._read_archive_file(path)
        self.validate(archive)
        return archive

    def fingerprint(self, path: str | Path) -> ArchiveFingerprint:
        """Return the engine-owned file fingerprint used for stale detection."""

        archive_path = self._resolved_file_path(path)
        try:
            first_stat = archive_path.stat()
            if not archive_path.is_file():
                self._raise_issue(
                    ArchiveIssue(
                        ArchiveIssueCode.PATH_IS_DIRECTORY,
                        ArchiveIssueSeverity.ERROR,
                        f"Archive path is not a file: {archive_path}",
                        field="archive_path",
                        actual=str(archive_path),
                    ),
                    ArchiveReadError,
                )
            digest = hashlib.sha256()
            with archive_path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            final_stat = archive_path.stat()
        except ArchiveError:
            raise
        except FileNotFoundError as error:
            self._raise_error(
                f"Archive file was not found: {archive_path}",
                ArchiveIssueCode.FILE_MISSING,
                ArchiveReadError,
                error,
            )
        except PermissionError as error:
            self._raise_error(
                f"Archive file could not be read because access was denied: {archive_path}",
                ArchiveIssueCode.PERMISSION_DENIED,
                ArchiveReadError,
                error,
            )
        except OSError as error:
            self._raise_error(
                f"Archive file could not be read: {error}",
                ArchiveIssueCode.READ_FAILED,
                ArchiveReadError,
                error,
            )
        if (
            first_stat.st_size != final_stat.st_size
            or first_stat.st_mtime_ns != final_stat.st_mtime_ns
        ):
            self._raise_error(
                f"Archive changed while it was being read: {archive_path}",
                ArchiveIssueCode.STALE_ARCHIVE,
                ArchiveValidationError,
            )
        return ArchiveFingerprint(
            path=archive_path,
            size=final_stat.st_size,
            modified_ns=final_stat.st_mtime_ns,
            sha256=digest.hexdigest(),
        )

    def inspect(self, path: str | Path) -> ArchivePreview:
        """Inspect an archive file without writing the archive or database."""

        archive_path = self._resolved_file_path(path)
        try:
            fingerprint = self.fingerprint(archive_path)
            archive = self._read_archive_file(archive_path)
            current_fingerprint = self.fingerprint(archive_path)
            if current_fingerprint != fingerprint:
                self._raise_error(
                    f"Archive changed while it was being inspected: {archive_path}",
                    ArchiveIssueCode.STALE_ARCHIVE,
                    ArchiveValidationError,
                )
        except ArchiveError as error:
            issue = error.issue or ArchiveIssue(
                cast(ArchiveIssueCode, error.code)
                if isinstance(error.code, ArchiveIssueCode)
                else ArchiveIssueCode.READ_FAILED,
                ArchiveIssueSeverity.ERROR,
                str(error),
                field="archive_path",
                actual=str(archive_path),
            )
            return self._empty_preview(
                archive_path=archive_path,
                fingerprint=None,
                issues=(issue,),
            )
        return self.inspect_archive(
            archive,
            archive_path=archive_path,
            fingerprint=current_fingerprint,
        )

    def inspect_archive(
        self,
        archive: Any,
        *,
        archive_path: Path | None = None,
        fingerprint: ArchiveFingerprint | None = None,
    ) -> ArchivePreview:
        issues: list[ArchiveIssue] = []
        format_identifier: str | None = None
        archive_version: int | None = None
        created_at: str | None = None
        benchpup_version: str | None = None
        archive_schema_version: int | None = None
        declared_counts: dict[str, int | None] = {table: None for table in TABLES}
        actual_counts: dict[str, int] = {table: 0 for table in TABLES}
        data: dict[str, Any] = {}

        if not isinstance(archive, dict):
            issues.append(
                ArchiveIssue(
                    ArchiveIssueCode.ROOT_NOT_OBJECT,
                    ArchiveIssueSeverity.ERROR,
                    "Archive must be a JSON object",
                    section="root",
                    expected="object",
                    actual=type(archive).__name__,
                )
            )
        else:
            raw_format = archive.get("format")
            format_identifier = raw_format if isinstance(raw_format, str) else None
            if raw_format != ARCHIVE_FORMAT:
                issues.append(
                    ArchiveIssue(
                        ArchiveIssueCode.INVALID_FORMAT,
                        ArchiveIssueSeverity.ERROR,
                        "Archive format is not a BenchPup archive",
                        field="format",
                        expected=ARCHIVE_FORMAT,
                        actual=raw_format,
                    )
                )

            raw_archive_version = archive.get("archive_version")
            if not isinstance(raw_archive_version, int) or isinstance(
                raw_archive_version, bool
            ):
                issues.append(
                    ArchiveIssue(
                        ArchiveIssueCode.UNSUPPORTED_ARCHIVE_VERSION,
                        ArchiveIssueSeverity.ERROR,
                        f"Unsupported archive version: {raw_archive_version!r}",
                        field="archive_version",
                        expected=ARCHIVE_VERSION,
                        actual=raw_archive_version,
                    )
                )
            else:
                archive_version = raw_archive_version
                if archive_version != ARCHIVE_VERSION:
                    issues.append(
                        ArchiveIssue(
                            ArchiveIssueCode.UNSUPPORTED_ARCHIVE_VERSION,
                            ArchiveIssueSeverity.ERROR,
                            f"Unsupported archive version: {archive_version!r}",
                            field="archive_version",
                            expected=ARCHIVE_VERSION,
                            actual=archive_version,
                        )
                    )

            raw_created_at = archive.get("created_at")
            if not isinstance(raw_created_at, str) or not self._valid_timestamp(
                raw_created_at
            ):
                issues.append(
                    ArchiveIssue(
                        ArchiveIssueCode.INVALID_METADATA,
                        ArchiveIssueSeverity.ERROR,
                        "Archive created_at must be a timezone-aware ISO-8601 timestamp",
                        field="created_at",
                        expected="timezone-aware ISO-8601 string",
                        actual=raw_created_at,
                    )
                )
            else:
                created_at = raw_created_at

            raw_version = archive.get("benchpup_version")
            if not isinstance(raw_version, str) or not raw_version.strip():
                issues.append(
                    ArchiveIssue(
                        ArchiveIssueCode.INVALID_METADATA,
                        ArchiveIssueSeverity.ERROR,
                        "Archive benchpup_version must be a non-empty string",
                        field="benchpup_version",
                        expected="non-empty string",
                        actual=raw_version,
                    )
                )
            else:
                benchpup_version = raw_version

            raw_schema_version = archive.get("schema_version")
            if not isinstance(raw_schema_version, int) or isinstance(
                raw_schema_version, bool
            ) or raw_schema_version < 1:
                issues.append(
                    ArchiveIssue(
                        ArchiveIssueCode.INVALID_METADATA,
                        ArchiveIssueSeverity.ERROR,
                        "Archive schema_version must be a positive integer",
                        field="schema_version",
                        expected="positive integer",
                        actual=raw_schema_version,
                    )
                )
            else:
                archive_schema_version = raw_schema_version
                if archive_schema_version > CURRENT_SCHEMA_VERSION:
                    issues.append(
                        ArchiveIssue(
                            ArchiveIssueCode.UNSUPPORTED_SCHEMA_VERSION,
                            ArchiveIssueSeverity.ERROR,
                            f"Unsupported schema version: {archive_schema_version!r}",
                            field="schema_version",
                            expected=f"<= {CURRENT_SCHEMA_VERSION}",
                            actual=archive_schema_version,
                        )
                    )
                elif archive_schema_version < CURRENT_SCHEMA_VERSION:
                    issues.append(
                        ArchiveIssue(
                            ArchiveIssueCode.LEGACY_SCHEMA,
                            ArchiveIssueSeverity.WARNING,
                            f"Archive uses older schema version {archive_schema_version}; current schema is {CURRENT_SCHEMA_VERSION}",
                            field="schema_version",
                            expected=CURRENT_SCHEMA_VERSION,
                            actual=archive_schema_version,
                        )
                    )

            raw_counts = archive.get("counts")
            if raw_counts is None:
                issues.append(
                    ArchiveIssue(
                        ArchiveIssueCode.MISSING_METADATA,
                        ArchiveIssueSeverity.ERROR,
                        "Archive counts are missing",
                        field="counts",
                        expected="object",
                        actual=None,
                    )
                )
                counts: dict[str, Any] = {}
            elif not isinstance(raw_counts, dict):
                issues.append(
                    ArchiveIssue(
                        ArchiveIssueCode.INVALID_COUNTS,
                        ArchiveIssueSeverity.ERROR,
                        "Archive counts must be an object",
                        field="counts",
                        expected="object",
                        actual=type(raw_counts).__name__,
                    )
                )
                counts = {}
            else:
                counts = raw_counts

            raw_data = archive.get("data")
            if not isinstance(raw_data, dict):
                issues.append(
                    ArchiveIssue(
                        ArchiveIssueCode.INVALID_METADATA,
                        ArchiveIssueSeverity.ERROR,
                        "Archive data section is missing or is not an object",
                        field="data",
                        expected="object",
                        actual=type(raw_data).__name__,
                    )
                )
            else:
                data = raw_data

            source_ids: dict[str, set[int]] = {table: set() for table in TABLES}
            duplicate_ids: dict[str, set[int]] = {table: set() for table in TABLES}
            for table in TABLES:
                if table not in data:
                    issues.append(
                        ArchiveIssue(
                            ArchiveIssueCode.MISSING_TABLE,
                            ArchiveIssueSeverity.WARNING,
                            f"{table} missing; treating as empty",
                            section="data",
                            table=table,
                        )
                    )
                    records: Any = []
                else:
                    records = data[table]
                if not isinstance(records, list):
                    issues.append(
                        ArchiveIssue(
                            ArchiveIssueCode.INVALID_COLLECTION,
                            ArchiveIssueSeverity.ERROR,
                            f"Archive field {table!r} must be a list of records",
                            section="data",
                            table=table,
                            expected="list",
                            actual=type(records).__name__,
                        )
                    )
                    records = []
                actual_counts[table] = len(records)
                declared = counts.get(table)
                if declared is None:
                    declared_counts[table] = None
                elif not isinstance(declared, int) or isinstance(declared, bool) or declared < 0:
                    issues.append(
                        ArchiveIssue(
                            ArchiveIssueCode.INVALID_COUNTS,
                            ArchiveIssueSeverity.ERROR,
                            f"Declared count for {table!r} must be a nonnegative integer",
                            field=f"counts.{table}",
                            table=table,
                            expected="nonnegative integer",
                            actual=declared,
                        )
                    )
                else:
                    declared_counts[table] = declared
                    if declared != len(records):
                        issues.append(
                            ArchiveIssue(
                                ArchiveIssueCode.COUNT_MISMATCH,
                                ArchiveIssueSeverity.ERROR,
                                f"Declared count for {table!r} does not match the archive data",
                                field=f"counts.{table}",
                                table=table,
                                expected=len(records),
                                actual=declared,
                            )
                        )

                for index, record in enumerate(records):
                    if not isinstance(record, dict):
                        issues.append(
                            ArchiveIssue(
                                ArchiveIssueCode.INVALID_RECORD,
                                ArchiveIssueSeverity.ERROR,
                                f"{table} record {index + 1} must be an object",
                                table=table,
                                expected="object",
                                actual=type(record).__name__,
                            )
                        )
                        continue
                    for key, value in record.items():
                        if not isinstance(key, str) or not self._database_scalar(value):
                            issues.append(
                                ArchiveIssue(
                                    ArchiveIssueCode.INVALID_RECORD,
                                    ArchiveIssueSeverity.ERROR,
                                    f"{table} record {index + 1} contains a non-database-compatible field",
                                    field=str(key),
                                    table=table,
                                    expected="string key with scalar or null value",
                                    actual=type(value).__name__,
                                )
                            )
                    source_id = record.get("id")
                    if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
                        issues.append(
                            ArchiveIssue(
                                ArchiveIssueCode.INVALID_ID,
                                ArchiveIssueSeverity.ERROR,
                                f"{table} record {index + 1} has an invalid source id",
                                field="id",
                                table=table,
                                expected="positive integer",
                                actual=source_id,
                            )
                        )
                    else:
                        if source_id in source_ids[table]:
                            duplicate_ids[table].add(source_id)
                            issues.append(
                                ArchiveIssue(
                                    ArchiveIssueCode.DUPLICATE_SOURCE_ID,
                                    ArchiveIssueSeverity.ERROR,
                                    f"{table} contains duplicate source id {source_id}",
                                    field="id",
                                    table=table,
                                    expected="unique source id",
                                    actual=source_id,
                                )
                            )
                        source_ids[table].add(source_id)
                    missing = [
                        field_name
                        for field_name in REQUIRED_RECORD_FIELDS.get(table, ())
                        if field_name not in record
                    ]
                    if missing:
                        issues.append(
                            ArchiveIssue(
                                ArchiveIssueCode.INVALID_RECORD,
                                ArchiveIssueSeverity.ERROR,
                                f"{table} record {index + 1} is missing required fields: {', '.join(missing)}",
                                table=table,
                                expected=REQUIRED_RECORD_FIELDS.get(table),
                                actual=tuple(record),
                            )
                        )

            for table, dependencies in DEPENDENCIES.items():
                records = data.get(table, [])
                if not isinstance(records, list):
                    continue
                for index, record in enumerate(records):
                    if not isinstance(record, dict):
                        continue
                    for field_name, parent in dependencies.items():
                        value = record.get(field_name)
                        required = (table, field_name) in REQUIRED_DEPENDENCIES
                        if value is None:
                            if required:
                                issues.append(
                                    ArchiveIssue(
                                        ArchiveIssueCode.INVALID_RELATIONSHIP,
                                        ArchiveIssueSeverity.ERROR,
                                        f"{table} record {index + 1} requires a {field_name} relationship",
                                        field=field_name,
                                        table=table,
                                        expected="positive parent id",
                                        actual=value,
                                    )
                                )
                            continue
                        if not isinstance(value, int) or isinstance(value, bool):
                            issues.append(
                                ArchiveIssue(
                                    ArchiveIssueCode.INVALID_RELATIONSHIP,
                                    ArchiveIssueSeverity.ERROR,
                                    f"{table} record {index + 1} has an invalid {field_name} relationship",
                                    field=field_name,
                                    table=table,
                                    expected="positive parent id or null",
                                    actual=value,
                                )
                            )
                        elif value not in source_ids[parent]:
                            issues.append(
                                ArchiveIssue(
                                    ArchiveIssueCode.INVALID_RELATIONSHIP,
                                    ArchiveIssueSeverity.ERROR,
                                    f"{table} references missing {parent} record {value}",
                                    field=field_name,
                                    table=table,
                                    expected=f"id from {parent}",
                                    actual=value,
                                )
                            )

        has_error = any(issue.severity is ArchiveIssueSeverity.ERROR for issue in issues)
        if archive_schema_version is not None and archive_schema_version > CURRENT_SCHEMA_VERSION:
            compatibility = ArchiveCompatibilityState.UNSUPPORTED
        elif any(issue.code is ArchiveIssueCode.LEGACY_SCHEMA for issue in issues):
            compatibility = ArchiveCompatibilityState.LEGACY
        else:
            compatibility = ArchiveCompatibilityState.COMPATIBLE
        state = ArchiveValidationState.INVALID if has_error else ArchiveValidationState.VALID
        eligible = state is ArchiveValidationState.VALID and compatibility is not ArchiveCompatibilityState.UNSUPPORTED
        return ArchivePreview(
            validation_state=state,
            compatibility_state=compatibility,
            archive_path=archive_path,
            fingerprint=fingerprint,
            format_identifier=format_identifier,
            archive_version=archive_version,
            created_at=created_at,
            benchpup_version=benchpup_version,
            archive_schema_version=archive_schema_version,
            current_schema_version=CURRENT_SCHEMA_VERSION,
            declared_counts=cast(Mapping[str, int | None], _frozen_mapping(declared_counts)),
            actual_counts=cast(Mapping[str, int], _frozen_mapping(actual_counts)),
            total_record_count=sum(actual_counts.values()),
            attachment_metadata_count=actual_counts["run_attachments"],
            issues=tuple(issues),
            merge_eligible=eligible,
            replace_eligible=eligible,
        )

    def validate(self, archive: Any) -> None:
        preview = self.inspect_archive(archive)
        self._require_valid(preview)

    def preview(self, archive: dict[str, Any] | str | Path) -> dict[str, Any]:
        """Preserve the legacy dictionary preview used by the CLI."""

        document = self.load(archive) if isinstance(archive, (str, Path)) else archive
        typed = self.inspect_archive(document)
        self._require_valid(typed)
        return {
            "created_at": typed.created_at or "",
            "benchpup_version": typed.benchpup_version or "",
            "archive_version": typed.archive_version,
            "schema_version": typed.archive_schema_version or "",
            "counts": dict(typed.actual_counts),
            "warnings": [
                issue.message
                for issue in typed.issues
                if issue.severity is ArchiveIssueSeverity.WARNING
            ],
        }

    def preview_typed(self, path: str | Path) -> ArchivePreview:
        return self.inspect(path)

    def merge(
        self,
        archive: dict[str, Any],
        *,
        fingerprint: ArchiveFingerprint | None = None,
    ) -> RestoreReport:
        """Preserve the legacy report return shape used by the CLI."""

        return self.merge_typed(archive, fingerprint=fingerprint).report

    def merge_file(
        self,
        path: str | Path,
        *,
        fingerprint: ArchiveFingerprint | None = None,
    ) -> ArchiveMergeResult:
        archive_path = self._resolved_file_path(path)
        expected = fingerprint or self.fingerprint(archive_path)
        self._verify_fingerprint(archive_path, expected)
        archive = self._read_archive_file(archive_path)
        self._verify_fingerprint(archive_path, expected)
        return self.merge_typed(
            archive,
            fingerprint=expected,
            archive_path=archive_path,
        )

    def merge_typed(
        self,
        archive: dict[str, Any],
        *,
        fingerprint: ArchiveFingerprint | None = None,
        archive_path: Path | None = None,
    ) -> ArchiveMergeResult:
        self.last_restore_report = None
        typed = self.inspect_archive(archive, archive_path=archive_path, fingerprint=fingerprint)
        self._require_valid(typed)
        if archive_path is not None and fingerprint is not None:
            self._verify_fingerprint(archive_path, fingerprint)
        try:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                report = self._merge_in_connection(connection, archive)
                self._verify_database_connection(connection)
        except ArchiveError:
            raise
        except DatabaseMaintenanceError as error:
            self._raise_error(
                "The database is busy with another maintenance operation",
                ArchiveIssueCode.ACTIVE_DATABASE_BUSY,
                ArchiveBusyError,
                error,
            )
        except sqlite3.IntegrityError as error:
            self._raise_error(
                f"Archive merge failed integrity checks: {error}",
                ArchiveIssueCode.INTEGRITY_FAILED,
                ArchivePersistenceError,
                error,
            )
        except sqlite3.OperationalError as error:
            self._raise_sqlite_error("Archive merge failed", error)
        except sqlite3.DatabaseError as error:
            self._raise_error(
                f"Archive merge failed: {error}",
                ArchiveIssueCode.MERGE_FAILED,
                ArchivePersistenceError,
                error,
            )
        self.last_restore_report = report
        return ArchiveMergeResult(
            report=report,
            warnings=tuple(
                issue
                for issue in typed.issues
                if issue.severity is ArchiveIssueSeverity.WARNING
            ),
        )

    def replace(
        self,
        archive: dict[str, Any],
        benchpup_version: str,
        *,
        fingerprint: ArchiveFingerprint | None = None,
    ) -> Path:
        """Preserve the legacy safety-path return shape used by the CLI."""

        result = self.replace_typed(
            archive,
            benchpup_version,
            fingerprint=fingerprint,
        )
        return result.safety_backup_path

    def replace_file(
        self,
        path: str | Path,
        benchpup_version: str,
        *,
        fingerprint: ArchiveFingerprint | None = None,
    ) -> ArchiveReplaceResult:
        archive_path = self._resolved_file_path(path)
        expected = fingerprint or self.fingerprint(archive_path)
        self._verify_fingerprint(archive_path, expected)
        archive = self._read_archive_file(archive_path)
        self._verify_fingerprint(archive_path, expected)
        return self.replace_typed(
            archive,
            benchpup_version,
            fingerprint=expected,
            archive_path=archive_path,
        )

    def replace_typed(
        self,
        archive: dict[str, Any],
        benchpup_version: str,
        *,
        fingerprint: ArchiveFingerprint | None = None,
        archive_path: Path | None = None,
        maintenance_timeout: float = 5.0,
    ) -> ArchiveReplaceResult:
        self.last_restore_report = None
        selected = self.inspect_archive(
            archive,
            archive_path=archive_path,
            fingerprint=fingerprint,
        )
        self._require_valid(selected)
        if archive_path is not None and fingerprint is not None:
            self._verify_fingerprint(archive_path, fingerprint)

        original = self._resolved_file_path(self.database.path)
        if not original.exists() or not original.is_file():
            self._raise_error(
                f"Active database file was not found: {original}",
                ArchiveIssueCode.FILE_MISSING,
                ArchivePersistenceError,
            )
        parent = original.parent
        safety_path: Path | None = None
        raw_stage: Path | None = None
        candidate: Path | None = None
        sidecar_stages: list[tuple[Path, Path]] = []
        swapped = False
        warnings: list[ArchiveIssue] = []
        failed_replacement: Path | None = None

        try:
            with self.database.maintenance(timeout=maintenance_timeout):
                if archive_path is not None and fingerprint is not None:
                    self._verify_fingerprint(archive_path, fingerprint)

                sidecars = self._prepare_journal_for_swap(original)
                current_archive = self.build_archive(
                    benchpup_version,
                    allow_during_maintenance=True,
                )
                current_preview = self.inspect_archive(current_archive)
                self._require_valid(current_preview)

                safety_path = self._reserve_path(
                    parent,
                    prefix=f"{original.stem}.pre-restore-",
                    suffix=".json",
                )
                try:
                    self._write_archive(
                        current_archive,
                        safety_path,
                        create_parent=False,
                        exclusive=True,
                    )
                    safety_preview = self.inspect(safety_path)
                    if not safety_preview.valid:
                        first = self._first_error(safety_preview)
                        message = (
                            first.message
                            if first is not None
                            else "the validation result contained no error detail"
                        )
                        self._raise_error(
                            f"Safety backup validation failed: {message}",
                            ArchiveIssueCode.SAFETY_BACKUP_INVALID,
                            ArchiveValidationError,
                            issue=first,
                        )
                except ArchiveError as error:
                    if error.code is ArchiveIssueCode.SAFETY_BACKUP_INVALID:
                        raise
                    self._raise_error(
                        f"Could not create the pre-restore safety backup: {error}",
                        ArchiveIssueCode.SAFETY_BACKUP_FAILED,
                        ArchivePersistenceError,
                        error,
                    )

                raw_stage = self._stage_file(
                    original,
                    parent,
                    prefix=f".{original.stem}.original-",
                    suffix=original.suffix,
                )
                sidecar_stages = self._stage_sidecars(sidecars, parent)

                candidate = self._reserve_path(
                    parent,
                    prefix=f".{original.stem}.restore-",
                    suffix=original.suffix,
                )
                candidate_database = EngineDatabase(candidate)
                try:
                    try:
                        candidate_database.migrate()
                    except sqlite3.OperationalError as error:
                        self._raise_error(
                            f"Temporary replacement database migration failed: {error}",
                            ArchiveIssueCode.MIGRATION_FAILED,
                            ArchivePersistenceError,
                            error,
                        )
                    except sqlite3.DatabaseError as error:
                        self._raise_error(
                            f"Temporary replacement database migration failed: {error}",
                            ArchiveIssueCode.MIGRATION_FAILED,
                            ArchivePersistenceError,
                            error,
                        )
                    candidate_result = ArchiveService(candidate_database).merge_typed(archive)
                    self._verify_database(candidate_database)
                    candidate_database.close()
                    self._fsync_path(candidate)
                    os.replace(candidate, original)
                    candidate = None
                    swapped = True
                except ArchiveError:
                    candidate_database.close()
                    raise
                except sqlite3.OperationalError as error:
                    candidate_database.close()
                    self._raise_sqlite_error("Temporary replacement database failed", error)
                except sqlite3.DatabaseError as error:
                    candidate_database.close()
                    self._raise_error(
                        f"Temporary replacement database failed: {error}",
                        ArchiveIssueCode.TEMPORARY_DATABASE_FAILED,
                        ArchivePersistenceError,
                        error,
                    )
                except OSError as error:
                    candidate_database.close()
                    self._raise_error(
                        f"Active database replacement failed: {error}",
                        self._filesystem_code(error, replacement=True),
                        ArchivePersistenceError,
                        error,
                    )

                try:
                    self.database.migrate(allow_during_maintenance=True)
                    self._verify_database(
                        self.database,
                        allow_during_maintenance=True,
                    )
                except Exception as error:
                    recovery_succeeded, failed_replacement = self._recover_original(
                        original,
                        raw_stage,
                        sidecar_stages,
                        parent,
                        allow_during_maintenance=True,
                    )
                    raw_stage = None
                    sidecar_stages = []
                    if recovery_succeeded:
                        self._raise_error(
                            "Replacement database failed reopen verification; the original database was restored",
                            ArchiveIssueCode.REOPEN_FAILED,
                            ArchivePersistenceError,
                            error,
                            recovery_status=ArchiveRecoveryStatus.SUCCEEDED,
                            safety_backup_path=safety_path,
                            recovery_path=failed_replacement,
                        )
                    self._raise_error(
                        "Replacement database failed reopen verification and recovery failed",
                        ArchiveIssueCode.RECOVERY_FAILED,
                        ArchivePersistenceError,
                        error,
                        recovery_status=ArchiveRecoveryStatus.FAILED,
                        safety_backup_path=safety_path,
                    )

                self.last_restore_report = candidate_result.report
                warnings.extend(self._cleanup_staged_paths(raw_stage, sidecar_stages))
                raw_stage = None
                sidecar_stages = []
                return ArchiveReplaceResult(
                    report=candidate_result.report,
                    safety_backup_path=cast(Path, safety_path),
                    recovery_status=ArchiveRecoveryStatus.NOT_ATTEMPTED,
                    restart_required=False,
                    warnings=tuple(warnings),
                )
        except ArchiveError:
            if not swapped:
                self._restore_sidecars(sidecar_stages)
            raise
        except DatabaseMaintenanceError as error:
            self._raise_error(
                "The active database could not be quiesced for replacement",
                ArchiveIssueCode.ACTIVE_DATABASE_BUSY,
                ArchiveBusyError,
                error,
                safety_backup_path=safety_path,
            )
        except sqlite3.OperationalError as error:
            self._raise_sqlite_error("Active database replacement failed", error)
        except sqlite3.DatabaseError as error:
            self._raise_error(
                f"Active database replacement failed: {error}",
                ArchiveIssueCode.ACTIVE_REPLACE_FAILED,
                ArchivePersistenceError,
                error,
                safety_backup_path=safety_path,
            )
        except OSError as error:
            self._raise_error(
                f"Active database replacement failed: {error}",
                self._filesystem_code(error, replacement=True),
                ArchivePersistenceError,
                error,
                safety_backup_path=safety_path,
            )
        finally:
            if candidate is not None:
                self._safe_unlink(candidate)
            if raw_stage is not None:
                self._safe_unlink(raw_stage)
            if sidecar_stages:
                self._restore_sidecars(sidecar_stages)

    @staticmethod
    def _valid_timestamp(value: str) -> bool:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None

    @staticmethod
    def _database_scalar(value: Any) -> bool:
        if value is None or isinstance(value, (str, int, bool)):
            return True
        if isinstance(value, float):
            return math.isfinite(value)
        return False

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _find_existing(
        connection: sqlite3.Connection,
        table: str,
        record: dict[str, Any],
    ) -> int | None:
        if table == "review_scores" and record.get("run_id") is not None:
            row = connection.execute(
                "SELECT id FROM review_scores WHERE run_id = ?",
                (record["run_id"],),
            ).fetchone()
            return row["id"] if row else None
        if table == "run_attachments" and record.get("run_id") is not None:
            row = connection.execute(
                "SELECT id FROM run_attachments WHERE run_id = ? AND file_path = ? AND original_filename = ?",
                (
                    record["run_id"],
                    record.get("file_path", ""),
                    record.get("original_filename", ""),
                ),
            ).fetchone()
            return row["id"] if row else None
        if table == "scoreboard_entries":
            row = connection.execute(
                "SELECT id FROM scoreboard_entries WHERE model_name = ? AND imported_at = ? AND import_batch_id IS ?",
                (
                    record.get("model_name", ""),
                    record.get("imported_at", ""),
                    record.get("import_batch_id"),
                ),
            ).fetchone()
            return row["id"] if row else None
        unique = UNIQUE.get(table)
        if not unique or any(record.get(field) in (None, "") for field in unique):
            return None
        where = " AND ".join(f"{field} = ?" for field in unique)
        row = connection.execute(
            f"SELECT id FROM {table} WHERE {where}",
            [record[field] for field in unique],
        ).fetchone()
        return row["id"] if row else None

    def _merge_in_connection(
        self,
        connection: sqlite3.Connection,
        archive: dict[str, Any],
    ) -> RestoreReport:
        report = RestoreReport()
        id_maps: dict[str, dict[int, int]] = {table: {} for table in TABLES}
        available = {table: self._columns(connection, table) for table in TABLES}
        for table in TABLES:
            for source in archive["data"].get(table, []):
                source_id = cast(int, source["id"])
                record = {
                    key: value
                    for key, value in source.items()
                    if key in available[table] and key != "id"
                }
                remapped_record = False
                for field_name, parent in DEPENDENCIES.get(table, {}).items():
                    old_id = source.get(field_name)
                    if old_id is None:
                        record[field_name] = None
                    elif old_id in id_maps[parent]:
                        new_id = id_maps[parent][old_id]
                        record[field_name] = new_id
                        remapped_record = remapped_record or new_id != old_id
                    else:
                        self._raise_error(
                            f"{table} references missing {parent} record {old_id}",
                            ArchiveIssueCode.INVALID_RELATIONSHIP,
                            ArchiveValidationError,
                        )
                existing_id = self._find_existing(connection, table, record)
                if existing_id is not None:
                    id_maps[table][source_id] = existing_id
                    report.skipped[table] += 1
                    if remapped_record:
                        report.remapped[table] += 1
                    continue
                if not record:
                    self._raise_error(
                        f"{table} contains no supported fields",
                        ArchiveIssueCode.INVALID_RECORD,
                        ArchiveValidationError,
                    )
                columns = list(record)
                cursor = connection.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    [record[column] for column in columns],
                )
                inserted_id = cursor.lastrowid
                if inserted_id is None:
                    self._raise_error(
                        f"Insert into {table} did not return an ID",
                        ArchiveIssueCode.MERGE_FAILED,
                        ArchivePersistenceError,
                    )
                id_maps[table][source_id] = int(inserted_id)
                report.created[table] += 1
                if remapped_record:
                    report.remapped[table] += 1
        return report

    def _verify_database(
        self,
        database: EngineDatabase,
        *,
        allow_during_maintenance: bool = False,
    ) -> None:
        try:
            database.migrate(allow_during_maintenance=allow_during_maintenance)
            with database.connection(
                allow_during_maintenance=allow_during_maintenance
            ) as connection:
                self._verify_database_connection(connection)
                for table in ("benchmark_runs", "review_scores", "run_attachments"):
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        except ArchiveError:
            raise
        except DatabaseMaintenanceError:
            raise
        except sqlite3.IntegrityError as error:
            self._raise_error(
                f"Database integrity validation failed: {error}",
                ArchiveIssueCode.INTEGRITY_FAILED,
                ArchivePersistenceError,
                error,
            )
        except sqlite3.OperationalError as error:
            self._raise_sqlite_error("Database reopen validation failed", error)
        except sqlite3.DatabaseError as error:
            self._raise_error(
                f"Database reopen validation failed: {error}",
                ArchiveIssueCode.TEMPORARY_DATABASE_INVALID,
                ArchivePersistenceError,
                error,
            )

    @staticmethod
    def _verify_database_connection(connection: sqlite3.Connection) -> None:
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ArchivePersistenceError(
                "Restored archive contains invalid relationships",
                code=ArchiveIssueCode.FOREIGN_KEY_FAILED,
            )
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if not integrity or any(str(row[0]).casefold() != "ok" for row in integrity):
            raise ArchivePersistenceError(
                "Database integrity check failed",
                code=ArchiveIssueCode.INTEGRITY_FAILED,
            )

    def _prepare_journal_for_swap(self, original: Path) -> tuple[Path, ...]:
        sidecar_candidates = (
            Path(f"{original}-wal"),
            Path(f"{original}-shm"),
        )
        sidecars_before = tuple(
            candidate for candidate in sidecar_candidates if candidate.exists()
        )
        try:
            with self.database.connection(allow_during_maintenance=True) as connection:
                row = connection.execute("PRAGMA journal_mode").fetchone()
                journal_mode = str(row[0]).casefold() if row else "delete"
                if journal_mode == "wal":
                    try:
                        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    except sqlite3.OperationalError as error:
                        self._raise_error(
                            f"The active database WAL could not be checkpointed: {error}",
                            ArchiveIssueCode.WAL_CHECKPOINT_FAILED,
                            ArchiveBusyError,
                            error,
                        )
                    if result is not None and int(result[0]) != 0:
                        self._raise_error(
                            "The active database WAL could not be checkpointed",
                            ArchiveIssueCode.WAL_CHECKPOINT_FAILED,
                            ArchiveBusyError,
                        )
                elif sidecars_before:
                    self._raise_error(
                        "Unexpected SQLite WAL/SHM sidecars are present; replacement was blocked",
                        ArchiveIssueCode.WAL_CHECKPOINT_FAILED,
                        ArchiveBusyError,
                    )
        except ArchiveError:
            raise
        except sqlite3.OperationalError as error:
            self._raise_sqlite_error("The active database journal could not be prepared", error)
        return tuple(candidate for candidate in sidecar_candidates if candidate.exists())

    def _recover_original(
        self,
        original: Path,
        raw_stage: Path | None,
        sidecar_stages: list[tuple[Path, Path]],
        parent: Path,
        *,
        allow_during_maintenance: bool,
    ) -> tuple[bool, Path | None]:
        failed_replacement: Path | None = None
        try:
            if original.exists():
                failed_replacement = self._reserve_path(
                    parent,
                    prefix=f".{original.stem}.failed-replacement-",
                    suffix=original.suffix,
                )
                os.replace(original, failed_replacement)
            for sidecar_path in (Path(f"{original}-wal"), Path(f"{original}-shm")):
                if sidecar_path.exists():
                    failed_sidecar = self._reserve_path(
                        parent,
                        prefix=f".{sidecar_path.name}.failed-replacement-",
                        suffix=".sidecar",
                    )
                    os.replace(sidecar_path, failed_sidecar)
            if raw_stage is None or not raw_stage.exists():
                self._raise_error(
                    "The original database recovery copy is missing",
                    ArchiveIssueCode.RECOVERY_FAILED,
                    ArchivePersistenceError,
                )
            os.replace(raw_stage, original)
            for sidecar_path, staged_path in sidecar_stages:
                if staged_path.exists():
                    os.replace(staged_path, sidecar_path)
            self._verify_database(
                self.database,
                allow_during_maintenance=allow_during_maintenance,
            )
            return True, failed_replacement
        except Exception:
            recovery_path = (
                raw_stage
                if raw_stage is not None and raw_stage.exists()
                else failed_replacement
            )
            return False, recovery_path

    def _stage_sidecars(
        self,
        sidecars: tuple[Path, ...],
        parent: Path,
    ) -> list[tuple[Path, Path]]:
        staged: list[tuple[Path, Path]] = []
        try:
            for sidecar in sidecars:
                destination = self._reserve_path(
                    parent,
                    prefix=f".{sidecar.name}.old-",
                    suffix=".sidecar",
                )
                os.replace(sidecar, destination)
                staged.append((sidecar, destination))
        except OSError as error:
            self._restore_sidecars(staged)
            self._raise_error(
                f"SQLite journal sidecars could not be staged safely: {error}",
                self._filesystem_code(error, replacement=True),
                ArchivePersistenceError,
                error,
            )
        return staged

    @staticmethod
    def _restore_sidecars(staged: list[tuple[Path, Path]]) -> None:
        for original, temporary in reversed(staged):
            if temporary.exists() and not original.exists():
                try:
                    os.replace(temporary, original)
                except OSError:
                    pass

    def _cleanup_staged_paths(
        self,
        raw_stage: Path | None,
        sidecar_stages: list[tuple[Path, Path]],
    ) -> list[ArchiveIssue]:
        warnings: list[ArchiveIssue] = []
        if raw_stage is not None and raw_stage.exists():
            try:
                raw_stage.unlink()
            except OSError as error:
                warnings.append(
                    ArchiveIssue(
                        ArchiveIssueCode.CLEANUP_FAILED,
                        ArchiveIssueSeverity.WARNING,
                        f"Temporary recovery copy was retained at {raw_stage}: {error}",
                        field="recovery_path",
                        actual=str(raw_stage),
                    )
                )
        for _original, staged in sidecar_stages:
            if staged.exists():
                try:
                    staged.unlink()
                except OSError as error:
                    warnings.append(
                        ArchiveIssue(
                            ArchiveIssueCode.CLEANUP_FAILED,
                            ArchiveIssueSeverity.WARNING,
                            f"Old SQLite sidecar was retained at {staged}: {error}",
                            field="sidecar_path",
                            actual=str(staged),
                        )
                    )
        return warnings

    def _stage_file(
        self,
        source: Path,
        parent: Path,
        *,
        prefix: str,
        suffix: str,
    ) -> Path:
        destination = self._reserve_path(parent, prefix=prefix, suffix=suffix)
        try:
            shutil.copy2(source, destination)
            self._fsync_path(destination)
            return destination
        except OSError as error:
            self._safe_unlink(destination)
            self._raise_error(
                f"Could not stage the active database safely: {error}",
                ArchiveIssueCode.SAFETY_BACKUP_FAILED,
                ArchivePersistenceError,
                error,
            )

    @staticmethod
    def _reserve_path(parent: Path, *, prefix: str, suffix: str) -> Path:
        try:
            descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=parent)
            os.close(descriptor)
            path = Path(name)
            path.unlink(missing_ok=True)
            return path
        except PermissionError as error:
            raise ArchivePersistenceError(
                f"Could not create a temporary archive path in {parent}: access denied",
                code=ArchiveIssueCode.PERMISSION_DENIED,
            ) from error
        except OSError as error:
            raise ArchivePersistenceError(
                f"Could not create a temporary archive path in {parent}: {error}",
                code=ArchiveIssueCode.INVALID_DESTINATION,
            ) from error

    def _write_archive(
        self,
        archive: dict[str, Any],
        destination: Path,
        *,
        create_parent: bool,
        exclusive: bool = False,
    ) -> None:
        self._validate_destination(destination, create_parent=create_parent)
        if create_parent:
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
            except PermissionError as error:
                self._raise_error(
                    f"Archive destination folder could not be created: {destination.parent}",
                    ArchiveIssueCode.PERMISSION_DENIED,
                    ArchivePersistenceError,
                    error,
                )
            except OSError as error:
                self._raise_error(
                    f"Archive destination folder could not be created: {error}",
                    ArchiveIssueCode.INVALID_DESTINATION,
                    ArchivePersistenceError,
                    error,
                )
        descriptor: int | None = None
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                descriptor = None
                json.dump(
                    archive,
                    output,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                output.write("\n")
                output.flush()
                self._fsync_file(output.fileno())
            try:
                loaded = self._read_archive_file(temporary)
            except ArchiveError as error:
                self._raise_error(
                    f"Temporary archive validation could not read the staged file: {error}",
                    ArchiveIssueCode.TEMPORARY_VALIDATION_FAILED,
                    ArchiveValidationError,
                    error,
                    issue=error.issue,
                )
            temporary_preview = self.inspect_archive(loaded, archive_path=temporary)
            self._require_valid(temporary_preview, temporary=True)
            if exclusive:
                self._install_exclusive(temporary, destination)
            else:
                os.replace(temporary, destination)
            temporary = None
            self._flush_parent_directory(destination.parent)
        except ArchiveError:
            raise
        except (TypeError, ValueError) as error:
            self._raise_error(
                f"Archive serialization failed: {error}",
                ArchiveIssueCode.SERIALIZATION_FAILED,
                ArchivePersistenceError,
                error,
            )
        except PermissionError as error:
            self._raise_error(
                f"Archive destination could not be replaced because access was denied: {destination}",
                ArchiveIssueCode.DESTINATION_LOCKED
                if destination.exists()
                else ArchiveIssueCode.PERMISSION_DENIED,
                ArchivePersistenceError,
                error,
            )
        except OSError as error:
            self._raise_error(
                f"Archive destination could not be replaced: {error}",
                ArchiveIssueCode.ATOMIC_REPLACE_FAILED,
                ArchivePersistenceError,
                error,
            )
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                self._safe_unlink(temporary)

    @staticmethod
    def _install_exclusive(temporary: Path, destination: Path) -> None:
        """Publish a file without replacing an existing path.

        ``os.replace`` is intentionally retained for normal exports, where
        replacing the selected destination is the legacy CLI behavior.  A
        safety backup has stronger semantics: an existing backup must never
        be overwritten.  Windows rename is non-overwriting; on POSIX a hard
        link provides the same atomic no-replace publication.
        """

        try:
            if os.name == "nt":
                os.rename(temporary, destination)
            else:
                os.link(temporary, destination)
                temporary.unlink()
        except FileExistsError as error:
            raise ArchivePersistenceError(
                f"Archive destination already exists: {destination}",
                code=ArchiveIssueCode.DESTINATION_LOCKED,
            ) from error
        except OSError as error:
            raise ArchivePersistenceError(
                f"Archive destination could not be published exclusively: {error}",
                code=ArchiveIssueCode.ATOMIC_REPLACE_FAILED,
            ) from error

    @staticmethod
    def _fsync_file(descriptor: int) -> None:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if getattr(error, "errno", None) in {
                errno.EINVAL,
                errno.ENOSYS,
                getattr(errno, "ENOTSUP", -1),
            }:
                return
            raise

    def _fsync_path(self, path: Path) -> None:
        try:
            # Windows may reject fsync on a read-only handle.  A writable
            # handle keeps this durability check meaningful for staged DBs
            # while remaining read-only at the data level.
            with path.open("r+b") as source:
                self._fsync_file(source.fileno())
        except OSError as error:
            if getattr(error, "errno", None) in {
                errno.EINVAL,
                errno.ENOSYS,
                getattr(errno, "ENOTSUP", -1),
            }:
                return
            raise

    @staticmethod
    def _flush_parent_directory(parent: Path) -> None:
        if os.name == "nt":
            return
        descriptor: int | None = None
        try:
            descriptor = os.open(parent, os.O_RDONLY)
            os.fsync(descriptor)
        except OSError:
            # Directory fsync is not available on every supported filesystem;
            # the file itself was already fsynced before replacement.
            return
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _validate_destination(
        self,
        path: str | Path,
        *,
        create_parent: bool,
    ) -> Path:
        try:
            destination = Path(path).expanduser()
        except (TypeError, ValueError) as error:
            self._raise_error(
                "Archive destination is not a valid path",
                ArchiveIssueCode.INVALID_DESTINATION,
                ArchivePersistenceError,
                error,
            )
        if destination.exists() and destination.is_dir():
            self._raise_error(
                f"Archive destination is a directory: {destination}",
                ArchiveIssueCode.PATH_IS_DIRECTORY,
                ArchivePersistenceError,
            )
        parent = destination.parent
        if parent.exists() and not parent.is_dir():
            self._raise_error(
                f"Archive destination parent is not a directory: {parent}",
                ArchiveIssueCode.INVALID_DESTINATION,
                ArchivePersistenceError,
            )
        if not parent.exists() and not create_parent:
            self._raise_error(
                f"Archive destination folder does not exist: {parent}",
                ArchiveIssueCode.INVALID_DESTINATION,
                ArchivePersistenceError,
            )
        return destination

    def _read_archive_file(self, path: str | Path) -> Any:
        archive_path = self._resolved_file_path(path)
        try:
            if archive_path.is_dir():
                self._raise_issue(
                    ArchiveIssue(
                        ArchiveIssueCode.PATH_IS_DIRECTORY,
                        ArchiveIssueSeverity.ERROR,
                        f"Archive path is a directory: {archive_path}",
                        field="archive_path",
                        actual=str(archive_path),
                    ),
                    ArchiveReadError,
                )
            with archive_path.open("r", encoding="utf-8") as source:
                return json.load(source)
        except ArchiveError:
            raise
        except FileNotFoundError as error:
            self._raise_error(
                f"Could not read archive; file was not found: {archive_path}",
                ArchiveIssueCode.FILE_MISSING,
                ArchiveReadError,
                error,
            )
        except PermissionError as error:
            self._raise_error(
                f"Could not read archive because access was denied: {archive_path}",
                ArchiveIssueCode.PERMISSION_DENIED,
                ArchiveReadError,
                error,
            )
        except UnicodeDecodeError as error:
            self._raise_error(
                f"Could not decode archive as UTF-8: {error}",
                ArchiveIssueCode.MALFORMED_JSON,
                ArchiveReadError,
                error,
            )
        except json.JSONDecodeError as error:
            self._raise_error(
                f"Could not read archive: {error}",
                ArchiveIssueCode.MALFORMED_JSON,
                ArchiveReadError,
                error,
            )
        except OSError as error:
            self._raise_error(
                f"Could not read archive: {error}",
                ArchiveIssueCode.READ_FAILED,
                ArchiveReadError,
                error,
            )

    @staticmethod
    def _resolved_file_path(path: str | Path) -> Path:
        try:
            return Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise ArchiveReadError(
                f"Archive path is not valid: {path}",
                code=ArchiveIssueCode.READ_FAILED,
            ) from error

    def _verify_fingerprint(self, path: Path, expected: ArchiveFingerprint) -> None:
        current = self.fingerprint(path)
        if current != expected:
            self._raise_error(
                f"Archive changed since it was inspected: {path}",
                ArchiveIssueCode.STALE_ARCHIVE,
                ArchiveValidationError,
            )

    def _require_valid(self, preview: ArchivePreview, *, temporary: bool = False) -> None:
        issue = self._first_error(preview)
        if issue is None:
            return
        code = (
            ArchiveIssueCode.TEMPORARY_VALIDATION_FAILED
            if temporary
            else issue.code
        )
        self._raise_error(
            issue.message,
            code,
            ArchiveValidationError,
            issue=issue,
        )

    @staticmethod
    def _first_error(preview: ArchivePreview) -> ArchiveIssue | None:
        return next(
            (
                issue
                for issue in preview.issues
                if issue.severity is ArchiveIssueSeverity.ERROR
            ),
            None,
        )

    def _empty_preview(
        self,
        *,
        archive_path: Path | None,
        fingerprint: ArchiveFingerprint | None,
        issues: tuple[ArchiveIssue, ...],
    ) -> ArchivePreview:
        zero_counts = {table: 0 for table in TABLES}
        return ArchivePreview(
            validation_state=ArchiveValidationState.INVALID,
            compatibility_state=ArchiveCompatibilityState.UNSUPPORTED,
            archive_path=archive_path,
            fingerprint=fingerprint,
            format_identifier=None,
            archive_version=None,
            created_at=None,
            benchpup_version=None,
            archive_schema_version=None,
            current_schema_version=CURRENT_SCHEMA_VERSION,
            declared_counts=cast(
                Mapping[str, int | None],
                _frozen_mapping({table: None for table in TABLES}),
            ),
            actual_counts=cast(Mapping[str, int], _frozen_mapping(zero_counts)),
            total_record_count=0,
            attachment_metadata_count=0,
            issues=issues,
            merge_eligible=False,
            replace_eligible=False,
        )

    @staticmethod
    def _filesystem_code(error: OSError, *, replacement: bool = False) -> ArchiveIssueCode:
        if isinstance(error, PermissionError):
            return ArchiveIssueCode.PERMISSION_DENIED
        if getattr(error, "errno", None) in {errno.EACCES, errno.EPERM}:
            return ArchiveIssueCode.PERMISSION_DENIED
        return (
            ArchiveIssueCode.ACTIVE_REPLACE_FAILED
            if replacement
            else ArchiveIssueCode.ATOMIC_REPLACE_FAILED
        )

    def _raise_sqlite_error(self, prefix: str, error: sqlite3.OperationalError) -> NoReturn:
        text = str(error).casefold()
        if "locked" in text:
            code = ArchiveIssueCode.DATABASE_LOCKED
            error_type: type[ArchiveError] = ArchiveBusyError
        elif "busy" in text:
            code = ArchiveIssueCode.DATABASE_BUSY
            error_type = ArchiveBusyError
        else:
            code = ArchiveIssueCode.MERGE_FAILED
            error_type = ArchivePersistenceError
        self._raise_error(f"{prefix}: {error}", code, error_type, error)

    @staticmethod
    def _raise_issue(issue: ArchiveIssue, error_type: type[ArchiveError]) -> NoReturn:
        raise error_type(issue.message, code=issue.code, issue=issue)

    @staticmethod
    def _raise_error(
        message: str,
        code: ArchiveIssueCode,
        error_type: type[ArchiveError],
        cause: BaseException | None = None,
        *,
        issue: ArchiveIssue | None = None,
        recovery_status: ArchiveRecoveryStatus = ArchiveRecoveryStatus.NOT_ATTEMPTED,
        safety_backup_path: Path | None = None,
        recovery_path: Path | None = None,
    ) -> NoReturn:
        error = error_type(
            message,
            code=code,
            issue=issue,
            recovery_status=recovery_status,
            safety_backup_path=safety_backup_path,
            recovery_path=recovery_path,
        )
        if cause is None:
            raise error
        raise error from cause

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = (
    "ARCHIVE_FORMAT",
    "ARCHIVE_VERSION",
    "CURRENT_SCHEMA_VERSION",
    "TABLES",
    "ArchiveCompatibilityState",
    "ArchiveError",
    "ArchiveExportResult",
    "ArchiveFingerprint",
    "ArchiveIssue",
    "ArchiveIssueCode",
    "ArchiveIssueSeverity",
    "ArchiveMergeResult",
    "ArchivePersistenceError",
    "ArchivePreview",
    "ArchiveReadError",
    "ArchiveRecoveryStatus",
    "ArchiveReplaceResult",
    "ArchiveService",
    "ArchiveValidationError",
    "ArchiveValidationState",
    "ArchiveBusyError",
    "RestoreReport",
)
