from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from .database import EngineDatabase
from .domain import BenchmarkDefinition, BenchmarkRun, BenchmarkSession, ExportProfile, HardwareProfile, ModelProfile, PromptTemplate, ReviewScore, RunAttachment, ScoreboardEntry, ScoreboardImportBatch, now
from .repositories import Repository


class CatalogService:
    """CRUD services for reusable catalog records."""
    def __init__(self, database: EngineDatabase):
        self.sessions = Repository(database, "benchmark_sessions", BenchmarkSession, bool_fields={"is_deleted"})
        self.model_profiles = Repository(database, "model_profiles", ModelProfile, bool_fields={"thinking_enabled", "flash_attention", "is_default"})
        self.hardware_profiles = Repository(database, "hardware_profiles", HardwareProfile, json_fields={"backend_versions"})
        self.benchmark_definitions = Repository(database, "benchmark_definitions", BenchmarkDefinition, bool_fields={"is_active"})
        self.prompt_templates = Repository(database, "prompt_templates", PromptTemplate, bool_fields={"is_active"})
        self.export_profiles = Repository(database, "export_profiles", ExportProfile, json_fields={"field_selection", "filter_json"})
        self.scoreboard_entries = Repository(database, "scoreboard_entries", ScoreboardEntry, bool_fields={"is_deleted"})
        self.scoreboard_import_batches = Repository(database, "scoreboard_import_batches", ScoreboardImportBatch, bool_fields={"is_deleted"})


class BenchmarkService:
    def __init__(self, database: EngineDatabase, catalog: CatalogService | None = None):
        self.database = database
        self.catalog = catalog or CatalogService(database)
        self.runs = Repository(database, "benchmark_runs", BenchmarkRun, json_fields={"model_snapshot", "benchmark_snapshot", "prompt_snapshot", "hardware_snapshot"}, bool_fields={"is_deleted"})
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
        prompt_snapshot = dict(run.prompt_snapshot) if run.prompt_snapshot else (self._snapshot(template) if template else {})
        if template: prompt_snapshot["prompt_text"] = template.prompt_text
        updated = replace(run,
            model_snapshot=run.model_snapshot or (self._snapshot(model) if model else {}),
            benchmark_snapshot=run.benchmark_snapshot or (self._snapshot(definition) if definition else {}),
            prompt_snapshot=prompt_snapshot,
            hardware_snapshot=run.hardware_snapshot or (self._snapshot(hardware) if hardware else {}),
            prompt_text=run.prompt_text or (template.prompt_text if template else ""),
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

    def delete_score(self, score_id: int) -> None:
        self.scores.delete(score_id)

    def update_attachment(self, attachment: RunAttachment) -> RunAttachment:
        return self.attachments.update(attachment)

    def delete_attachment(self, attachment_id: int) -> None:
        self.attachments.delete(attachment_id)
