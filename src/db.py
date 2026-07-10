from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from models import BenchmarkRun

COLUMNS = [name for name in BenchmarkRun.__dataclass_fields__ if name != "id"]
BOOL_COLUMNS = {"thinking_enabled", "flash_attention"}


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        con = self.connect()
        try:
            fields = []
            for column in COLUMNS:
                if column in BOOL_COLUMNS:
                    kind = "INTEGER"
                elif column in {"top_k", "context_length"}:
                    kind = "INTEGER"
                elif column.endswith("_score") or column in {"temperature", "top_p", "min_p", "tokens_per_second"}:
                    kind = "REAL"
                else:
                    kind = "TEXT"
                fields.append(f"{column} {kind}")
            con.execute(f"CREATE TABLE IF NOT EXISTS benchmark_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, {', '.join(fields)}, fingerprint TEXT UNIQUE NOT NULL)")
            con.commit()
        finally:
            con.close()

    @staticmethod
    def fingerprint(run: BenchmarkRun) -> str:
        values = run.to_dict()
        values.pop("id", None)
        values.pop("created_at", None)
        return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()

    def add_run(self, run: BenchmarkRun, skip_duplicates: bool = False) -> int | None:
        run.validate()
        values = run.to_dict()
        values.pop("id", None)
        values = {key: int(value) if key in BOOL_COLUMNS else value for key, value in values.items()}
        fingerprint = self.fingerprint(run)
        statement = f"INSERT {'OR IGNORE' if skip_duplicates else ''} INTO benchmark_runs ({', '.join(COLUMNS)}, fingerprint) VALUES ({', '.join('?' for _ in COLUMNS)}, ?)"
        con = self.connect()
        try:
            cursor = con.execute(statement, [values[name] for name in COLUMNS] + [fingerprint])
            con.commit()
            return cursor.lastrowid if cursor.rowcount else None
        finally:
            con.close()

    def list_runs(self) -> list[BenchmarkRun]:
        con = self.connect()
        try:
            rows = con.execute("SELECT * FROM benchmark_runs ORDER BY id DESC").fetchall()
        finally:
            con.close()
        result = []
        for row in rows:
            values = dict(row)
            values.pop("fingerprint", None)
            for column in BOOL_COLUMNS:
                values[column] = bool(values[column])
            result.append(BenchmarkRun(**values))
        return result
