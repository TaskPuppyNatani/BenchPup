from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


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
"""), (2, """
CREATE TABLE IF NOT EXISTS import_mapping_profiles (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, mapping_json TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""), (3, """
CREATE TABLE IF NOT EXISTS scoreboard_entries (
 id INTEGER PRIMARY KEY, model_name TEXT NOT NULL, temperature REAL, moe_experts TEXT NOT NULL DEFAULT '', context_length INTEGER,
 tokens_per_second REAL, review_quality TEXT NOT NULL DEFAULT '', score REAL,
 hallucination_level TEXT NOT NULL DEFAULT '', consistency TEXT NOT NULL DEFAULT '', reliability_score TEXT NOT NULL DEFAULT '',
 verdict TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', notes_extra TEXT NOT NULL DEFAULT '', source_file TEXT NOT NULL DEFAULT '',
 imported_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0, 1)));
CREATE INDEX IF NOT EXISTS idx_scoreboard_created_at ON scoreboard_entries(created_at);
"""), (4, """
CREATE TABLE IF NOT EXISTS scoreboard_import_batches (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, source_file TEXT NOT NULL, imported_at TEXT NOT NULL,
 notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0, 1)));
ALTER TABLE scoreboard_entries ADD COLUMN import_batch_id INTEGER REFERENCES scoreboard_import_batches(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_scoreboard_batch ON scoreboard_entries(import_batch_id);
"""), (5, """
ALTER TABLE hardware_profiles ADD COLUMN computer_name TEXT NOT NULL DEFAULT '';
ALTER TABLE hardware_profiles ADD COLUMN import_source TEXT NOT NULL DEFAULT '';
ALTER TABLE hardware_profiles ADD COLUMN imported_at TEXT;
""")]


class DatabaseMaintenanceError(RuntimeError):
    """The database could not be quiesced for a bounded maintenance window."""


class _MaintenanceState:
    """Process-local coordination for all EngineDatabase instances at one path."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.active_connections = 0
        self.maintenance_owner: int | None = None

    def enter_connection(self, *, allow_during_maintenance: bool) -> None:
        owner = threading.get_ident()
        with self.condition:
            if self.maintenance_owner is not None and not (
                allow_during_maintenance and self.maintenance_owner == owner
            ):
                raise DatabaseMaintenanceError("Database is temporarily in maintenance mode")
            self.active_connections += 1

    def leave_connection(self) -> None:
        with self.condition:
            self.active_connections = max(0, self.active_connections - 1)
            self.condition.notify_all()

    def acquire_maintenance(self, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        owner = threading.get_ident()
        with self.condition:
            while self.maintenance_owner is not None or self.active_connections:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DatabaseMaintenanceError(
                        "Database connections could not be quiesced within the maintenance timeout"
                    )
                self.condition.wait(remaining)
            self.maintenance_owner = owner

    def release_maintenance(self) -> None:
        owner = threading.get_ident()
        with self.condition:
            if self.maintenance_owner != owner:
                raise DatabaseMaintenanceError("Database maintenance is owned by another thread")
            self.maintenance_owner = None
            self.condition.notify_all()


class _TrackedConnection(sqlite3.Connection):
    """SQLite connection that releases the process-local maintenance slot on close."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._release_callback: Callable[[], None] | None = None
        self._released = False

    def set_release_callback(self, callback: Callable[[], None]) -> None:
        self._release_callback = callback

    def close(self) -> None:
        try:
            super().close()
        finally:
            callback = self._release_callback
            if callback is not None and not self._released:
                self._released = True
                self._release_callback = None
                callback()

    def __del__(self) -> None:  # pragma: no cover - interpreter cleanup fallback.
        try:
            self.close()
        except Exception:
            pass


_MAINTENANCE_STATES: dict[str, _MaintenanceState] = {}
_MAINTENANCE_STATES_LOCK = threading.Lock()


def _maintenance_state_for(path: Path) -> _MaintenanceState:
    try:
        key = str(path.resolve(strict=False))
    except (OSError, RuntimeError):
        key = str(path.absolute())
    with _MAINTENANCE_STATES_LOCK:
        state = _MAINTENANCE_STATES.get(key)
        if state is None:
            state = _MaintenanceState()
            _MAINTENANCE_STATES[key] = state
        return state


class EngineDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._maintenance_state = _maintenance_state_for(self.path)

    def connect(self, *, allow_during_maintenance: bool = False) -> sqlite3.Connection:
        self._maintenance_state.enter_connection(
            allow_during_maintenance=allow_during_maintenance
        )
        connection: _TrackedConnection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=5.0,
                factory=_TrackedConnection,
            )
            assert isinstance(connection, _TrackedConnection)
            connection.set_release_callback(self._maintenance_state.leave_connection)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except Exception:
            if connection is None:
                self._maintenance_state.leave_connection()
            else:
                connection.close()
            raise

    def close(self) -> None:
        """Close database resources owned by the engine boundary.

        EngineDatabase opens connections for individual operations and closes
        them in ``connection``.  The explicit lifecycle method gives front
        ends a stable shutdown hook without introducing a long-lived shared
        SQLite connection.
        """

        return None

    @contextmanager
    def connection(self, *, allow_during_maintenance: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect(allow_during_maintenance=allow_during_maintenance)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def maintenance(self, *, timeout: float = 5.0) -> Iterator[None]:
        """Quiesce this database path for a bounded file-maintenance window.

        All current engine operations use short-lived connections, so this
        process-local guard is sufficient to prevent another BenchPup thread
        or EngineDatabase instance from opening the active path during a
        replace-and-reopen sequence.  The maintenance owner may explicitly
        open verification connections with ``allow_during_maintenance=True``.
        """

        self._maintenance_state.acquire_maintenance(timeout)
        try:
            yield
        finally:
            self._maintenance_state.release_maintenance()

    def migrate(self, *, allow_during_maintenance: bool = False) -> None:
        with self.connection(allow_during_maintenance=allow_during_maintenance) as connection:
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
