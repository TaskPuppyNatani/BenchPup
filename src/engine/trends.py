"""Interpretation-neutral historical trend analysis for BenchPup.

``TrendService`` is deliberately separate from both the comparison and
reporting boundaries.  It selects immutable historical aggregates through
``StatisticsService``, reuses its UTC bucket and descriptive-statistics
conventions, and returns structured results.  Markdown rendering and CLI
presentation are handled by their existing layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .domain import now
from .services import BenchmarkService, CatalogService
from .statistics import (
    BenchmarkStatisticsFilters,
    CategoricalDistribution,
    NumericSummary,
    ScoreboardStatisticsFilters,
    StatisticsService,
    TimeBucketGranularity,
    benchmark_snapshot_name,
    categorical_distribution,
    model_snapshot_name,
    normalize_benchmark_identity,
    normalize_model_identity,
    numeric_summary,
    parse_utc_timestamp,
    time_bucket_for,
)


class TrendType(str, Enum):
    """Stable source-family identifiers for trend reports."""

    BENCHMARK_RUN = "benchmark_run"
    BENCHMARK_RUNS = "benchmark_run"
    SCOREBOARD_ENTRY = "scoreboard_entry"
    SCOREBOARD = "scoreboard_entry"


class TrendGrouping(str, Enum):
    """Supported trend series groupings across both source families."""

    OVERALL = "overall"
    MODEL = "model"
    BENCHMARK = "benchmark"
    BENCHMARK_TYPE = "benchmark_type"
    SESSION = "session"
    HARDWARE = "hardware"
    HARDWARE_ENVIRONMENT = "hardware"
    IMPORT_BATCH = "import_batch"
    BATCH = "import_batch"


# These aliases make the source-specific vocabulary discoverable without
# duplicating enum definitions or allowing the two source families to merge.
BenchmarkTrendGrouping = TrendGrouping
ScoreboardTrendGrouping = TrendGrouping


def _freeze_mapping(value: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _sorted_labels(values: Iterable[str]) -> tuple[str, ...]:
    labels: dict[str, str] = {}
    for value in values:
        label = _as_text(value)
        if not label:
            continue
        key = label.casefold()
        current = labels.get(key)
        if current is None or (label.casefold(), label) < (current.casefold(), current):
            labels[key] = label
    return tuple(sorted(labels.values(), key=lambda item: (item.casefold(), item)))


def _normalize_granularity(value: str | TimeBucketGranularity) -> TimeBucketGranularity:
    if isinstance(value, TimeBucketGranularity):
        return value
    normalized = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    try:
        return TimeBucketGranularity(normalized)
    except ValueError as error:
        raise ValueError(f"Unsupported trend bucket interval: {value}") from error


def _normalize_grouping(value: str | TrendGrouping) -> TrendGrouping:
    if isinstance(value, TrendGrouping):
        return value
    normalized = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "benchmark_type": TrendGrouping.BENCHMARK_TYPE,
        "benchmarktype": TrendGrouping.BENCHMARK_TYPE,
        "hardware_environment": TrendGrouping.HARDWARE,
        "hardware_profile": TrendGrouping.HARDWARE,
        "batch": TrendGrouping.IMPORT_BATCH,
        "import_batch": TrendGrouping.IMPORT_BATCH,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return TrendGrouping(normalized)
    except ValueError as error:
        raise ValueError(f"Unsupported trend grouping: {value}") from error


def _date_range(values: Iterable[datetime]) -> tuple[str | None, str | None]:
    ordered = sorted(values)
    return (ordered[0].isoformat(), ordered[-1].isoformat()) if ordered else (None, None)


def _next_bucket_start(start: str, granularity: TimeBucketGranularity) -> str:
    value = datetime.fromisoformat(start).astimezone(timezone.utc)
    if granularity is TimeBucketGranularity.DAY:
        value += timedelta(days=1)
    elif granularity is TimeBucketGranularity.WEEK:
        value += timedelta(days=7)
    else:
        if value.month == 12:
            value = value.replace(year=value.year + 1, month=1, day=1)
        else:
            value = value.replace(month=value.month + 1, day=1)
    return value.isoformat()


def _bucket_range(start: str, end: str, granularity: TimeBucketGranularity) -> tuple[str, ...]:
    values: list[str] = []
    current = start
    while current <= end:
        values.append(current)
        current = _next_bucket_start(current, granularity)
    return tuple(values)


def _delta(
    first: NumericSummary | None,
    last: NumericSummary | None,
) -> tuple[float | None, float | None]:
    if first is None or last is None or first.mean is None or last.mean is None:
        return None, None
    absolute = last.mean - first.mean
    percentage = None if first.mean == 0 else (absolute / first.mean) * 100.0
    return absolute, percentage


def _filter_value(value: Any) -> str:
    if isinstance(value, (set, frozenset, tuple, list)):
        return ",".join(str(item) for item in sorted(value))
    return str(value)


def _benchmark_filter_mapping(filters: BenchmarkStatisticsFilters) -> Mapping[str, str]:
    values: dict[str, str] = {}
    names = (
        "model", "benchmark", "benchmark_type", "session", "session_id",
        "hardware", "hardware_profile_id", "date_from", "date_to",
        "minimum_score", "maximum_score", "hallucination", "reliability",
    )
    for name in names:
        value = getattr(filters, name)
        if value not in (None, "", frozenset()):
            values[name] = _filter_value(value)
    if filters.include_run_ids:
        values["include_run_ids"] = _filter_value(filters.include_run_ids)
    if filters.exclude_run_ids:
        values["exclude_run_ids"] = _filter_value(filters.exclude_run_ids)
    if filters.include_deleted:
        values["include_deleted"] = "true"
    return values


def _scoreboard_filter_mapping(filters: ScoreboardStatisticsFilters) -> Mapping[str, str]:
    values: dict[str, str] = {}
    names = (
        "model", "batch_id", "date_from", "date_to", "minimum_score",
        "maximum_score", "hallucination", "consistency", "reliability",
    )
    for name in names:
        value = getattr(filters, name)
        if value not in (None, ""):
            values[name] = _filter_value(value)
    if filters.include_deleted:
        values["include_deleted"] = "true"
    return values


@dataclass(frozen=True)
class TrendMetadata:
    """Selection and coverage metadata for one immutable trend result."""

    trend_type: TrendType | str
    title: str
    generated_at: str
    source_record_family: str
    bucket_interval: TimeBucketGranularity | str
    date_range: tuple[str | None, str | None] = (None, None)
    contributing_record_count: int = 0
    excluded_timestamp_count: int = 0
    active_filters: Mapping[str, str] = field(default_factory=dict)
    selected_grouping: TrendGrouping | str = TrendGrouping.OVERALL

    def __post_init__(self) -> None:
        object.__setattr__(self, "date_range", tuple(self.date_range))
        object.__setattr__(self, "active_filters", _freeze_mapping(self.active_filters))

    @property
    def source_record_type(self) -> str:
        return self.source_record_family

    @property
    def interval(self) -> str:
        return self.bucket_interval.value if isinstance(self.bucket_interval, TimeBucketGranularity) else str(self.bucket_interval)

    @property
    def granularity(self) -> str:
        return self.interval

    @property
    def bucket_granularity(self) -> str:
        return self.interval

    @property
    def date_from(self) -> str | None:
        return self.date_range[0]

    @property
    def date_to(self) -> str | None:
        return self.date_range[1]

    @property
    def excluded_missing_invalid_timestamp_count(self) -> int:
        return self.excluded_timestamp_count

    @property
    def contributing_count(self) -> int:
        return self.contributing_record_count

    @property
    def grouping(self) -> TrendGrouping | str:
        return self.selected_grouping


@dataclass(frozen=True)
class TrendPoint:
    """Descriptive metrics for one populated or explicitly empty bucket."""

    bucket_start: str
    bucket_end: str | None = None
    label: str = ""
    record_count: int = 0
    scored_count: int = 0
    missing_score_count: int = 0
    overall_score: NumericSummary = field(default_factory=NumericSummary)
    tokens_per_second: NumericSummary = field(default_factory=NumericSummary)
    hallucination: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    reliability: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    consistency: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    represented_models: tuple[str, ...] = ()
    represented_benchmarks: tuple[str, ...] = ()
    represented_sessions: tuple[str, ...] = ()
    represented_hardware: tuple[str, ...] = ()
    represented_import_batches: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "represented_models", "represented_benchmarks", "represented_sessions",
            "represented_hardware", "represented_import_batches",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def score_summary(self) -> NumericSummary:
        return self.overall_score

    @property
    def score(self) -> NumericSummary:
        return self.overall_score

    @property
    def overall_score_summary(self) -> NumericSummary:
        return self.overall_score

    @property
    def speed_summary(self) -> NumericSummary:
        return self.tokens_per_second

    @property
    def speed(self) -> NumericSummary:
        return self.tokens_per_second

    @property
    def tokens_per_second_summary(self) -> NumericSummary:
        return self.tokens_per_second

    @property
    def count(self) -> int:
        return self.record_count

    @property
    def is_empty(self) -> bool:
        return self.record_count == 0

    @property
    def missing_speed_count(self) -> int:
        return self.tokens_per_second.missing_count

    @property
    def missing_score(self) -> int:
        return self.missing_score_count

    @property
    def represented_batches(self) -> tuple[str, ...]:
        return self.represented_import_batches


@dataclass(frozen=True)
class TrendSeries:
    """One deterministic grouping series and its neutral first-to-last deltas."""

    identity: str
    label: str
    points: tuple[TrendPoint, ...] = ()
    total_contributing_records: int = 0
    total_scored_records: int = 0
    first_available_score_summary: NumericSummary | None = None
    last_available_score_summary: NumericSummary | None = None
    score_absolute_delta: float | None = None
    score_percentage_delta: float | None = None
    first_available_speed_summary: NumericSummary | None = None
    last_available_speed_summary: NumericSummary | None = None
    speed_absolute_delta: float | None = None
    speed_percentage_delta: float | None = None
    missing_bucket_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", tuple(self.points))

    @property
    def bucket_count(self) -> int:
        return len(self.points)

    @property
    def populated_bucket_count(self) -> int:
        return sum(1 for point in self.points if point.record_count)

    @property
    def record_count(self) -> int:
        return self.total_contributing_records

    @property
    def scored_count(self) -> int:
        return self.total_scored_records

    @property
    def first_score_summary(self) -> NumericSummary | None:
        return self.first_available_score_summary

    @property
    def score_summary(self) -> NumericSummary | None:
        return self.last_available_score_summary

    @property
    def speed_summary(self) -> NumericSummary | None:
        return self.last_available_speed_summary

    @property
    def first_available_score(self) -> NumericSummary | None:
        return self.first_available_score_summary

    @property
    def last_score_summary(self) -> NumericSummary | None:
        return self.last_available_score_summary

    @property
    def last_available_score(self) -> NumericSummary | None:
        return self.last_available_score_summary

    @property
    def first_speed_summary(self) -> NumericSummary | None:
        return self.first_available_speed_summary

    @property
    def first_available_speed(self) -> NumericSummary | None:
        return self.first_available_speed_summary

    @property
    def last_speed_summary(self) -> NumericSummary | None:
        return self.last_available_speed_summary

    @property
    def last_available_speed(self) -> NumericSummary | None:
        return self.last_available_speed_summary

    @property
    def score_delta(self) -> float | None:
        return self.score_absolute_delta

    @property
    def percentage_delta(self) -> float | None:
        return self.score_percentage_delta

    @property
    def mean_score_delta(self) -> float | None:
        return self.score_absolute_delta

    @property
    def speed_delta(self) -> float | None:
        return self.speed_absolute_delta

    @property
    def mean_speed_delta(self) -> float | None:
        return self.speed_absolute_delta

    @property
    def missing_buckets(self) -> int:
        return self.missing_bucket_count

    @property
    def first_bucket_start(self) -> str | None:
        return next((point.bucket_start for point in self.points if point.record_count), None)

    @property
    def last_bucket_start(self) -> str | None:
        return next((point.bucket_start for point in reversed(self.points) if point.record_count), None)


@dataclass(frozen=True)
class TrendReport:
    """Structured trend output consumed by Markdown and terminal renderers."""

    metadata: TrendMetadata
    series: tuple[TrendSeries, ...] = ()
    aggregate_series: TrendSeries = field(default_factory=lambda: TrendSeries("overall", "Overall"))
    grouping: TrendGrouping | str = TrendGrouping.OVERALL
    coverage_warnings: tuple[str, ...] = ()
    excluded_data_notes: tuple[str, ...] = ()
    methodology_note: str = (
        "Values are descriptive and use the first and last available bucket means; "
        "the delta direction is last minus first. Missing values are excluded rather "
        "than treated as zero. No smoothing, interpolation, forecasting, significance, "
        "or causal interpretation is performed."
    )
    include_empty_buckets: bool = False
    same_time_range: bool = True
    same_represented_benchmarks: bool | None = None
    same_represented_models: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "series", tuple(self.series))
        object.__setattr__(self, "coverage_warnings", tuple(self.coverage_warnings))
        object.__setattr__(self, "excluded_data_notes", tuple(self.excluded_data_notes))

    @property
    def overall(self) -> TrendSeries:
        return self.aggregate_series

    @property
    def overall_series(self) -> TrendSeries:
        return self.aggregate_series

    @property
    def aggregate(self) -> TrendSeries:
        return self.aggregate_series

    @property
    def contributing_record_count(self) -> int:
        return self.metadata.contributing_record_count

    @property
    def excluded_timestamp_count(self) -> int:
        return self.metadata.excluded_timestamp_count

    @property
    def populated_bucket_count(self) -> int:
        return self.aggregate_series.populated_bucket_count

    @property
    def date_range(self) -> tuple[str | None, str | None]:
        return self.metadata.date_range

    @property
    def coverage_notes(self) -> tuple[str, ...]:
        return self.coverage_warnings

    @property
    def excluded_data(self) -> tuple[str, ...]:
        return self.excluded_data_notes


class TrendService:
    """Build immutable trend models without performing repository writes."""

    def __init__(
        self,
        service: BenchmarkService,
        catalog: CatalogService | None = None,
        statistics: StatisticsService | None = None,
        *,
        statistics_service: StatisticsService | None = None,
    ):
        self.service = service
        self.catalog = catalog or service.catalog
        self.statistics = statistics or statistics_service or StatisticsService(service, self.catalog)

    @staticmethod
    def _benchmark_group(aggregate: Any, grouping: TrendGrouping) -> tuple[str, str]:
        run = aggregate.run
        if grouping is TrendGrouping.OVERALL:
            return "overall", "Overall"
        if grouping is TrendGrouping.MODEL:
            label = model_snapshot_name(run.model_snapshot)
            return normalize_model_identity(label), label or "Unknown model"
        if grouping is TrendGrouping.BENCHMARK:
            label = benchmark_snapshot_name(run.benchmark_snapshot)
            return normalize_benchmark_identity(run.benchmark_snapshot), label or "Unknown benchmark"
        if grouping is TrendGrouping.BENCHMARK_TYPE:
            label = _as_text(run.benchmark_snapshot.get("benchmark_type"))
            return label.casefold() or "unknown", label or "Unknown benchmark type"
        if grouping is TrendGrouping.SESSION:
            session_id = run.session_id if run.session_id is not None else (aggregate.session.id if aggregate.session else None)
            if session_id is None:
                return "unknown", "Unknown session"
            label = _as_text(aggregate.session.title) if aggregate.session else ""
            return f"session:{session_id}", label or f"Session {session_id}"
        from .reporting import hardware_snapshot_label, normalize_hardware_snapshot

        return normalize_hardware_snapshot(run.hardware_snapshot), hardware_snapshot_label(run.hardware_snapshot)

    @staticmethod
    def _scoreboard_group(aggregate: Any, grouping: TrendGrouping) -> tuple[str, str]:
        entry = aggregate.entry
        if grouping is TrendGrouping.OVERALL:
            return "overall", "Overall"
        if grouping is TrendGrouping.MODEL:
            label = _as_text(entry.model_name)
            return normalize_model_identity(label), label or "Unknown model"
        batch_id = entry.import_batch_id
        if batch_id is None or aggregate.batch is None:
            return "unknown", "Unknown import batch"
        label = _as_text(aggregate.batch.name)
        return f"batch:{batch_id}", label or "Unknown import batch"

    @staticmethod
    def _merge_label(current: str, candidate: str) -> str:
        return min((current, candidate), key=lambda item: (item.casefold(), item))

    @staticmethod
    def _benchmark_point(records: Sequence[Any], start: str, label: str, end: str | None) -> TrendPoint:
        from .reporting import hardware_snapshot_label

        count = len(records)
        scores = [aggregate.score.overall_score if aggregate.score else None for aggregate in records]
        speeds = [aggregate.run.model_snapshot.get("tokens_per_second") for aggregate in records]
        score_summary = numeric_summary(scores, total_count=count)
        return TrendPoint(
            bucket_start=start,
            bucket_end=end,
            label=label,
            record_count=count,
            scored_count=score_summary.available_count,
            missing_score_count=score_summary.missing_count,
            overall_score=score_summary,
            tokens_per_second=numeric_summary(speeds, total_count=count),
            hallucination=categorical_distribution(
                [aggregate.score.hallucination_level if aggregate.score else None for aggregate in records],
                total_count=count,
            ),
            reliability=categorical_distribution(
                [aggregate.score.reliability_level if aggregate.score else None for aggregate in records],
                total_count=count,
            ),
            represented_models=_sorted_labels(
                model_snapshot_name(aggregate.run.model_snapshot) or "Unknown model" for aggregate in records
            ),
            represented_benchmarks=_sorted_labels(
                benchmark_snapshot_name(aggregate.run.benchmark_snapshot) or "Unknown benchmark" for aggregate in records
            ),
            represented_sessions=_sorted_labels(
                (
                    _as_text(aggregate.session.title)
                    if aggregate.session and aggregate.session.title
                    else (f"Session {aggregate.run.session_id}" if aggregate.run.session_id is not None else "Unknown session")
                )
                for aggregate in records
            ),
            represented_hardware=_sorted_labels(
                hardware_snapshot_label(aggregate.run.hardware_snapshot)
                for aggregate in records
            ),
        )

    @staticmethod
    def _scoreboard_point(records: Sequence[Any], start: str, label: str, end: str | None) -> TrendPoint:
        count = len(records)
        scores = [aggregate.entry.score for aggregate in records]
        score_summary = numeric_summary(scores, total_count=count)
        return TrendPoint(
            bucket_start=start,
            bucket_end=end,
            label=label,
            record_count=count,
            scored_count=score_summary.available_count,
            missing_score_count=score_summary.missing_count,
            overall_score=score_summary,
            tokens_per_second=numeric_summary(
                [aggregate.entry.tokens_per_second for aggregate in records], total_count=count
            ),
            hallucination=categorical_distribution(
                [aggregate.entry.hallucination_level for aggregate in records], total_count=count
            ),
            consistency=categorical_distribution(
                [aggregate.entry.consistency for aggregate in records], total_count=count
            ),
            reliability=categorical_distribution(
                [aggregate.entry.reliability_score for aggregate in records], total_count=count
            ),
            represented_models=_sorted_labels(aggregate.entry.model_name or "Unknown model" for aggregate in records),
            represented_import_batches=_sorted_labels(
                (
                    aggregate.batch.name
                    if aggregate.batch and aggregate.batch.name
                    else "Unknown import batch"
                )
                for aggregate in records
            ),
        )

    @classmethod
    def _series(
        cls,
        grouped: Mapping[str, tuple[str, list[tuple[str, Any]]]],
        *,
        granularity: TimeBucketGranularity,
        include_empty_buckets: bool,
        point_builder: Any,
    ) -> tuple[TrendSeries, ...]:
        output: list[TrendSeries] = []
        for identity, (label, bucket_records) in sorted(
            grouped.items(), key=lambda item: (item[1][0].casefold(), item[1][0], item[0])
        ):
            buckets: dict[str, list[Any]] = {}
            labels: dict[str, str] = {}
            for start, record in bucket_records:
                bucket = buckets.setdefault(start, [])
                bucket.append(record)
                labels[start] = time_bucket_for(record.run.created_at if hasattr(record, "run") else record.entry.imported_at, granularity)[1]  # type: ignore[index]
            starts = tuple(sorted(buckets))
            point_starts = starts
            if include_empty_buckets and starts:
                point_starts = _bucket_range(starts[0], starts[-1], granularity)
            points: list[TrendPoint] = []
            for start in point_starts:
                end = _next_bucket_start(start, granularity)
                records = buckets.get(start, [])
                point_label = labels.get(start)
                if point_label is None:
                    result = time_bucket_for(start, granularity)
                    point_label = result[1] if result else start
                points.append(point_builder(records, start, point_label, end))
            point_tuple = tuple(points)
            score_points = tuple(
                (point.bucket_start, point.overall_score)
                for point in point_tuple
                if point.overall_score.mean is not None
            )
            speed_points = tuple(
                (point.bucket_start, point.tokens_per_second)
                for point in point_tuple
                if point.tokens_per_second.mean is not None
            )
            first_score = score_points[0][1] if score_points else None
            last_score = score_points[-1][1] if score_points else None
            first_speed = speed_points[0][1] if speed_points else None
            last_speed = speed_points[-1][1] if speed_points else None
            score_delta, score_percentage = (
                _delta(first_score, last_score) if len(score_points) >= 2 else (None, None)
            )
            speed_delta, speed_percentage = (
                _delta(first_speed, last_speed) if len(speed_points) >= 2 else (None, None)
            )
            output.append(
                TrendSeries(
                    identity=identity,
                    label=label,
                    points=point_tuple,
                    total_contributing_records=sum(point.record_count for point in point_tuple),
                    total_scored_records=sum(point.scored_count for point in point_tuple),
                    first_available_score_summary=first_score,
                    last_available_score_summary=last_score,
                    score_absolute_delta=score_delta,
                    score_percentage_delta=score_percentage,
                    first_available_speed_summary=first_speed,
                    last_available_speed_summary=last_speed,
                    speed_absolute_delta=speed_delta,
                    speed_percentage_delta=speed_percentage,
                    missing_bucket_count=sum(1 for point in point_tuple if point.record_count == 0),
                )
            )
        return tuple(output)

    @staticmethod
    def _coverage(
        series: Sequence[TrendSeries],
        aggregate: TrendSeries,
        *,
        source: TrendType,
    ) -> tuple[tuple[str, ...], bool, bool | None, bool | None]:
        warnings: set[str] = set()
        ranges = {(item.first_bucket_start, item.last_bucket_start) for item in series if item.first_bucket_start}
        same_time_range = len(ranges) <= 1
        if len(ranges) > 1:
            warnings.add("series cover different date ranges")
        populated_counts = [item.populated_bucket_count for item in series]
        if len(populated_counts) > 1 and len(set(populated_counts)) > 1:
            warnings.add("sparse series")
        if aggregate.populated_bucket_count == 1:
            warnings.add("only one populated bucket, so no first-to-last delta exists")

        def varies(attribute: str) -> bool:
            observed = {
                frozenset(str(value).casefold() for value in getattr(point, attribute))
                for point in aggregate.points
                if point.record_count
            }
            return len(observed) > 1

        same_benchmarks: bool | None = None
        same_models: bool | None = None
        if source is TrendType.BENCHMARK_RUN:
            benchmark_varies = varies("represented_benchmarks")
            model_varies = varies("represented_models")
            hardware_varies = varies("represented_hardware")
            if benchmark_varies:
                warnings.add("benchmark composition varies across buckets")
            if model_varies:
                warnings.add("model composition varies across buckets")
            if hardware_varies:
                warnings.add("hardware composition varies across buckets")
            if len(series) > 1:
                benchmark_sets = {
                    frozenset(
                        label.casefold()
                        for point in item.points
                        for label in point.represented_benchmarks
                        if point.record_count
                    )
                    for item in series
                }
                model_sets = {
                    frozenset(
                        label.casefold()
                        for point in item.points
                        for label in point.represented_models
                        if point.record_count
                    )
                    for item in series
                }
                same_benchmarks = len(benchmark_sets) <= 1
                same_models = len(model_sets) <= 1
        else:
            model_varies = varies("represented_models")
            if model_varies:
                warnings.add("model composition varies across buckets")
            if len(series) > 1:
                model_sets = {
                    frozenset(
                        label.casefold()
                        for point in item.points
                        for label in point.represented_models
                        if point.record_count
                    )
                    for item in series
                }
                same_models = len(model_sets) <= 1
        return tuple(sorted(warnings)), same_time_range, same_benchmarks, same_models

    @staticmethod
    def _excluded_notes(excluded_timestamp_count: int, *, scoreboard: bool) -> tuple[str, ...]:
        notes: list[str] = []
        if excluded_timestamp_count:
            notes.append(
                f"{excluded_timestamp_count} eligible {('scoreboard entries' if scoreboard else 'BenchmarkRun records')} "
                "were excluded because their timeline timestamp was missing or invalid."
            )
        notes.extend(
            (
                "Missing scores and speeds are excluded from numeric summaries rather than treated as zero.",
                "Soft-deleted source records are excluded by default; historical snapshots remain authoritative.",
            )
        )
        return tuple(notes)

    def benchmark_run_trend(
        self,
        runs: Sequence[Any] | None = None,
        *,
        granularity: str | TimeBucketGranularity = TimeBucketGranularity.DAY,
        interval: str | TimeBucketGranularity | None = None,
        grouping: str | TrendGrouping = TrendGrouping.OVERALL,
        group_by: str | TrendGrouping | None = None,
        filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
        include_empty_buckets: bool = False,
        title: str = "Benchmark Run Trends",
        generated_at: str | None = None,
    ) -> TrendReport:
        active_granularity = _normalize_granularity(interval if interval is not None else granularity)
        active_grouping = _normalize_grouping(group_by if group_by is not None else grouping)
        if active_grouping is TrendGrouping.IMPORT_BATCH:
            raise ValueError("BenchmarkRun trends do not support import-batch grouping")
        selected = self.statistics.select_benchmark_runs(runs, filters=filters)
        valid: list[tuple[str, Any, datetime]] = []
        invalid_count = 0
        for aggregate in selected:
            bucket = time_bucket_for(aggregate.run.created_at, active_granularity)
            parsed = parse_utc_timestamp(aggregate.run.created_at)
            if bucket is None or parsed is None:
                invalid_count += 1
                continue
            valid.append((bucket[0], aggregate, parsed))
        grouped: dict[str, tuple[str, list[tuple[str, Any]]]] = {}
        for start, aggregate, _ in valid:
            identity, label = self._benchmark_group(aggregate, active_grouping)
            if identity in grouped:
                current, records = grouped[identity]
                grouped[identity] = (self._merge_label(current, label), records)
            else:
                grouped[identity] = (label, [])
            grouped[identity][1].append((start, aggregate))
        aggregate_group = {"overall": ("Overall", [(start, aggregate) for start, aggregate, _ in valid])}
        series = self._series(
            grouped,
            granularity=active_granularity,
            include_empty_buckets=include_empty_buckets,
            point_builder=self._benchmark_point,
        )
        aggregate_series = self._series(
            aggregate_group,
            granularity=active_granularity,
            include_empty_buckets=include_empty_buckets,
            point_builder=self._benchmark_point,
        )
        overall = aggregate_series[0] if aggregate_series else TrendSeries("overall", "Overall")
        warnings, same_time_range, same_benchmarks, same_models = self._coverage(
            series, overall, source=TrendType.BENCHMARK_RUN
        )
        metadata = TrendMetadata(
            trend_type=TrendType.BENCHMARK_RUN,
            title=title,
            generated_at=generated_at or now(),
            source_record_family="BenchmarkRun",
            bucket_interval=active_granularity,
            date_range=_date_range(parsed for _, _, parsed in valid),
            contributing_record_count=len(valid),
            excluded_timestamp_count=invalid_count,
            active_filters=_benchmark_filter_mapping(filters),
            selected_grouping=active_grouping,
        )
        return TrendReport(
            metadata=metadata,
            series=series,
            aggregate_series=overall,
            grouping=active_grouping,
            coverage_warnings=warnings,
            excluded_data_notes=self._excluded_notes(invalid_count, scoreboard=False),
            include_empty_buckets=include_empty_buckets,
            same_time_range=same_time_range,
            same_represented_benchmarks=same_benchmarks,
            same_represented_models=same_models,
        )

    def benchmark_run_trends(self, *args: Any, **kwargs: Any) -> TrendReport:
        return self.benchmark_run_trend(*args, **kwargs)

    def build_benchmark_run_trend(self, *args: Any, **kwargs: Any) -> TrendReport:
        return self.benchmark_run_trend(*args, **kwargs)

    def trend_benchmark_runs(self, *args: Any, **kwargs: Any) -> TrendReport:
        return self.benchmark_run_trend(*args, **kwargs)

    def build_benchmark_run_trends(self, *args: Any, **kwargs: Any) -> TrendReport:
        return self.benchmark_run_trend(*args, **kwargs)

    def scoreboard_entry_trend(
        self,
        entries: Sequence[Any] | None = None,
        *,
        batches: Sequence[Any] | None = None,
        granularity: str | TimeBucketGranularity = TimeBucketGranularity.DAY,
        interval: str | TimeBucketGranularity | None = None,
        grouping: str | TrendGrouping = TrendGrouping.OVERALL,
        group_by: str | TrendGrouping | None = None,
        filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters(),
        include_empty_buckets: bool = False,
        title: str = "Historical Scoreboard Trends",
        generated_at: str | None = None,
    ) -> TrendReport:
        active_granularity = _normalize_granularity(interval if interval is not None else granularity)
        active_grouping = _normalize_grouping(group_by if group_by is not None else grouping)
        if active_grouping not in {
            TrendGrouping.OVERALL, TrendGrouping.MODEL, TrendGrouping.IMPORT_BATCH,
        }:
            raise ValueError("Scoreboard trends support overall, model, or import batch grouping")
        selected = self.statistics.select_scoreboard_entries(entries, batches=batches, filters=filters)
        valid: list[tuple[str, Any, datetime]] = []
        invalid_count = 0
        for aggregate in selected:
            bucket = time_bucket_for(aggregate.entry.imported_at, active_granularity)
            parsed = parse_utc_timestamp(aggregate.entry.imported_at)
            if bucket is None or parsed is None:
                invalid_count += 1
                continue
            valid.append((bucket[0], aggregate, parsed))
        grouped: dict[str, tuple[str, list[tuple[str, Any]]]] = {}
        for start, aggregate, _ in valid:
            identity, label = self._scoreboard_group(aggregate, active_grouping)
            if identity in grouped:
                current, records = grouped[identity]
                grouped[identity] = (self._merge_label(current, label), records)
            else:
                grouped[identity] = (label, [])
            grouped[identity][1].append((start, aggregate))
        aggregate_group = {"overall": ("Overall", [(start, aggregate) for start, aggregate, _ in valid])}
        series = self._series(
            grouped,
            granularity=active_granularity,
            include_empty_buckets=include_empty_buckets,
            point_builder=self._scoreboard_point,
        )
        aggregate_series = self._series(
            aggregate_group,
            granularity=active_granularity,
            include_empty_buckets=include_empty_buckets,
            point_builder=self._scoreboard_point,
        )
        overall = aggregate_series[0] if aggregate_series else TrendSeries("overall", "Overall")
        warnings, same_time_range, _, same_models = self._coverage(
            series, overall, source=TrendType.SCOREBOARD_ENTRY
        )
        metadata = TrendMetadata(
            trend_type=TrendType.SCOREBOARD_ENTRY,
            title=title,
            generated_at=generated_at or now(),
            source_record_family="ScoreboardEntry",
            bucket_interval=active_granularity,
            date_range=_date_range(parsed for _, _, parsed in valid),
            contributing_record_count=len(valid),
            excluded_timestamp_count=invalid_count,
            active_filters=_scoreboard_filter_mapping(filters),
            selected_grouping=active_grouping,
        )
        return TrendReport(
            metadata=metadata,
            series=series,
            aggregate_series=overall,
            grouping=active_grouping,
            coverage_warnings=warnings,
            excluded_data_notes=self._excluded_notes(invalid_count, scoreboard=True),
            include_empty_buckets=include_empty_buckets,
            same_time_range=same_time_range,
            same_represented_models=same_models,
        )

    def scoreboard_entry_trends(self, *args: Any, **kwargs: Any) -> TrendReport:
        return self.scoreboard_entry_trend(*args, **kwargs)

    def build_scoreboard_entry_trend(self, *args: Any, **kwargs: Any) -> TrendReport:
        return self.scoreboard_entry_trend(*args, **kwargs)

    def build_scoreboard_entry_trends(self, *args: Any, **kwargs: Any) -> TrendReport:
        return self.scoreboard_entry_trend(*args, **kwargs)

    def trend_scoreboard_entries(self, *args: Any, **kwargs: Any) -> TrendReport:
        return self.scoreboard_entry_trend(*args, **kwargs)

    def scoreboard_trend(self, *args: Any, **kwargs: Any) -> TrendReport:
        return self.scoreboard_entry_trend(*args, **kwargs)

    def scoreboard_trends(self, *args: Any, **kwargs: Any) -> TrendReport:
        return self.scoreboard_entry_trend(*args, **kwargs)

    def build_scoreboard_trend(self, *args: Any, **kwargs: Any) -> TrendReport:
        return self.scoreboard_entry_trend(*args, **kwargs)


__all__ = (
    "BenchmarkTrendGrouping",
    "ScoreboardTrendGrouping",
    "TrendGrouping",
    "TrendMetadata",
    "TrendPoint",
    "TrendReport",
    "TrendSeries",
    "TrendService",
    "TrendType",
)
