"""Dashboard read models backed by existing typed engine service results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..context import GuiApplicationContext

try:
    from ...engine.statistics import parse_utc_timestamp
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.statistics import parse_utc_timestamp  # type: ignore[no-redef]


@dataclass(frozen=True)
class DashboardSummary:
    benchmark_run_count: int
    scored_run_count: int
    model_count: int
    session_count: int
    scoreboard_entry_count: int
    average_overall_score: float | None


@dataclass(frozen=True)
class DashboardRecentRun:
    run_id: int | None
    recorded_at: str
    model: str
    benchmark: str
    overall_score: float | None
    tokens_per_second: Any


@dataclass(frozen=True)
class DashboardSnapshot:
    summary: DashboardSummary
    recent_runs: tuple[DashboardRecentRun, ...]


def _snapshot_label(snapshot: dict[str, Any], *keys: str, default: str) -> str:
    for key in keys:
        value = snapshot.get(key)
        if value not in (None, ""):
            return str(value)
    return default


def _recent_sort_key(aggregate: Any) -> tuple[datetime, str, int]:
    run = aggregate.run
    parsed = parse_utc_timestamp(run.created_at)
    timestamp = parsed or datetime.min.replace(tzinfo=timezone.utc)
    return timestamp, str(run.created_at), run.id or -1


class DashboardDataProvider:
    """Adapt engine aggregates into a small immutable dashboard view model."""

    def __init__(self, context: GuiApplicationContext) -> None:
        self.context = context

    def load(self) -> DashboardSnapshot:
        aggregates = self.context.statistics.select_benchmark_runs()
        summary = self.context.statistics.benchmark_run_statistics(aggregates)
        recent = tuple(
            DashboardRecentRun(
                run_id=aggregate.run.id,
                recorded_at=aggregate.run.created_at,
                model=_snapshot_label(aggregate.run.model_snapshot, "model_name", "name", default="Unknown model"),
                benchmark=_snapshot_label(
                    aggregate.run.benchmark_snapshot,
                    "name",
                    "file_path",
                    "benchmark_file",
                    default="Unknown benchmark",
                ),
                overall_score=aggregate.score.overall_score if aggregate.score else None,
                tokens_per_second=aggregate.run.model_snapshot.get("tokens_per_second"),
            )
            for aggregate in sorted(aggregates, key=_recent_sort_key, reverse=True)[:10]
        )
        return DashboardSnapshot(
            summary=DashboardSummary(
                benchmark_run_count=summary.total_eligible_runs,
                scored_run_count=summary.scored_runs,
                model_count=len(self.context.catalog.list_model_profiles()),
                session_count=len(self.context.catalog.list_sessions()),
                scoreboard_entry_count=len(self.context.catalog.scoreboard_entries.list()),
                average_overall_score=summary.overall_score.mean,
            ),
            recent_runs=recent,
        )
