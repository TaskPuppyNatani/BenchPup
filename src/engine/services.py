from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, TypeVar

from .database import EngineDatabase
from .domain import BenchmarkDefinition, BenchmarkRun, BenchmarkSession, ExportProfile, HardwareProfile, ModelProfile, PromptTemplate, ReviewScore, RunAttachment, ScoreboardEntry, ScoreboardImportBatch, now, prompt_hash_for, resolve_prompt_text
from .hardware_importers import HardwareProfileDraft
from .repositories import Repository

_T = TypeVar("_T")


def is_database_integrity_error(error: BaseException) -> bool:
    """Expose the engine's persistence-conflict classification without leaking it into UI code."""

    return isinstance(error, sqlite3.IntegrityError)


class AttachmentStorageError(ValueError):
    """A user-actionable failure while resolving an attachment source or copy."""


class HardwareProfileImportValidationError(ValueError):
    """An imported hardware profile has no meaningful hardware values."""


class HardwareProfileNameConflictError(ValueError):
    """An imported hardware profile name collides after normalization."""


HardwareProfileConflictReason = Literal["name", "computer_name", "cpu_gpu"]


@dataclass(frozen=True)
class HardwareProfileConflict:
    """One existing hardware profile matched an imported candidate."""

    profile: HardwareProfile
    reasons: tuple[HardwareProfileConflictReason, ...]


class CatalogService:
    """Typed, UI-independent operations for reusable catalog records."""

    def __init__(self, database: EngineDatabase):
        self.database = database
        self.sessions = Repository(database, "benchmark_sessions", BenchmarkSession, bool_fields={"is_deleted"})
        self.model_profiles = Repository(database, "model_profiles", ModelProfile, bool_fields={"thinking_enabled", "flash_attention", "is_default"})
        self.hardware_profiles = Repository(database, "hardware_profiles", HardwareProfile, json_fields={"backend_versions"})
        self.benchmark_definitions = Repository(database, "benchmark_definitions", BenchmarkDefinition, bool_fields={"is_active"})
        self.prompt_templates = Repository(database, "prompt_templates", PromptTemplate, bool_fields={"is_active"})
        self.export_profiles = Repository(database, "export_profiles", ExportProfile, json_fields={"field_selection", "filter_json"})
        self.scoreboard_entries = Repository(database, "scoreboard_entries", ScoreboardEntry, bool_fields={"is_deleted"})
        self.scoreboard_import_batches = Repository(database, "scoreboard_import_batches", ScoreboardImportBatch, bool_fields={"is_deleted"})

    @staticmethod
    def _record_id(record: object) -> int:
        value = getattr(record, "id", None)
        return int(value) if value is not None else -1

    @staticmethod
    def _ordered(records: list[_T], label: Any) -> list[_T]:
        return sorted(
            records,
            key=lambda record: (
                str(label(record)).casefold(),
                str(label(record)),
                CatalogService._record_id(record),
            ),
        )

    def list_sessions(self, *, include_deleted: bool = False) -> list[BenchmarkSession]:
        return self._ordered(self.sessions.list(include_deleted=include_deleted), lambda record: record.title)

    def get_session(self, session_id: int) -> BenchmarkSession | None:
        return self.sessions.get(session_id)

    def create_session(self, session: BenchmarkSession) -> BenchmarkSession:
        return self.sessions.create(session)

    def update_session(self, session: BenchmarkSession) -> BenchmarkSession:
        return self.sessions.update(session)

    def archive_session(self, session_id: int) -> BenchmarkSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"benchmark_sessions {session_id} does not exist")
        if session.is_deleted:
            return session
        return self.sessions.update(replace(session, is_deleted=True))

    def restore_session(self, session_id: int) -> BenchmarkSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"benchmark_sessions {session_id} does not exist")
        if not session.is_deleted:
            return session
        return self.sessions.update(replace(session, is_deleted=False))

    def session_run_counts(self) -> dict[int, int]:
        """Return ordinary, non-deleted run counts grouped by session."""

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT session_id, COUNT(*) AS run_count "
                "FROM benchmark_runs "
                "WHERE session_id IS NOT NULL AND is_deleted = 0 "
                "GROUP BY session_id"
            )
            return {int(row["session_id"]): int(row["run_count"]) for row in rows}

    def list_model_profiles(self) -> list[ModelProfile]:
        return self._ordered(self.model_profiles.list(), lambda record: record.name)

    def get_model_profile(self, profile_id: int) -> ModelProfile | None:
        return self.model_profiles.get(profile_id)

    def save_model_profile(self, profile: ModelProfile, *, make_default: bool | None = None) -> ModelProfile:
        """Create or update a profile while enforcing one engine-owned default."""

        desired_default = profile.is_default if make_default is None else make_default
        with self.database.connection() as connection:
            if desired_default:
                if profile.id is None:
                    connection.execute("UPDATE model_profiles SET is_default = 0")
                else:
                    connection.execute("UPDATE model_profiles SET is_default = 0 WHERE id <> ?", (profile.id,))
            replacement = replace(profile, is_default=desired_default)
            if replacement.id is None:
                return self.model_profiles.create_in_connection(replacement, connection)
            return self.model_profiles.update_in_connection(replacement, connection)

    def create_model_profile(self, profile: ModelProfile, *, make_default: bool | None = None) -> ModelProfile:
        return self.save_model_profile(profile, make_default=make_default)

    def update_model_profile(self, profile: ModelProfile, *, make_default: bool | None = None) -> ModelProfile:
        return self.save_model_profile(profile, make_default=make_default)

    def set_default_model_profile(self, profile_id: int) -> ModelProfile:
        profile = self.model_profiles.get(profile_id)
        if profile is None:
            raise KeyError(f"model_profiles {profile_id} does not exist")
        if profile.is_default:
            return profile
        with self.database.connection() as connection:
            connection.execute("UPDATE model_profiles SET is_default = 0 WHERE id <> ?", (profile_id,))
            return self.model_profiles.update_in_connection(replace(profile, is_default=True), connection)

    def list_benchmark_definitions(self, *, include_inactive: bool = False) -> list[BenchmarkDefinition]:
        records = self.benchmark_definitions.list()
        if not include_inactive:
            records = [record for record in records if record.is_active]
        return self._ordered(records, lambda record: record.name)

    def get_benchmark_definition(self, definition_id: int) -> BenchmarkDefinition | None:
        return self.benchmark_definitions.get(definition_id)

    def create_benchmark_definition(self, definition: BenchmarkDefinition) -> BenchmarkDefinition:
        return self.benchmark_definitions.create(definition)

    def update_benchmark_definition(self, definition: BenchmarkDefinition) -> BenchmarkDefinition:
        return self.benchmark_definitions.update(definition)

    def deactivate_benchmark_definition(self, definition_id: int) -> BenchmarkDefinition:
        definition = self.benchmark_definitions.get(definition_id)
        if definition is None:
            raise KeyError(f"benchmark_definitions {definition_id} does not exist")
        if not definition.is_active:
            return definition
        return self.benchmark_definitions.update(replace(definition, is_active=False))

    def reactivate_benchmark_definition(self, definition_id: int) -> BenchmarkDefinition:
        definition = self.benchmark_definitions.get(definition_id)
        if definition is None:
            raise KeyError(f"benchmark_definitions {definition_id} does not exist")
        if definition.is_active:
            return definition
        return self.benchmark_definitions.update(replace(definition, is_active=True))

    def list_prompt_templates(self, *, include_inactive: bool = False) -> list[PromptTemplate]:
        records = self.prompt_templates.list()
        if not include_inactive:
            records = [record for record in records if record.is_active]
        return self._ordered(records, lambda record: (record.name, record.version))

    def get_prompt_template(self, template_id: int) -> PromptTemplate | None:
        return self.prompt_templates.get(template_id)

    @staticmethod
    def _with_prompt_hash(template: PromptTemplate) -> PromptTemplate:
        """Prepare prompt text through the engine-owned hash contract."""

        return replace(template, prompt_hash=prompt_hash_for(template.prompt_text))

    def create_prompt_template(self, template: PromptTemplate) -> PromptTemplate:
        return self.prompt_templates.create(self._with_prompt_hash(template))

    def update_prompt_template(self, template: PromptTemplate) -> PromptTemplate:
        return self.prompt_templates.update(self._with_prompt_hash(template))

    def deactivate_prompt_template(self, template_id: int) -> PromptTemplate:
        template = self.prompt_templates.get(template_id)
        if template is None:
            raise KeyError(f"prompt_templates {template_id} does not exist")
        if not template.is_active:
            return template
        return self.prompt_templates.update(replace(template, is_active=False))

    def reactivate_prompt_template(self, template_id: int) -> PromptTemplate:
        template = self.prompt_templates.get(template_id)
        if template is None:
            raise KeyError(f"prompt_templates {template_id} does not exist")
        if template.is_active:
            return template
        return self.prompt_templates.update(replace(template, is_active=True))

    def list_hardware_profiles(self) -> list[HardwareProfile]:
        return self._ordered(self.hardware_profiles.list(), lambda record: record.name)

    def get_hardware_profile(self, profile_id: int) -> HardwareProfile | None:
        return self.hardware_profiles.get(profile_id)

    def create_hardware_profile(self, profile: HardwareProfile) -> HardwareProfile:
        return self.hardware_profiles.create(profile)

    def update_hardware_profile(self, profile: HardwareProfile) -> HardwareProfile:
        return self.hardware_profiles.update(profile)

    @staticmethod
    def _normalized_import_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, dict):
            pairs = []
            for key, item in value.items():
                normalized_key = " ".join(str(key).split())
                normalized_item = " ".join(str(item).split())
                if normalized_key and normalized_item:
                    pairs.append(f"{normalized_key}={normalized_item}")
            return " ".join(pairs)
        return " ".join(str(value).split())

    @classmethod
    def has_meaningful_imported_hardware_data(cls, profile: HardwareProfile) -> bool:
        """Return whether an import contains data beyond its generated name/provenance."""

        for field_name in (
            "computer_name",
            "cpu",
            "gpu",
            "vram_gb",
            "ram_gb",
            "operating_system",
            "backend_versions",
        ):
            if cls._normalized_import_value(getattr(profile, field_name)):
                return True
        return False

    def validate_imported_hardware_profile(self, profile: HardwareProfile) -> None:
        """Apply import-only validation without changing manual profile rules."""

        profile.validate()
        if not self._normalize_hardware_match(profile.name):
            raise HardwareProfileImportValidationError("a hardware profile name is required")
        if not self.has_meaningful_imported_hardware_data(profile):
            raise HardwareProfileImportValidationError(
                "at least one meaningful hardware value is required"
            )

    def create_imported_hardware_profile(self, profile: HardwareProfile) -> HardwareProfile:
        """Create an imported profile with normalized-name validation in one transaction."""

        self.validate_imported_hardware_profile(profile)
        normalized_name = self._normalize_hardware_match(profile.name)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_names = connection.execute("SELECT name FROM hardware_profiles").fetchall()
            if any(
                normalized_name == self._normalize_hardware_match(str(row["name"]))
                for row in existing_names
            ):
                raise HardwareProfileNameConflictError(
                    f"a hardware profile named {profile.name!r} already exists"
                )
            return self.hardware_profiles.create_in_connection(profile, connection)

    @staticmethod
    def _normalize_hardware_match(value: str) -> str:
        return " ".join(value.split()).casefold()

    def build_imported_hardware_profile(
        self,
        draft: HardwareProfileDraft,
        *,
        existing: HardwareProfile | None = None,
        imported_at: str | None = None,
    ) -> HardwareProfile:
        """Build imported profile data without writing it to the database."""

        name = draft.name.strip() or draft.computer_name.strip() or f"{draft.source_name} hardware"
        values = {
            "name": name,
            "computer_name": draft.computer_name,
            "cpu": draft.cpu,
            "gpu": draft.gpu,
            "vram_gb": draft.vram_gb,
            "ram_gb": draft.ram_gb,
            "operating_system": draft.operating_system,
            "backend_versions": dict(draft.backend_versions),
            "notes": draft.notes,
            "import_source": draft.source_name,
            "imported_at": imported_at or now(),
        }
        if existing is not None:
            return replace(existing, **values)
        return HardwareProfile(**values)

    def find_hardware_profile_conflicts(
        self,
        candidate: HardwareProfile,
    ) -> tuple[HardwareProfileConflict, ...]:
        """Find deterministic, non-empty identity matches for an imported candidate."""

        normalized = {
            "name": self._normalize_hardware_match(candidate.name),
            "computer_name": self._normalize_hardware_match(candidate.computer_name),
            "cpu": self._normalize_hardware_match(candidate.cpu),
            "gpu": self._normalize_hardware_match(candidate.gpu),
        }
        conflicts: list[HardwareProfileConflict] = []
        for profile in self.list_hardware_profiles():
            if candidate.id is not None and profile.id == candidate.id:
                continue
            reasons: list[HardwareProfileConflictReason] = []
            if normalized["name"] and normalized["name"] == self._normalize_hardware_match(profile.name):
                reasons.append("name")
            if normalized["computer_name"] and normalized["computer_name"] == self._normalize_hardware_match(profile.computer_name):
                reasons.append("computer_name")
            if normalized["cpu"] and normalized["gpu"]:
                if normalized["cpu"] == self._normalize_hardware_match(profile.cpu) and normalized["gpu"] == self._normalize_hardware_match(profile.gpu):
                    reasons.append("cpu_gpu")
            if reasons:
                conflicts.append(HardwareProfileConflict(profile, tuple(reasons)))
        return tuple(conflicts)

    def next_available_hardware_profile_name(self, base_name: str) -> str:
        """Return the first deterministic, case-insensitively unused profile name."""

        base = " ".join(base_name.split()) or "Imported hardware profile"
        names = {
            self._normalize_hardware_match(profile.name)
            for profile in self.list_hardware_profiles()
        }
        if self._normalize_hardware_match(base) not in names:
            return base
        suffix = 2
        while self._normalize_hardware_match(f"{base} ({suffix})") in names:
            suffix += 1
        return f"{base} ({suffix})"


class BenchmarkService:
    def __init__(self, database: EngineDatabase, catalog: CatalogService | None = None):
        self.database = database
        self.catalog = catalog or CatalogService(database)
        snapshot_fields = {"model_snapshot", "benchmark_snapshot", "prompt_snapshot", "hardware_snapshot"}
        self.runs = Repository(
            database,
            "benchmark_runs",
            BenchmarkRun,
            json_fields=snapshot_fields,
            bool_fields={"is_deleted"},
            tolerant_json_fields=snapshot_fields,
        )
        self.scores = Repository(database, "review_scores", ReviewScore)
        self.attachments = Repository(database, "run_attachments", RunAttachment)

    @staticmethod
    def _snapshot(value: Any, omit: set[str] = {"id", "created_at", "updated_at", "is_deleted"}) -> dict[str, Any]:
        return {key: item for key, item in value.__dict__.items() if key not in omit}

    def _resolved_run(self, run: BenchmarkRun) -> BenchmarkRun:
        model = self.catalog.model_profiles.get(run.model_profile_id) if run.model_profile_id else None
        definition = self.catalog.benchmark_definitions.get(run.benchmark_definition_id) if run.benchmark_definition_id else None
        template = self.catalog.prompt_templates.get(run.prompt_template_id) if run.prompt_template_id else None
        hardware = self.catalog.hardware_profiles.get(run.hardware_profile_id) if run.hardware_profile_id else None
        if run.model_profile_id and not model: raise ValueError("model_profile_id does not exist")
        if run.benchmark_definition_id and not definition: raise ValueError("benchmark_definition_id does not exist")
        if run.prompt_template_id and not template: raise ValueError("prompt_template_id does not exist")
        if run.hardware_profile_id and not hardware: raise ValueError("hardware_profile_id does not exist")
        if run.session_id and not self.catalog.sessions.get(run.session_id): raise ValueError("session_id does not exist")
        resolved_prompt_text = resolve_prompt_text(
            run.prompt_text,
            template.prompt_text if template else "",
            definition.default_prompt if definition else "",
        )
        prompt_snapshot = dict(run.prompt_snapshot) if run.prompt_snapshot else (self._snapshot(template) if template else {})
        if template: prompt_snapshot["prompt_text"] = template.prompt_text
        updated = replace(run,
            model_snapshot=run.model_snapshot or (self._snapshot(model) if model else {}),
            benchmark_snapshot=run.benchmark_snapshot or (self._snapshot(definition) if definition else {}),
            prompt_snapshot=prompt_snapshot,
            hardware_snapshot=run.hardware_snapshot or (self._snapshot(hardware) if hardware else {}),
            prompt_text=resolved_prompt_text,
            updated_at=now())
        canonical = {"model": updated.model_snapshot, "benchmark": updated.benchmark_snapshot, "prompt": updated.prompt_snapshot, "hardware": updated.hardware_snapshot, "output": updated.raw_model_output}
        return replace(updated, fingerprint=hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest())

    def save_run(self, run: BenchmarkRun, score: ReviewScore | None = None) -> tuple[BenchmarkRun, ReviewScore | None]:
        """Persist a run and optional review as one engine-owned transaction."""

        return self.save_run_atomic(run, score)

    def save_run_atomic(self, run: BenchmarkRun, score: ReviewScore | None = None) -> tuple[BenchmarkRun, ReviewScore | None]:
        """Create a run and optional review without leaving a partial aggregate."""

        resolved = self._resolved_run(run)
        with self.database.connection() as connection:
            saved = self.runs.create_in_connection(resolved, connection)
            if score is None:
                return saved, None
            if saved.id is None:
                raise ValueError("saved benchmark run is missing its ID")
            saved_score = self.scores.create_in_connection(replace(score, run_id=saved.id), connection)
            return saved, saved_score

    def update_run(self, run: BenchmarkRun) -> BenchmarkRun:
        return self.runs.update(self._resolved_run(run))

    def delete_run(self, run_id: int) -> None: self.runs.delete(run_id, soft=True)

    def get_run(self, run_id: int) -> tuple[BenchmarkRun | None, ReviewScore | None, list[RunAttachment]]:
        run = self.runs.get(run_id)
        if run:
            with self.database.connection() as connection:
                row = connection.execute("SELECT * FROM review_scores WHERE run_id = ?", (run_id,)).fetchone()
                score = self.scores._item(row) if row else None
                attachments = [self.attachments._item(item) for item in connection.execute("SELECT * FROM run_attachments WHERE run_id = ? ORDER BY id", (run_id,))]
            return run, score, attachments
        return None, None, []

    def add_attachment(self, attachment: RunAttachment) -> RunAttachment:
        if not self.runs.get(attachment.run_id): raise ValueError("run_id does not exist")
        return self.attachments.create(attachment)

    def save_attachment_from_source(
        self,
        attachment: RunAttachment,
        *,
        source_path: str | Path | None,
        managed_destination: str | Path | None = None,
    ) -> RunAttachment:
        """Save attachment metadata and optionally copy a selected source file.

        ``source_path`` is intentionally optional for updates: metadata-only
        edits can preserve a missing or inaccessible existing path without
        touching the filesystem.  A non-``None`` ``managed_destination`` is
        the explicit signal to copy the source into managed storage.
        """

        if not self.runs.get(attachment.run_id):
            raise AttachmentStorageError("The benchmark run does not exist.")
        if attachment.id is None and source_path is None:
            raise AttachmentStorageError("A source file is required for a new attachment.")
        if managed_destination is not None and source_path is None:
            raise AttachmentStorageError("Select a source file before copying into managed storage.")

        draft = attachment
        source: Path | None = None
        if source_path is not None:
            source = self._attachment_source(source_path)
            filename = attachment.original_filename if attachment.original_filename.strip() else source.name
            if not filename.strip():
                raise AttachmentStorageError("A filename could not be derived from the selected source file.")
            draft = replace(attachment, file_path=str(source), original_filename=filename)
        elif attachment.id is not None:
            draft = replace(attachment, file_path=str(self._normalize_attachment_path(attachment.file_path)))

        draft.validate()
        if source is not None and managed_destination is not None:
            stored_path = self.copy_attachment_file(source, managed_destination, filename=draft.original_filename)
            draft = replace(draft, file_path=str(stored_path))

        if draft.id is None:
            return self.add_attachment(draft)
        return self.update_attachment(draft)

    @staticmethod
    def _normalize_attachment_path(path_value: str | Path) -> Path:
        try:
            return Path(path_value).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as error:
            raise AttachmentStorageError("The attachment path could not be normalized.") from error

    @staticmethod
    def _attachment_source(source_path: str | Path) -> Path:
        source = BenchmarkService._normalize_attachment_path(source_path)
        try:
            is_file = source.is_file()
        except (OSError, ValueError) as error:
            raise AttachmentStorageError("The selected source file could not be accessed.") from error
        if not is_file:
            raise AttachmentStorageError("The selected source file does not exist or is not a regular file.")
        return source

    @staticmethod
    def copy_attachment_file(
        source_path: str | Path,
        destination_folder: str | Path,
        *,
        filename: str,
    ) -> Path:
        """Copy one attachment without overwriting an existing destination."""

        source = BenchmarkService._attachment_source(source_path)
        try:
            if not str(destination_folder).strip():
                raise AttachmentStorageError("The managed destination folder is required.")
            destination = BenchmarkService._normalize_attachment_path(destination_folder)
            if not destination.is_dir():
                raise AttachmentStorageError("The managed destination folder does not exist.")
            target_name = Path(filename)
            if not filename.strip() or target_name.name != filename:
                raise AttachmentStorageError("Filename must be a file name, not a folder path.")
            target = destination / filename
        except AttachmentStorageError:
            raise
        except (OSError, ValueError) as error:
            raise AttachmentStorageError("The managed destination folder could not be accessed.") from error

        created = False
        try:
            with source.open("rb") as source_handle, target.open("xb") as target_handle:
                created = True
                shutil.copyfileobj(source_handle, target_handle)
            shutil.copystat(source, target)
        except FileExistsError as error:
            raise AttachmentStorageError(
                f"A file named '{filename}' already exists in the managed destination. Nothing was overwritten."
            ) from error
        except (OSError, ValueError) as error:
            cleanup_error: OSError | None = None
            if created:
                try:
                    target.unlink()
                except OSError as cleanup_exception:
                    cleanup_error = cleanup_exception
            message = f"The file could not be copied: {error}"
            if cleanup_error is not None:
                message += f" The partial destination could not be removed: {cleanup_error}"
            raise AttachmentStorageError(message) from error
        return target

    def update_score(self, score: ReviewScore) -> ReviewScore:
        return self.scores.update(score)

    def create_score(self, score: ReviewScore) -> ReviewScore:
        """Create a review for an existing run through the typed service boundary."""

        if self.runs.get(score.run_id) is None:
            raise ValueError("run_id does not exist")
        return self.scores.create(score)

    def delete_score(self, score_id: int) -> None:
        self.scores.delete(score_id)

    def update_attachment(self, attachment: RunAttachment) -> RunAttachment:
        return self.attachments.update(attachment)

    def delete_attachment(self, attachment_id: int) -> None:
        self.attachments.delete(attachment_id)
