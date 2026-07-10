from __future__ import annotations

import json
from dataclasses import asdict, fields, replace
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from .database import EngineDatabase

T = TypeVar("T")


class Repository(Generic[T]):
    def __init__(self, database: EngineDatabase, table: str, model: type[T], json_fields: set[str] | None = None, bool_fields: set[str] | None = None):
        self.database, self.table, self.model = database, table, model
        self.json_fields, self.bool_fields = json_fields or set(), bool_fields or set()
        self.columns = [field.name for field in fields(model) if field.name != "id"]

    def _values(self, item: T) -> dict[str, Any]:
        values = asdict(item)
        values.pop("id", None)
        for name in self.json_fields: values[name] = json.dumps(values[name], sort_keys=True)
        for name in self.bool_fields: values[name] = int(values[name])
        return values

    def _item(self, row: Any) -> T:
        values = dict(row)
        for name in self.json_fields: values[name] = json.loads(values[name])
        for name in self.bool_fields: values[name] = bool(values[name])
        return self.model(**values)

    def create(self, item: T) -> T:
        item.validate()
        values = self._values(item)
        marks = ", ".join("?" for _ in self.columns)
        with self.database.connection() as connection:
            cursor = connection.execute(f"INSERT INTO {self.table} ({', '.join(self.columns)}) VALUES ({marks})", [values[column] for column in self.columns])
            return self.get(cursor.lastrowid, connection=connection)

    def get(self, item_id: int, connection=None) -> T | None:
        if connection is None:
            with self.database.connection() as active: return self.get(item_id, active)
        row = connection.execute(f"SELECT * FROM {self.table} WHERE id = ?", (item_id,)).fetchone()
        return self._item(row) if row else None

    def list(self, include_deleted: bool = False) -> list[T]:
        statement = f"SELECT * FROM {self.table}"
        if "is_deleted" in self.columns and not include_deleted: statement += " WHERE is_deleted = 0"
        statement += " ORDER BY id"
        with self.database.connection() as connection:
            return [self._item(row) for row in connection.execute(statement)]

    def update(self, item: T) -> T:
        if item.id is None: raise ValueError("id is required for update")
        if "updated_at" in self.columns:
            item = replace(item, updated_at=datetime.now(timezone.utc).isoformat())
        item.validate(); values = self._values(item)
        assignments = ", ".join(f"{column} = ?" for column in self.columns)
        with self.database.connection() as connection:
            connection.execute(f"UPDATE {self.table} SET {assignments} WHERE id = ?", [values[column] for column in self.columns] + [item.id])
            updated = self.get(item.id, connection)
        if updated is None: raise KeyError(f"{self.table} {item.id} does not exist")
        return updated

    def delete(self, item_id: int, soft: bool = False) -> None:
        with self.database.connection() as connection:
            if soft and "is_deleted" in self.columns:
                connection.execute(f"UPDATE {self.table} SET is_deleted = 1 WHERE id = ?", (item_id,))
            else:
                connection.execute(f"DELETE FROM {self.table} WHERE id = ?", (item_id,))
