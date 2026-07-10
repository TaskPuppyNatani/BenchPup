from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


MIGRATIONS: list[tuple[int, str]] = [(1, """
CREATE TABLE IF NOT EXISTS benchmark_sessions (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', started_at TEXT,
 completed_at TEXT, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0, 1)));
CREATE TABLE IF NOT EXISTS model_profiles (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, model_name TEXT NOT NULL, model_family TEXT NOT NULL DEFAULT '',
 model_size TEXT NOT NULL DEFAULT '', quantization TEXT NOT NULL DEFAULT '', backend TEXT NOT NULL DEFAULT 'Other',
 temperature REAL, top_p REAL, top_k INTEGER, min_p REAL, thinking_enabled INTEGER NOT NULL DEFAULT 0,
 flash_attention INTEGER NOT NULL DEFAULT 0, moe_experts TEXT NOT NULL DEFAULT '', context_length INTEGER,
 tokens_per_second REAL, is_default INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hardware_profiles (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, cpu TEXT NOT NULL DEFAULT '', gpu TEXT NOT NULL DEFAULT '',
 vram_gb REAL, ram_gb REAL, operating_system TEXT NOT NULL DEFAULT '', backend_versions TEXT NOT NULL DEFAULT '{}',
 notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS benchmark_definitions (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, file_path TEXT NOT NULL, benchmark_type TEXT NOT NULL,
 default_prompt TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prompt_templates (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL, prompt_text TEXT NOT NULL, prompt_hash TEXT NOT NULL,
 benchmark_type TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 is_active INTEGER NOT NULL DEFAULT 1, UNIQUE(name, version));
CREATE TABLE IF NOT EXISTS benchmark_runs (
 id INTEGER PRIMARY KEY, session_id INTEGER REFERENCES benchmark_sessions(id) ON DELETE SET NULL,
 model_profile_id INTEGER REFERENCES model_profiles(id) ON DELETE SET NULL,
 benchmark_definition_id INTEGER REFERENCES benchmark_definitions(id) ON DELETE SET NULL,
 prompt_template_id INTEGER REFERENCES prompt_templates(id) ON DELETE SET NULL,
 hardware_profile_id INTEGER REFERENCES hardware_profiles(id) ON DELETE SET NULL,
 raw_model_output TEXT NOT NULL, prompt_name TEXT NOT NULL DEFAULT '', prompt_text TEXT NOT NULL DEFAULT '',
 model_snapshot TEXT NOT NULL DEFAULT '{}', benchmark_snapshot TEXT NOT NULL DEFAULT '{}',
 prompt_snapshot TEXT NOT NULL DEFAULT '{}', hardware_snapshot TEXT NOT NULL DEFAULT '{}', fingerprint TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0, 1)));
CREATE TABLE IF NOT EXISTS review_scores (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL UNIQUE REFERENCES benchmark_runs(id) ON DELETE CASCADE,
 accuracy_score REAL, hallucination_level TEXT NOT NULL, reliability_level TEXT NOT NULL, depth_score REAL,
 signal_noise_score REAL, actionability_score REAL, seniority_score REAL, overall_score REAL,
 strengths TEXT NOT NULL DEFAULT '', weaknesses TEXT NOT NULL DEFAULT '', verdict TEXT NOT NULL DEFAULT '',
 notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_attachments (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES benchmark_runs(id) ON DELETE CASCADE,
 attachment_type TEXT NOT NULL, file_path TEXT NOT NULL, original_filename TEXT NOT NULL,
 notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS export_profiles (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, format TEXT NOT NULL, field_selection TEXT NOT NULL DEFAULT '{}',
 filter_json TEXT NOT NULL DEFAULT '{}', destination TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS change_history (
 id INTEGER PRIMARY KEY, operation TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
 before_json TEXT, after_json TEXT, created_at TEXT NOT NULL, undone_at TEXT);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON benchmark_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_runs_session ON benchmark_runs(session_id);
CREATE INDEX IF NOT EXISTS idx_runs_deleted ON benchmark_runs(is_deleted);
CREATE INDEX IF NOT EXISTS idx_attachments_run ON run_attachments(run_id);
""")]


class EngineDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def connection(self):
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connection() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY CHECK(id = 1), version INTEGER NOT NULL, applied_at TEXT NOT NULL)")
            row = connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
            current = row["version"] if row else 0
            legacy_table = self._move_legacy_runs(connection) if current == 0 else None
            for version, script in MIGRATIONS:
                if version > current:
                    connection.executescript(script)
                    connection.execute("INSERT INTO schema_version(id, version, applied_at) VALUES(1, ?, datetime('now')) ON CONFLICT(id) DO UPDATE SET version = excluded.version, applied_at = excluded.applied_at", (version,))
            if legacy_table:
                self._copy_legacy_runs(connection, legacy_table)

    @staticmethod
    def _move_legacy_runs(connection: sqlite3.Connection) -> str | None:
        """Keep the original MVP records while replacing its incompatible table."""
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'benchmark_runs'").fetchone()
        if not exists:
            return None
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(benchmark_runs)")}
        if "model_snapshot" in columns:
            return None
        legacy = "legacy_benchmark_runs"
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (legacy,)).fetchone():
            return None
        connection.execute(f"ALTER TABLE benchmark_runs RENAME TO {legacy}")
        return legacy

    @staticmethod
    def _copy_legacy_runs(connection: sqlite3.Connection, table: str) -> None:
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY id"):
            old = dict(row)
            model = {name: old.get(name) for name in ("model_name", "model_family", "model_size", "quantization", "backend", "temperature", "top_p", "top_k", "min_p", "thinking_enabled", "flash_attention", "moe_experts", "context_length", "tokens_per_second")}
            benchmark = {name: old.get(name) for name in ("benchmark_file", "benchmark_type")}
            prompt = {"name": old.get("prompt_name", ""), "prompt_text": old.get("prompt_text", "")}
            canonical = {"model": model, "benchmark": benchmark, "prompt": prompt, "hardware": {}, "output": old.get("raw_model_output", "")}
            fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()
            connection.execute("INSERT INTO benchmark_runs(id, raw_model_output, prompt_name, prompt_text, model_snapshot, benchmark_snapshot, prompt_snapshot, hardware_snapshot, fingerprint, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (old["id"], old.get("raw_model_output", ""), old.get("prompt_name", ""), old.get("prompt_text", ""), json.dumps(model), json.dumps(benchmark), json.dumps(prompt), "{}", fingerprint, old.get("created_at") or "", old.get("created_at") or ""))
            if any(old.get(name) is not None for name in ("accuracy_score", "depth_score", "signal_noise_score", "actionability_score", "seniority_score", "overall_score")):
                connection.execute("INSERT INTO review_scores(run_id, accuracy_score, hallucination_level, reliability_level, depth_score, signal_noise_score, actionability_score, seniority_score, overall_score, strengths, weaknesses, verdict, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (old["id"], old.get("accuracy_score"), old.get("hallucination_level") or "Medium", old.get("reliability_level") or "Medium", old.get("depth_score"), old.get("signal_noise_score"), old.get("actionability_score"), old.get("seniority_score"), old.get("overall_score"), old.get("strengths") or "", old.get("weaknesses") or "", old.get("verdict") or "", old.get("notes") or "", old.get("created_at") or "", old.get("created_at") or ""))
