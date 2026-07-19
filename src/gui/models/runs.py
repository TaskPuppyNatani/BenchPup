"""Immutable presentation rows for the GUI Runs browser."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..context import GuiApplicationContext

try:
    from ...engine.reporting import BenchmarkRunAggregate
    from ...engine.statistics import parse_utc_timestamp
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.reporting import BenchmarkRunAggregate  # type: ignore[no-redef]
    from engine.statistics import parse_utc_timestamp  # type: ignore[no-redef]


NOT_RECORDED = "Not recorded"
UNAVAILABLE = "Unavailable"


def snapshot_label(snapshot: Any, *keys: str, default: str = UNAVAILABLE) -> str:
    """Read a display label without assuming optional legacy snapshot shape."""

    if not isinstance(snapshot, dict):
        return default
    for key in keys:
        value = snapshot.get(key)
        if value not in (None, ""):
            return str(value)
    return default


def _session_label(aggregate: BenchmarkRunAggregate) -> str:
    if aggregate.session is not None and aggregate.session.title.strip():
        return aggregate.session.title
    return NOT_RECORDED if aggregate.run.session_id is None else UNAVAILABLE


@dataclass(frozen=True)
class RunBrowserRow:
    """One read-only table row retaining its complete engine aggregate."""

    aggregate: BenchmarkRunAggregate
    run_id: int | None
    recorded_at: str
    model: str
    benchmark: str
    session: str
    prompt_name: str
    overall_score: float | None
    accuracy_score: float | None
    hallucination_level: str | None
    reliability_level: str | None
    tokens_per_second: Any

    @property
    def search_text(self) -> str:
        run = self.aggregate.run
        return " ".join(
            (
                self.model,
                self.benchmark,
                self.session,
                self.prompt_name,
                str(run.prompt_text or ""),
                str(self.run_id or ""),
            )
        ).casefold()

    @property
    def is_scored(self) -> bool:
        return self.aggregate.score is not None


def row_from_aggregate(aggregate: BenchmarkRunAggregate) -> RunBrowserRow:
    run = aggregate.run
    model_snapshot = run.model_snapshot
    benchmark_snapshot = run.benchmark_snapshot
    return RunBrowserRow(
        aggregate=aggregate,
        run_id=run.id,
        recorded_at=run.created_at,
        model=snapshot_label(model_snapshot, "model_name", "name"),
        benchmark=snapshot_label(benchmark_snapshot, "name", "file_path", "benchmark_file"),
        session=_session_label(aggregate),
        prompt_name=run.prompt_name or snapshot_label(run.prompt_snapshot, "name", default=NOT_RECORDED),
        overall_score=aggregate.score.overall_score if aggregate.score else None,
        accuracy_score=aggregate.score.accuracy_score if aggregate.score else None,
        hallucination_level=aggregate.score.hallucination_level if aggregate.score else None,
        reliability_level=aggregate.score.reliability_level if aggregate.score else None,
        tokens_per_second=model_snapshot.get("tokens_per_second") if isinstance(model_snapshot, dict) else None,
    )


def _sort_key(row: RunBrowserRow) -> tuple[datetime, str, int]:
    parsed = parse_utc_timestamp(row.recorded_at)
    timestamp = parsed or datetime.min.replace(tzinfo=timezone.utc)
    return timestamp, row.recorded_at or "", row.run_id or -1


class RunsDataProvider:
    """Adapt the existing statistics selection boundary into browser rows."""

    def __init__(self, context: GuiApplicationContext) -> None:
        self.context = context

    def load(self) -> tuple[RunBrowserRow, ...]:
        aggregates = self.context.statistics.select_benchmark_runs()
        rows = tuple(row_from_aggregate(aggregate) for aggregate in aggregates)
        return tuple(sorted(rows, key=_sort_key, reverse=True))


__all__ = (
    "NOT_RECORDED",
    "UNAVAILABLE",
    "RunBrowserRow",
    "RunsDataProvider",
    "row_from_aggregate",
    "snapshot_label",
)
