from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from typing import Any, TypeVar

from .database import EngineDatabase
from .domain import BenchmarkDefinition, BenchmarkRun, BenchmarkSession, ExportProfile, HardwareProfile, ModelProfile, PromptTemplate, ReviewScore, RunAttachment, ScoreboardEntry, ScoreboardImportBatch, now, prompt_hash_for, resolve_prompt_text
from .repositories import Repository

_T = TypeVar("_T")


def is_database_integrity_error(error: BaseException) -> bool:
    """Expose the engine's persistence-conflict classification without leaking it into UI code."""

    return isinstance(error, sqlite3.IntegrityError)


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
