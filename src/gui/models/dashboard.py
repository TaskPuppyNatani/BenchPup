"""Dashboard read models backed by existing typed engine service results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..context import GuiApplicationContext

try:
    from ...engine.statistics import (
        BenchmarkRunStatisticsGroup,
        BenchmarkStatisticsFilters,
        CategoricalDistribution,
        ReviewStatisticsSummary,
        ScoreboardStatisticsGroup,
        ScoreboardStatisticsFilters,
        StatisticsAvailability,
        StatisticsOverview,
        parse_utc_timestamp,
    )
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine.statistics import (  # type: ignore[no-redef]
        BenchmarkRunStatisticsGroup,
        BenchmarkStatisticsFilters,
        CategoricalDistribution,
        ReviewStatisticsSummary,
        ScoreboardStatisticsGroup,
        ScoreboardStatisticsFilters,
        StatisticsAvailability,
        StatisticsOverview,
        parse_utc_timestamp,
    )


@dataclass(frozen=True)
class DashboardSummary:
    benchmark_run_count: int
    scored_run_count: int
    model_count: int
    session_count: int
    scoreboard_entry_count: int
    average_overall_score: float | None
    unscored_run_count: int = 0
    benchmark_count: int = 0
    known_hardware_count: int = 0
    median_overall_score: float | None = None
    reviewed_run_count: int = 0
    review_summary: ReviewStatisticsSummary = field(default_factory=ReviewStatisticsSummary)
    availability: StatisticsAvailability = field(default_factory=StatisticsAvailability)


@dataclass(frozen=True)
class DashboardRecentRun:
    run_id: int | None
    recorded_at: str
    model: str
    benchmark: str
    overall_score: float | None
    tokens_per_second: Any


@dataclass(frozen=True)
class DashboardVisualizationData:
    """Source-separated typed results used by the native dashboard charts."""

    benchmark_record_count: int = 0
    benchmark_score_missing_count: int = 0
    benchmark_speed_missing_count: int = 0
    benchmark_model_groups: tuple[BenchmarkRunStatisticsGroup, ...] = ()
    benchmark_hallucination: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    benchmark_reliability: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    scoreboard_record_count: int = 0
    scoreboard_score_missing_count: int = 0
    scoreboard_speed_missing_count: int = 0
    scoreboard_model_groups: tuple[ScoreboardStatisticsGroup, ...] = ()
    scoreboard_hallucination: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    scoreboard_consistency: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    scoreboard_reliability: CategoricalDistribution = field(default_factory=CategoricalDistribution)


@dataclass(frozen=True)
class DashboardSnapshot:
    summary: DashboardSummary
    recent_runs: tuple[DashboardRecentRun, ...]
    statistics: StatisticsOverview | None = None
    visualization: DashboardVisualizationData = field(default_factory=DashboardVisualizationData)


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

    def load(
        self,
        *,
        benchmark_filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
        scoreboard_filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters(),
    ) -> DashboardSnapshot:
        aggregates = self.context.statistics.select_benchmark_runs()
        entries = self.context.statistics.select_scoreboard_entries()
        overview = self.context.statistics.statistics_overview(runs=aggregates, entries=entries)
        filtered_overview = self.context.statistics.statistics_overview(
            runs=aggregates,
            entries=entries,
            benchmark_filters=benchmark_filters,
            scoreboard_filters=scoreboard_filters,
        )
        visualization = DashboardVisualizationData(
            benchmark_record_count=filtered_overview.benchmark_runs.total_eligible_runs,
            benchmark_score_missing_count=filtered_overview.benchmark_runs.overall_score.missing_count,
            benchmark_speed_missing_count=filtered_overview.benchmark_runs.tokens_per_second.missing_count,
            benchmark_model_groups=self.context.statistics.group_benchmark_runs(
                aggregates,
                filters=benchmark_filters,
            ),
            benchmark_hallucination=filtered_overview.benchmark_runs.hallucination,
            benchmark_reliability=filtered_overview.benchmark_runs.reliability,
            scoreboard_record_count=filtered_overview.scoreboard_entries.total_eligible_entries,
            scoreboard_score_missing_count=filtered_overview.scoreboard_entries.score.missing_count,
            scoreboard_speed_missing_count=filtered_overview.scoreboard_entries.tokens_per_second.missing_count,
            scoreboard_model_groups=self.context.statistics.group_scoreboard_entries(
                entries,
                filters=scoreboard_filters,
            ),
            scoreboard_hallucination=filtered_overview.scoreboard_entries.hallucination,
            scoreboard_consistency=filtered_overview.scoreboard_entries.consistency,
            scoreboard_reliability=filtered_overview.scoreboard_entries.reliability,
        )
        summary = overview.benchmark_runs
        scoreboard = overview.scoreboard_entries
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
                model_count=summary.unique_model_count,
                session_count=summary.unique_session_count,
                scoreboard_entry_count=scoreboard.total_eligible_entries,
                average_overall_score=summary.overall_score.mean,
                unscored_run_count=summary.unscored_runs,
                benchmark_count=summary.unique_benchmark_count,
                known_hardware_count=summary.known_hardware_environment_count,
                median_overall_score=summary.overall_score.median,
                reviewed_run_count=summary.reviewed_runs,
                review_summary=overview.reviews,
                availability=overview.availability,
            ),
            recent_runs=recent,
            statistics=overview,
            visualization=visualization,
        )
