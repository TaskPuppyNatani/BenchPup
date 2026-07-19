"""Typed, offline HTML analytics for BenchPup.

This module is the engine-owned boundary for the richer Phase 4 HTML report.
It prepares every metric and chart point before rendering.  The generated
document contains only inline assets and safe, precomputed data; browser
JavaScript is limited to presentation controls and table interaction.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from html import escape
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from .domain import BenchmarkRun, ScoreboardEntry, ScoreboardImportBatch, now
from .reporting import (
    BenchmarkReportFilters,
    BenchmarkRunAggregate,
    ReportWriteResult,
    ReportWriteStatus,
    ScoreboardEntryAggregate,
    ScoreboardReportFilters,
    build_benchmark_run_report,
    build_scoreboard_report,
    hardware_snapshot_label,
)
from .services import BenchmarkService, CatalogService
from .statistics import (
    BenchmarkRunGroupBy,
    BenchmarkStatisticsFilters,
    CategoricalDistribution,
    NumericSummary,
    ScoreboardGroupBy,
    ScoreboardStatisticsFilters,
    StatisticsService,
    TimeBucketGranularity,
)
from .trends import TrendPoint, TrendReport, TrendService


class AnalyticsSourceFamily(str, Enum):
    """The independent record family or families shown in one report."""

    BENCHMARK_RUNS = "benchmark_runs"
    BENCHMARK_RUN = "benchmark_runs"
    SCOREBOARD = "scoreboard"
    HISTORICAL_SCOREBOARD = "scoreboard"
    COMBINED = "combined"


class ChartType(str, Enum):
    BAR = "bar"
    LINE = "line"
    DISTRIBUTION = "distribution"
    TABLE = "table"


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], _freeze_value(dict(value or {})))


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value)
    return text if text else default


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _display_number(value: Any) -> str:
    number = _finite_number(value)
    if number is None:
        return "Unavailable"
    return f"{number:g}"


def _label(value: Any, fallback: str) -> str:
    text = _as_text(value).strip()
    return text or fallback


def _distinct_labels(values: Sequence[str]) -> tuple[str, ...]:
    labels: dict[str, str] = {}
    for value in values:
        label = _as_text(value).strip()
        if not label:
            continue
        key = label.casefold()
        current = labels.get(key)
        if current is None or (label.casefold(), label) < (current.casefold(), current):
            labels[key] = label
    return tuple(sorted(labels.values(), key=lambda item: (item.casefold(), item)))


def _normalize_source_family(value: AnalyticsSourceFamily | str) -> AnalyticsSourceFamily:
    if isinstance(value, AnalyticsSourceFamily):
        return value
    normalized = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "benchmark_run": AnalyticsSourceFamily.BENCHMARK_RUNS,
        "benchmark_runs": AnalyticsSourceFamily.BENCHMARK_RUNS,
        "run": AnalyticsSourceFamily.BENCHMARK_RUNS,
        "scoreboard": AnalyticsSourceFamily.SCOREBOARD,
        "scoreboard_entries": AnalyticsSourceFamily.SCOREBOARD,
        "historical_scoreboard": AnalyticsSourceFamily.SCOREBOARD,
        "combined": AnalyticsSourceFamily.COMBINED,
    }
    try:
        return aliases[normalized]
    except KeyError as error:
        raise ValueError(f"Unsupported HTML analytics source family: {value}") from error


def _normalize_interval(value: TimeBucketGranularity | str) -> TimeBucketGranularity:
    if isinstance(value, TimeBucketGranularity):
        return value
    normalized = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    try:
        return TimeBucketGranularity(normalized)
    except ValueError as error:
        raise ValueError(f"Unsupported HTML analytics trend interval: {value}") from error


@dataclass(frozen=True)
class ChartPoint:
    """One engine-prepared point with explicit unavailable-value semantics."""

    key: str
    label: str
    value: float | int | None = None
    secondary_value: float | int | None = None
    tooltip_metadata: Mapping[str, Any] = field(default_factory=dict)
    contributing_record_count: int = 0
    missing_value_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "tooltip_metadata", _freeze_mapping(self.tooltip_metadata))

    @property
    def stable_key(self) -> str:
        return self.key

    @property
    def tooltip(self) -> Mapping[str, Any]:
        return self.tooltip_metadata

    @property
    def available(self) -> bool:
        return _finite_number(self.value) is not None


@dataclass(frozen=True)
class ChartSeries:
    """A deterministic series of chart points."""

    series_id: str
    label: str
    points: tuple[ChartPoint, ...] = ()
    category: str | None = None
    unit: str | None = None
    unavailable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", tuple(self.points))

    @property
    def identity(self) -> str:
        return self.series_id

    @property
    def stable_identity(self) -> str:
        return self.series_id


@dataclass(frozen=True)
class ChartMetadata:
    """Presentation metadata for one chart dataset."""

    chart_id: str
    title: str
    subtitle: str
    chart_type: ChartType | str
    x_axis_label: str
    y_axis_label: str
    value_format: Mapping[str, Any] = field(default_factory=dict)
    missing_value_behavior: str = "Unavailable values remain unavailable and are not treated as zero."
    source_record_family: str = ""
    active_filters: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value_format", _freeze_mapping(self.value_format))
        object.__setattr__(self, "active_filters", cast(Mapping[str, str], _freeze_mapping(self.active_filters)))

    @property
    def identifier(self) -> str:
        return self.chart_id

    @property
    def type(self) -> str:
        return self.chart_type.value if isinstance(self.chart_type, ChartType) else str(self.chart_type)


@dataclass(frozen=True)
class ChartData:
    """Typed chart metadata and the immutable points rendered by HTML."""

    metadata: ChartMetadata
    series: tuple[ChartSeries, ...] = ()
    empty_message: str = "No meaningful values are available for this chart."

    def __post_init__(self) -> None:
        object.__setattr__(self, "series", tuple(self.series))

    @property
    def chart_id(self) -> str:
        return self.metadata.chart_id

    @property
    def identifier(self) -> str:
        return self.metadata.chart_id

    @property
    def title(self) -> str:
        return self.metadata.title

    @property
    def chart_type(self) -> str:
        return self.metadata.type


ChartDataset = ChartData


@dataclass(frozen=True)
class OmittedChart:
    chart_id: str
    title: str
    reason: str

    @property
    def identifier(self) -> str:
        return self.chart_id


@dataclass(frozen=True)
class DashboardMetadata:
    """Summary cards and coverage metadata for one source-family dashboard."""

    generated_at: str
    contributing_record_count: int = 0
    scored_record_count: int = 0
    represented_models: tuple[str, ...] = ()
    represented_benchmarks: tuple[str, ...] = ()
    represented_sessions: tuple[str, ...] = ()
    represented_hardware_environments: tuple[str, ...] = ()
    represented_import_batches: tuple[str, ...] = ()
    date_range: tuple[str | None, str | None] = (None, None)
    active_filters: Mapping[str, str] = field(default_factory=dict)
    coverage_warnings: tuple[str, ...] = ()
    mean_overall_score: float | None = None
    median_overall_score: float | None = None
    mean_tokens_per_second: float | None = None

    def __post_init__(self) -> None:
        for name in (
            "represented_models",
            "represented_benchmarks",
            "represented_sessions",
            "represented_hardware_environments",
            "represented_import_batches",
            "coverage_warnings",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "date_range", tuple(self.date_range))
        object.__setattr__(self, "active_filters", cast(Mapping[str, str], _freeze_mapping(self.active_filters)))

    @property
    def generated_timestamp(self) -> str:
        return self.generated_at

    @property
    def record_count(self) -> int:
        return self.contributing_record_count

    @property
    def scored_count(self) -> int:
        return self.scored_record_count

    @property
    def represented_hardware(self) -> tuple[str, ...]:
        return self.represented_hardware_environments

    @property
    def date_from(self) -> str | None:
        return self.date_range[0]

    @property
    def date_to(self) -> str | None:
        return self.date_range[1]


@dataclass(frozen=True)
class AnalyticsTableRow:
    """Safe, presentation-only row data; raw output and prompt text are excluded."""

    values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", _freeze_mapping(self.values))


@dataclass(frozen=True)
class AnalyticsDashboard:
    source_record_family: str
    title: str
    metadata: DashboardMetadata
    charts: tuple[ChartData, ...] = ()
    omitted_charts: tuple[OmittedChart, ...] = ()
    table_columns: tuple[str, ...] = ()
    table_rows: tuple[AnalyticsTableRow, ...] = ()
    comparison_summaries: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "charts", tuple(self.charts))
        object.__setattr__(self, "omitted_charts", tuple(self.omitted_charts))
        object.__setattr__(self, "table_columns", tuple(self.table_columns))
        object.__setattr__(self, "table_rows", tuple(self.table_rows))
        object.__setattr__(
            self,
            "comparison_summaries",
            tuple(_freeze_mapping(item) for item in self.comparison_summaries),
        )

    @property
    def dashboard_metadata(self) -> DashboardMetadata:
        return self.metadata

    @property
    def chart_datasets(self) -> tuple[ChartData, ...]:
        return self.charts

    @property
    def included_charts(self) -> tuple[str, ...]:
        return tuple(chart.chart_id for chart in self.charts)


@dataclass(frozen=True)
class HtmlAnalyticsReportOptions:
    """Session-local presentation choices kept separate from record filters."""

    title: str = "BenchPup HTML Analytics"
    source_family: AnalyticsSourceFamily | str = AnalyticsSourceFamily.BENCHMARK_RUNS
    include_benchmark_run_dashboard: bool | None = None
    include_scoreboard_dashboard: bool | None = None
    include_detailed_tables: bool = True
    include_model_quality_chart: bool = True
    include_speed_chart: bool = True
    include_score_distribution: bool = True
    include_categorical_distributions: bool = True
    include_trends: bool = True
    trend_interval: TimeBucketGranularity | str = TimeBucketGranularity.DAY
    include_comparison_summaries: bool = False
    include_hardware_summary: bool = False
    compact_layout: bool = False
    active_filters: Mapping[str, str] = field(default_factory=dict)
    benchmark_filters: BenchmarkStatisticsFilters = field(default_factory=BenchmarkStatisticsFilters)
    scoreboard_filters: ScoreboardStatisticsFilters = field(default_factory=ScoreboardStatisticsFilters)
    include_empty_trend_buckets: bool = False
    output_destination: str | Path | None = None
    destination: str | Path | None = None
    overwrite: bool = False

    def __post_init__(self) -> None:
        source = _normalize_source_family(self.source_family)
        interval = _normalize_interval(self.trend_interval)
        include_runs = self.include_benchmark_run_dashboard
        include_scoreboard = self.include_scoreboard_dashboard
        if include_runs is None:
            include_runs = source in {AnalyticsSourceFamily.BENCHMARK_RUNS, AnalyticsSourceFamily.COMBINED}
        if include_scoreboard is None:
            include_scoreboard = source in {AnalyticsSourceFamily.SCOREBOARD, AnalyticsSourceFamily.COMBINED}
        if not include_runs and not include_scoreboard:
            raise ValueError("HTML analytics must include at least one dashboard")
        object.__setattr__(self, "source_family", source)
        object.__setattr__(self, "trend_interval", interval)
        object.__setattr__(self, "include_benchmark_run_dashboard", bool(include_runs))
        object.__setattr__(self, "include_scoreboard_dashboard", bool(include_scoreboard))
        object.__setattr__(self, "active_filters", cast(Mapping[str, str], _freeze_mapping(self.active_filters)))
        if self.output_destination is None and self.destination is not None:
            object.__setattr__(self, "output_destination", self.destination)

    @property
    def include_benchmark_runs(self) -> bool:
        return bool(self.include_benchmark_run_dashboard)

    @property
    def include_scoreboard(self) -> bool:
        return bool(self.include_scoreboard_dashboard)

    @property
    def interval(self) -> TimeBucketGranularity:
        return cast(TimeBucketGranularity, self.trend_interval)

    @property
    def compact(self) -> bool:
        return self.compact_layout

    @property
    def output_path(self) -> Path | None:
        return Path(self.output_destination) if self.output_destination is not None else None


AnalyticsReportOptions = HtmlAnalyticsReportOptions
HtmlReportOptions = HtmlAnalyticsReportOptions


@dataclass(frozen=True)
class HtmlAnalyticsReport:
    title: str
    source_family: AnalyticsSourceFamily | str
    generated_at: str
    dashboards: tuple[AnalyticsDashboard, ...]
    options: HtmlAnalyticsReportOptions

    def __post_init__(self) -> None:
        object.__setattr__(self, "dashboards", tuple(self.dashboards))
        object.__setattr__(self, "source_family", _normalize_source_family(self.source_family))

    @property
    def dashboard_metadata(self) -> tuple[DashboardMetadata, ...]:
        return tuple(dashboard.metadata for dashboard in self.dashboards)

    @property
    def charts(self) -> tuple[ChartData, ...]:
        return tuple(chart for dashboard in self.dashboards for chart in dashboard.charts)

    @property
    def omitted_charts(self) -> tuple[OmittedChart, ...]:
        return tuple(chart for dashboard in self.dashboards for chart in dashboard.omitted_charts)

    @property
    def included_chart_ids(self) -> tuple[str, ...]:
        return tuple(chart.chart_id for chart in self.charts)

    @property
    def omitted_chart_ids(self) -> tuple[str, ...]:
        return tuple(chart.chart_id for chart in self.omitted_charts)


def _stringify_filter(value: Any) -> str:
    if isinstance(value, (set, frozenset, tuple, list)):
        return ",".join(str(item) for item in sorted(value))
    return str(value)


def _filter_mapping(filters: Any, *, prefix: str = "") -> dict[str, str]:
    result: dict[str, str] = {}
    for item in fields(filters):
        name = item.name
        value = getattr(filters, name)
        if value in (None, "", frozenset(), (), []):
            continue
        if name in {"include_deleted"} and value is False:
            continue
        key = f"{prefix}.{name}" if prefix else name
        result[key] = _stringify_filter(value)
    return result


def _active_filters(options: HtmlAnalyticsReportOptions, family: str) -> Mapping[str, str]:
    result = dict(options.active_filters)
    if family == "BenchmarkRun":
        source = _filter_mapping(options.benchmark_filters)
        result.update({key: value for key, value in source.items() if key not in result})
        result.update({f"benchmark.{key}": value for key, value in source.items()})
    else:
        source = _filter_mapping(options.scoreboard_filters)
        result.update({key: value for key, value in source.items() if key not in result})
        result.update({f"scoreboard.{key}": value for key, value in source.items()})
    return cast(Mapping[str, str], _freeze_mapping(result))


def _run_model(aggregate: Any) -> str:
    snapshot = aggregate.run.model_snapshot
    return _label(snapshot.get("model_name") or snapshot.get("name"), "Unknown model")


def _run_benchmark(aggregate: Any) -> str:
    snapshot = aggregate.run.benchmark_snapshot
    return _label(snapshot.get("name") or snapshot.get("benchmark_file") or snapshot.get("file_path"), "Custom benchmark")


def _run_benchmark_type(aggregate: Any) -> str:
    return _label(aggregate.run.benchmark_snapshot.get("benchmark_type"), "Unknown benchmark type")


def _run_session(aggregate: Any) -> str:
    if aggregate.session is not None and aggregate.session.title:
        return aggregate.session.title
    return f"Session {aggregate.run.session_id}" if aggregate.run.session_id is not None else "Unknown session"


def _run_hardware(aggregate: Any) -> str:
    return hardware_snapshot_label(aggregate.run.hardware_snapshot)


def _score(aggregate: Any) -> float | None:
    return _finite_number(aggregate.score.overall_score if aggregate.score else None)


def _run_speed(aggregate: Any) -> float | None:
    return _finite_number(aggregate.run.model_snapshot.get("tokens_per_second"))


def _entry_speed(aggregate: Any) -> float | None:
    return _finite_number(aggregate.entry.tokens_per_second)


def _entry_batch(aggregate: Any) -> str:
    if aggregate.batch is not None and aggregate.batch.name:
        return aggregate.batch.name
    if aggregate.entry.import_batch_id is not None:
        return f"Import batch {aggregate.entry.import_batch_id}"
    return "Unknown import batch"


def _numeric_points(
    groups: Sequence[Any],
    *,
    chart_id: str,
    title: str,
    subtitle: str,
    source_family: str,
    filters: Mapping[str, str],
    metric_name: str,
    unit: str,
    secondary_label: str,
) -> ChartData:
    points: list[ChartPoint] = []
    for group in groups:
        summary: NumericSummary = getattr(group.summary, metric_name)
        points.append(
            ChartPoint(
                key=_as_text(group.key, group.label),
                label=_label(group.label, "Unknown"),
                value=summary.mean,
                secondary_value=summary.available_count,
                tooltip_metadata={
                    "record_count": group.record_count,
                    secondary_label: summary.available_count,
                    "missing_value_count": summary.missing_count,
                    "minimum": summary.minimum,
                    "maximum": summary.maximum,
                },
                contributing_record_count=group.record_count,
                missing_value_count=summary.missing_count,
            )
        )
    metadata = ChartMetadata(
        chart_id=chart_id,
        title=title,
        subtitle=subtitle,
        chart_type=ChartType.BAR,
        x_axis_label="Category",
        y_axis_label=unit,
        value_format={"kind": "decimal", "decimals": 2, "unit": unit},
        source_record_family=source_family,
        active_filters=filters,
    )
    return ChartData(
        metadata=metadata,
        series=(ChartSeries(f"{chart_id}-series", title, tuple(points), unit=unit),),
    )


def _distribution_points(
    distribution: CategoricalDistribution | Mapping[Any, int],
    *,
    chart_id: str,
    title: str,
    subtitle: str,
    source_family: str,
    filters: Mapping[str, str],
    numeric_labels: bool = False,
) -> ChartData:
    if isinstance(distribution, CategoricalDistribution):
        counts = distribution.counts
        percentages = distribution.percentages
        total = distribution.observed_count
        missing = distribution.missing_count
    else:
        counts = distribution
        total = sum(int(value) for value in counts.values())
        percentages = {str(key): (int(value) / total * 100.0 if total else 0.0) for key, value in counts.items()}
        missing = 0
    ordered = list(counts.items())
    if numeric_labels:
        ordered.sort(key=lambda item: (_finite_number(item[0]) is None, _finite_number(item[0]) or 0.0, str(item[0])))
    else:
        ordered.sort(key=lambda item: (str(item[0]).casefold(), str(item[0])))
    points = tuple(
        ChartPoint(
            key=f"{chart_id}:{str(key)}",
            label=_label(key, "Unknown"),
            value=int(count),
            secondary_value=float(percentages.get(key, percentages.get(str(key), 0.0))),
            tooltip_metadata={"count": int(count), "percentage": percentages.get(key, percentages.get(str(key), 0.0))},
            contributing_record_count=total,
            missing_value_count=missing,
        )
        for key, count in ordered
    )
    metadata = ChartMetadata(
        chart_id=chart_id,
        title=title,
        subtitle=subtitle,
        chart_type=ChartType.DISTRIBUTION,
        x_axis_label="Category",
        y_axis_label="Records",
        value_format={"kind": "count", "secondary": "percentage"},
        source_record_family=source_family,
        active_filters=filters,
    )
    return ChartData(
        metadata=metadata,
        series=(ChartSeries(f"{chart_id}-series", title, points, unit="records"),),
    )


def _trend_points(
    trend: TrendReport,
    *,
    chart_id: str,
    title: str,
    subtitle: str,
    source_family: str,
    filters: Mapping[str, str],
    speed: bool = False,
) -> ChartData:
    points: list[ChartPoint] = []
    for point in trend.aggregate_series.points:
        summary = point.tokens_per_second if speed else point.overall_score
        points.append(
            ChartPoint(
                key=point.bucket_start,
                label=point.label,
                value=summary.mean,
                secondary_value=point.record_count,
                tooltip_metadata={
                    "record_count": point.record_count,
                    "scored_count": point.scored_count,
                    "missing_value_count": summary.missing_count,
                    "bucket_start": point.bucket_start,
                },
                contributing_record_count=point.record_count,
                missing_value_count=summary.missing_count,
            )
        )
    unit = "tokens/s" if speed else "overall score"
    metadata = ChartMetadata(
        chart_id=chart_id,
        title=title,
        subtitle=subtitle,
        chart_type=ChartType.LINE,
        x_axis_label=f"{trend.metadata.interval.title()} bucket",
        y_axis_label=unit,
        value_format={"kind": "decimal", "decimals": 2, "unit": unit},
        missing_value_behavior="Missing bucket values remain unavailable; no interpolation is performed.",
        source_record_family=source_family,
        active_filters=filters,
    )
    return ChartData(
        metadata=metadata,
        series=(ChartSeries(f"{chart_id}-series", "Overall", tuple(points), unit=unit),),
    )


def _safe_comparison_summaries(reports: Sequence[Any]) -> tuple[Mapping[str, Any], ...]:
    """Extract small metadata-only comparison summaries without raw content."""

    summaries: list[Mapping[str, Any]] = []
    for report in reports:
        metadata = getattr(report, "metadata", None)
        if metadata is None:
            continue
        values: dict[str, Any] = {"type": type(report).__name__}
        for name in (
            "title",
            "comparison_type",
            "source_record_family",
            "contributing_record_count",
            "selected_entities",
        ):
            value = getattr(metadata, name, None)
            if value not in (None, ""):
                values[name] = value.value if isinstance(value, Enum) else value
        summaries.append(_freeze_mapping(values))
    return tuple(summaries)


def _benchmark_table(
    records: Sequence[Any],
) -> tuple[tuple[str, ...], tuple[AnalyticsTableRow, ...]]:
    columns = (
        "Model",
        "Benchmark",
        "Benchmark type",
        "Session",
        "Hardware",
        "Overall score",
        "Tokens/s",
        "Hallucination",
        "Reliability",
        "Created at",
    )
    rows = tuple(
        AnalyticsTableRow(
            {
                "Model": _run_model(aggregate),
                "Benchmark": _run_benchmark(aggregate),
                "Benchmark type": _run_benchmark_type(aggregate),
                "Session": _run_session(aggregate),
                "Hardware": _run_hardware(aggregate),
                "Overall score": _score(aggregate),
                "Tokens/s": _run_speed(aggregate),
                "Hallucination": aggregate.score.hallucination_level if aggregate.score else None,
                "Reliability": aggregate.score.reliability_level if aggregate.score else None,
                "Created at": aggregate.run.created_at,
            }
        )
        for aggregate in sorted(records, key=lambda item: (_run_model(item).casefold(), item.run.created_at, item.run.id or 0))
    )
    return columns, rows


def _scoreboard_table(
    records: Sequence[Any],
) -> tuple[tuple[str, ...], tuple[AnalyticsTableRow, ...]]:
    columns = (
        "Model",
        "Import batch",
        "Score",
        "Tokens/s",
        "Hallucination",
        "Consistency",
        "Reliability",
        "Verdict",
        "Notes",
        "Imported at",
    )
    rows = tuple(
        AnalyticsTableRow(
            {
                "Model": aggregate.entry.model_name,
                "Import batch": _entry_batch(aggregate),
                "Score": _finite_number(aggregate.entry.score),
                "Tokens/s": _entry_speed(aggregate),
                "Hallucination": aggregate.entry.hallucination_level or None,
                "Consistency": aggregate.entry.consistency or None,
                "Reliability": aggregate.entry.reliability_score or None,
                "Verdict": aggregate.entry.verdict or None,
                "Notes": aggregate.entry.notes or None,
                "Imported at": aggregate.entry.imported_at,
            }
        )
        for aggregate in sorted(records, key=lambda item: (str(item.entry.model_name).casefold(), item.entry.imported_at, item.entry.id or 0))
    )
    return columns, rows


def _append_chart(
    charts: list[ChartData],
    omitted: list[OmittedChart],
    chart: ChartData,
    *,
    title: str,
    meaningful: bool,
    reason: str,
) -> None:
    if meaningful:
        charts.append(chart)
    else:
        omitted.append(OmittedChart(chart.chart_id, title, reason))


def _benchmark_dashboard(
    records: Sequence[Any],
    *,
    statistics: StatisticsService,
    trends: TrendService,
    service: Any,
    catalog: Any,
    options: HtmlAnalyticsReportOptions,
    generated_at: str,
    comparison_reports: Sequence[Any],
) -> AnalyticsDashboard:
    filters = _active_filters(options, "BenchmarkRun")
    selected = tuple(records)
    summary = statistics.benchmark_run_statistics(selected)
    groups_model = statistics.group_benchmark_runs(selected, group_by=BenchmarkRunGroupBy.MODEL)
    groups_benchmark = statistics.group_benchmark_runs(selected, group_by=BenchmarkRunGroupBy.BENCHMARK)
    charts: list[ChartData] = []
    omitted: list[OmittedChart] = []

    if options.include_model_quality_chart:
        chart = _numeric_points(
            groups_model,
            chart_id="model_quality",
            title="Model quality",
            subtitle="Mean overall score by authoritative model snapshot; scored-run count is shown in each point.",
            source_family="BenchmarkRun",
            filters=filters,
            metric_name="overall_score",
            unit="overall score",
            secondary_label="scored_run_count",
        )
        _append_chart(charts, omitted, chart, title=chart.title, meaningful=bool(groups_model), reason="No eligible model groups are available.")

    if options.include_speed_chart:
        chart = _numeric_points(
            groups_model,
            chart_id="model_speed",
            title="Model speed",
            subtitle="Mean tokens per second by model; missing speeds remain unavailable and are not zero-filled.",
            source_family="BenchmarkRun",
            filters=filters,
            metric_name="tokens_per_second",
            unit="tokens/s",
            secondary_label="available_speed_count",
        )
        _append_chart(
            charts,
            omitted,
            chart,
            title=chart.title,
            meaningful=summary.tokens_per_second.available_count > 0,
            reason="No tokens-per-second values are available.",
        )

    detail_report = build_benchmark_run_report(
        selected,
        service=service,
        catalog=catalog,
        filters=BenchmarkReportFilters(include_deleted=True),
        generated_at=generated_at,
    )
    if options.include_score_distribution:
        chart = _distribution_points(
            detail_report.summary.score_distribution,
            chart_id="score_distribution",
            title="Overall score distribution",
            subtitle="Exact observed overall score categories from eligible runs; no smoothing is applied.",
            source_family="BenchmarkRun",
            filters=filters,
            numeric_labels=True,
        )
        _append_chart(
            charts,
            omitted,
            chart,
            title=chart.title,
            meaningful=summary.overall_score.available_count > 0,
            reason="No scored runs are available.",
        )

    if options.include_categorical_distributions:
        for chart_id, title, subtitle, distribution in (
            (
                "hallucination_distribution",
                "Hallucination distribution",
                "Counts use StatisticsService categorical definitions over observed review levels.",
                summary.hallucination,
            ),
            (
                "reliability_distribution",
                "Reliability distribution",
                "Counts use StatisticsService categorical definitions over observed review levels.",
                summary.reliability,
            ),
        ):
            chart = _distribution_points(
                distribution,
                chart_id=chart_id,
                title=title,
                subtitle=subtitle,
                source_family="BenchmarkRun",
                filters=filters,
            )
            _append_chart(
                charts,
                omitted,
                chart,
                title=chart.title,
                meaningful=distribution.observed_count > 0,
                reason="No observed categorical values are available.",
            )

    if options.include_trends:
        trend = trends.benchmark_run_trend(
            selected,
            interval=options.interval,
            include_empty_buckets=options.include_empty_trend_buckets,
            title="Benchmark Run Score Trend",
            generated_at=generated_at,
        )
        trend_filters = cast(Mapping[str, str], _freeze_mapping({**filters, **dict(trend.metadata.active_filters)}))
        coverage = list(trend.excluded_data_notes)
        coverage.extend(trend.coverage_warnings)
        score_chart = _trend_points(
            trend,
            chart_id="score_trend",
            title="Score trend",
            subtitle="Mean overall score by UTC bucket from TrendService; values are descriptive and chronological.",
            source_family="BenchmarkRun",
            filters=trend_filters,
        )
        _append_chart(
            charts,
            omitted,
            score_chart,
            title=score_chart.title,
            meaningful=any(point.available for point in score_chart.series[0].points),
            reason="No valid timestamped score values are available for a trend.",
        )
        speed_chart = _trend_points(
            trend,
            chart_id="speed_trend",
            title="Speed trend",
            subtitle="Mean tokens per second by UTC bucket from TrendService; missing buckets are not interpolated.",
            source_family="BenchmarkRun",
            filters=trend_filters,
            speed=True,
        )
        _append_chart(
            charts,
            omitted,
            speed_chart,
            title=speed_chart.title,
            meaningful=any(point.available for point in speed_chart.series[0].points),
            reason="No valid timestamped speed values are available for a trend.",
        )
    else:
        coverage = []

    benchmark_chart = _numeric_points(
        groups_benchmark,
        chart_id="benchmark_summary",
        title="Benchmark summary",
        subtitle="Mean overall score and scored-run count by benchmark snapshot.",
        source_family="BenchmarkRun",
        filters=filters,
        metric_name="overall_score",
        unit="overall score",
        secondary_label="scored_run_count",
    )
    if groups_benchmark:
        charts.append(benchmark_chart)
    elif options.include_score_distribution:
        omitted.append(OmittedChart(benchmark_chart.chart_id, benchmark_chart.title, "No eligible benchmark groups are available."))

    if options.include_hardware_summary:
        groups_hardware = statistics.group_benchmark_runs(selected, group_by=BenchmarkRunGroupBy.HARDWARE)
        hardware_chart = _numeric_points(
            groups_hardware,
            chart_id="hardware_summary",
            title="Hardware summary",
            subtitle="Mean overall score by normalized historical hardware snapshot.",
            source_family="BenchmarkRun",
            filters=filters,
            metric_name="overall_score",
            unit="overall score",
            secondary_label="scored_run_count",
        )
        _append_chart(
            charts,
            omitted,
            hardware_chart,
            title=hardware_chart.title,
            meaningful=bool(groups_hardware),
            reason="No historical hardware groups are available.",
        )

    columns, rows = _benchmark_table(selected) if options.include_detailed_tables else ((), ())
    warnings = tuple(dict.fromkeys(coverage))
    metadata = DashboardMetadata(
        generated_at=generated_at,
        contributing_record_count=summary.total_eligible_runs,
        scored_record_count=summary.scored_runs,
        represented_models=_distinct_labels([_run_model(item) for item in selected]),
        represented_benchmarks=_distinct_labels([_run_benchmark(item) for item in selected]),
        represented_sessions=_distinct_labels([_run_session(item) for item in selected]),
        represented_hardware_environments=_distinct_labels([_run_hardware(item) for item in selected]),
        date_range=(summary.created_at_min, summary.created_at_max),
        active_filters=filters,
        coverage_warnings=warnings,
        mean_overall_score=summary.overall_score.mean,
        median_overall_score=summary.overall_score.median,
        mean_tokens_per_second=summary.tokens_per_second.mean,
    )
    comparison = _safe_comparison_summaries(comparison_reports) if options.include_comparison_summaries else ()
    return AnalyticsDashboard(
        source_record_family="BenchmarkRun",
        title="Benchmark Run Analytics",
        metadata=metadata,
        charts=tuple(charts),
        omitted_charts=tuple(omitted),
        table_columns=columns,
        table_rows=rows,
        comparison_summaries=comparison,
    )


def _scoreboard_dashboard(
    records: Sequence[Any],
    *,
    batches: Sequence[Any],
    statistics: StatisticsService,
    trends: TrendService,
    service: Any,
    catalog: Any,
    options: HtmlAnalyticsReportOptions,
    generated_at: str,
    comparison_reports: Sequence[Any],
) -> AnalyticsDashboard:
    filters = _active_filters(options, "ScoreboardEntry")
    selected = tuple(records)
    summary = statistics.scoreboard_statistics(selected, batches=batches)
    groups_model = statistics.group_scoreboard_entries(selected, batches=batches, group_by=ScoreboardGroupBy.MODEL)
    groups_batch = statistics.group_scoreboard_entries(selected, batches=batches, group_by=ScoreboardGroupBy.IMPORT_BATCH)
    charts: list[ChartData] = []
    omitted: list[OmittedChart] = []

    score_chart = _numeric_points(
        groups_model,
        chart_id="score_by_model",
        title="Score by model",
        subtitle="Mean historical score by model; missing scores remain unavailable.",
        source_family="ScoreboardEntry",
        filters=filters,
        metric_name="score",
        unit="score",
        secondary_label="scored_entry_count",
    )
    if options.include_model_quality_chart:
        _append_chart(charts, omitted, score_chart, title=score_chart.title, meaningful=bool(groups_model), reason="No eligible model groups are available.")

    speed_chart = _numeric_points(
        groups_model,
        chart_id="speed_by_model",
        title="Speed by model",
        subtitle="Mean historical tokens per second by model; missing speeds are not zero-filled.",
        source_family="ScoreboardEntry",
        filters=filters,
        metric_name="tokens_per_second",
        unit="tokens/s",
        secondary_label="available_speed_count",
    )
    if options.include_speed_chart:
        _append_chart(
            charts,
            omitted,
            speed_chart,
            title=speed_chart.title,
            meaningful=summary.tokens_per_second.available_count > 0,
            reason="No tokens-per-second values are available.",
        )

    detail_report = build_scoreboard_report(
        selected,
        batches=batches,
        catalog=catalog,
        filters=ScoreboardReportFilters(include_deleted=True),
        title="Historical Scoreboard Analytics",
        generated_at=generated_at,
    )
    if options.include_score_distribution:
        chart = _distribution_points(
            detail_report.summary.score_distribution,
            chart_id="score_distribution",
            title="Score distribution",
            subtitle="Exact observed historical score categories; no smoothing is applied.",
            source_family="ScoreboardEntry",
            filters=filters,
            numeric_labels=True,
        )
        _append_chart(
            charts,
            omitted,
            chart,
            title=chart.title,
            meaningful=summary.score.available_count > 0,
            reason="No scored entries are available.",
        )

    if options.include_categorical_distributions:
        for chart_id, title, field in (
            ("hallucination_distribution", "Hallucination distribution", summary.hallucination),
            ("consistency_distribution", "Consistency distribution", summary.consistency),
            ("reliability_distribution", "Reliability distribution", summary.reliability),
        ):
            chart = _distribution_points(
                field,
                chart_id=chart_id,
                title=title,
                subtitle="Counts and percentages use StatisticsService observed-category definitions.",
                source_family="ScoreboardEntry",
                filters=filters,
            )
            _append_chart(
                charts,
                omitted,
                chart,
                title=chart.title,
                meaningful=field.observed_count > 0,
                reason="No observed categorical values are available.",
            )

    if options.include_trends:
        trend = trends.scoreboard_entry_trend(
            selected,
            batches=batches,
            interval=options.interval,
            include_empty_buckets=options.include_empty_trend_buckets,
            title="Imported-time Score Trend",
            generated_at=generated_at,
        )
        trend_filters = cast(Mapping[str, str], _freeze_mapping({**filters, **dict(trend.metadata.active_filters)}))
        coverage = list(trend.excluded_data_notes)
        coverage.extend(trend.coverage_warnings)
        chart = _trend_points(
            trend,
            chart_id="imported_time_score_trend",
            title="Imported-time score trend",
            subtitle="Mean score by imported_at UTC bucket from TrendService; no interpolation is performed.",
            source_family="ScoreboardEntry",
            filters=trend_filters,
        )
        _append_chart(
            charts,
            omitted,
            chart,
            title=chart.title,
            meaningful=any(point.available for point in chart.series[0].points),
            reason="No valid timestamped score values are available for a trend.",
        )
    else:
        coverage = []

    batch_chart = _numeric_points(
        groups_batch,
        chart_id="import_batch_summary",
        title="Import-batch summary",
        subtitle="Mean historical score and contributing-entry count by import batch.",
        source_family="ScoreboardEntry",
        filters=filters,
        metric_name="score",
        unit="score",
        secondary_label="scored_entry_count",
    )
    if groups_batch:
        charts.append(batch_chart)
    else:
        omitted.append(OmittedChart(batch_chart.chart_id, batch_chart.title, "No import-batch groups are available."))

    columns, rows = _scoreboard_table(selected) if options.include_detailed_tables else ((), ())
    metadata = DashboardMetadata(
        generated_at=generated_at,
        contributing_record_count=summary.total_eligible_entries,
        scored_record_count=summary.scored_entries,
        represented_models=_distinct_labels([aggregate.entry.model_name for aggregate in selected]),
        represented_import_batches=_distinct_labels([_entry_batch(item) for item in selected]),
        date_range=(summary.imported_at_min, summary.imported_at_max),
        active_filters=filters,
        coverage_warnings=tuple(dict.fromkeys(coverage)),
        mean_overall_score=summary.score.mean,
        median_overall_score=summary.score.median,
        mean_tokens_per_second=summary.tokens_per_second.mean,
    )
    comparison = _safe_comparison_summaries(comparison_reports) if options.include_comparison_summaries else ()
    return AnalyticsDashboard(
        source_record_family="ScoreboardEntry",
        title="Historical Scoreboard Analytics",
        metadata=metadata,
        charts=tuple(charts),
        omitted_charts=tuple(omitted),
        table_columns=columns,
        table_rows=rows,
        comparison_summaries=comparison,
    )


def build_html_analytics_report(
    service: BenchmarkService | None = None,
    catalog: CatalogService | None = None,
    *,
    options: HtmlAnalyticsReportOptions = HtmlAnalyticsReportOptions(),
    runs: Sequence[BenchmarkRun | BenchmarkRunAggregate] | None = None,
    entries: Sequence[ScoreboardEntry | ScoreboardEntryAggregate] | None = None,
    batches: Sequence[ScoreboardImportBatch] | None = None,
    statistics: StatisticsService | None = None,
    statistics_service: StatisticsService | None = None,
    trends: TrendService | None = None,
    trend_service: TrendService | None = None,
    comparison_reports: Sequence[Any] = (),
    generated_at: str | None = None,
) -> HtmlAnalyticsReport:
    """Prepare an immutable standalone analytics report from eligible records.

    ``runs`` and ``entries`` may be supplied as detached aggregates for
    deterministic API use.  When omitted, the normal repository-backed
    services select records using their existing soft-delete and snapshot
    rules.
    """

    active_generated_at = generated_at or now()
    if service is None:
        if statistics is not None:
            service = statistics.service
        elif statistics_service is not None:
            service = statistics_service.service
        elif trends is not None:
            service = trends.service
        elif trend_service is not None:
            service = trend_service.service
    if service is None:
        raise ValueError("A BenchmarkService or an existing statistics/trend service is required")
    active_catalog = catalog or service.catalog
    active_statistics = statistics or statistics_service or StatisticsService(service, active_catalog)
    active_trends = trends or trend_service or TrendService(service, active_catalog, active_statistics)
    dashboards: list[AnalyticsDashboard] = []

    if options.include_benchmark_run_dashboard:
        selected_runs = active_statistics.select_benchmark_runs(runs, filters=options.benchmark_filters)
        dashboards.append(
            _benchmark_dashboard(
                selected_runs,
                statistics=active_statistics,
                trends=active_trends,
                service=service,
                catalog=active_catalog,
                options=options,
                generated_at=active_generated_at,
                comparison_reports=comparison_reports,
            )
        )

    if options.include_scoreboard_dashboard:
        active_batches: Sequence[Any]
        if batches is None:
            active_batches = active_catalog.scoreboard_import_batches.list(include_deleted=True)
        else:
            active_batches = batches
        selected_entries = active_statistics.select_scoreboard_entries(
            entries,
            batches=active_batches,
            filters=options.scoreboard_filters,
        )
        dashboards.append(
            _scoreboard_dashboard(
                selected_entries,
                batches=active_batches,
                statistics=active_statistics,
                trends=active_trends,
                service=service,
                catalog=active_catalog,
                options=options,
                generated_at=active_generated_at,
                comparison_reports=comparison_reports,
            )
        )

    return HtmlAnalyticsReport(
        title=options.title,
        source_family=options.source_family,
        generated_at=active_generated_at,
        dashboards=tuple(dashboards),
        options=options,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def _chart_json(chart: ChartData) -> dict[str, Any]:
    metadata = chart.metadata
    return {
        "metadata": {
            "chart_id": metadata.chart_id,
            "title": metadata.title,
            "subtitle": metadata.subtitle,
            "chart_type": _json_value(metadata.chart_type),
            "x_axis_label": metadata.x_axis_label,
            "y_axis_label": metadata.y_axis_label,
            "value_format": _json_value(metadata.value_format),
            "missing_value_behavior": metadata.missing_value_behavior,
            "source_record_family": metadata.source_record_family,
            "active_filters": _json_value(metadata.active_filters),
        },
        "empty_message": chart.empty_message,
        "series": [
            {
                "series_id": series.series_id,
                "label": series.label,
                "category": series.category,
                "unit": series.unit,
                "unavailable": series.unavailable,
                "points": [
                    {
                        "key": point.key,
                        "label": point.label,
                        "value": point.value,
                        "secondary_value": point.secondary_value,
                        "tooltip_metadata": _json_value(point.tooltip_metadata),
                        "contributing_record_count": point.contributing_record_count,
                        "missing_value_count": point.missing_value_count,
                    }
                    for point in series.points
                ],
            }
            for series in chart.series
        ],
    }


def _report_json(report: HtmlAnalyticsReport) -> dict[str, Any]:
    return {
        "title": report.title,
        "source_family": _json_value(report.source_family),
        "generated_at": report.generated_at,
        "dashboards": [
            {
                "source_record_family": dashboard.source_record_family,
                "title": dashboard.title,
                "metadata": {
                    "generated_at": dashboard.metadata.generated_at,
                    "contributing_record_count": dashboard.metadata.contributing_record_count,
                    "scored_record_count": dashboard.metadata.scored_record_count,
                    "represented_models": list(dashboard.metadata.represented_models),
                    "represented_benchmarks": list(dashboard.metadata.represented_benchmarks),
                    "represented_sessions": list(dashboard.metadata.represented_sessions),
                    "represented_hardware_environments": list(dashboard.metadata.represented_hardware_environments),
                    "represented_import_batches": list(dashboard.metadata.represented_import_batches),
                    "date_range": list(dashboard.metadata.date_range),
                    "active_filters": _json_value(dashboard.metadata.active_filters),
                    "coverage_warnings": list(dashboard.metadata.coverage_warnings),
                    "mean_overall_score": dashboard.metadata.mean_overall_score,
                    "median_overall_score": dashboard.metadata.median_overall_score,
                    "mean_tokens_per_second": dashboard.metadata.mean_tokens_per_second,
                },
                "charts": [_chart_json(chart) for chart in dashboard.charts],
                "omitted_charts": [
                    {"chart_id": item.chart_id, "title": item.title, "reason": item.reason}
                    for item in dashboard.omitted_charts
                ],
                "comparison_summaries": [_json_value(item) for item in dashboard.comparison_summaries],
                "table_columns": list(dashboard.table_columns),
                "table_rows": [_json_value(row.values) for row in dashboard.table_rows],
            }
            for dashboard in report.dashboards
        ],
    }


def _safe_json_script(report: HtmlAnalyticsReport) -> str:
    value = json.dumps(_report_json(report), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # Keep JSON inside a non-executing script element without permitting stored
    # text to terminate it or create executable markup.
    return (
        value.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _html_text(value: Any) -> str:
    if value is None or value == "":
        return "Unavailable"
    if isinstance(value, float):
        return _display_number(value)
    return escape(str(value), quote=True)


def _slug(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    return result or "item"


def _series_dom_id(series_id: str, namespace: str = "") -> str:
    return _slug(f"{namespace}-{series_id}" if namespace else series_id)


def _chart_svg(chart: ChartData, *, namespace: str = "") -> str:
    points = [(series, point) for series in chart.series for point in series.points]
    values = [_finite_number(point.value) for _, point in points]
    available = [value for value in values if value is not None]
    if not available:
        return f'<p class="empty-state">{escape(chart.empty_message)}</p>'
    maximum = max(available)
    minimum = min(available)
    if maximum == minimum:
        maximum = minimum + 1.0
    width = max(720, min(1500, 110 * max(1, len(points))))
    height = 250
    left = 48
    bottom = 42
    plot_width = max(1, width - left - 20)
    plot_height = height - bottom - 20
    elements = [
        f'<svg class="chart-svg" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(chart.title, quote=True)}">',
        f'<line x1="{left}" y1="{height - bottom}" x2="{width - 20}" y2="{height - bottom}" class="axis" />',
        f'<line x1="{left}" y1="20" x2="{left}" y2="{height - bottom}" class="axis" />',
    ]
    count = max(1, len(points))
    for index, (series, point) in enumerate(points):
        value = _finite_number(point.value)
        x = left + (index + 0.5) * (plot_width / count)
        series_id = _series_dom_id(series.series_id, namespace)
        elements.append(f'<g data-series="{escape(series_id, quote=True)}">')
        if value is None:
            elements.append(f'<text x="{x:.2f}" y="{height - bottom - 8}" class="unavailable-mark">-</text>')
            elements.append("</g>")
            continue
        y = 20 + (maximum - value) / (maximum - minimum) * plot_height
        if chart.metadata.type == ChartType.LINE.value:
            radius = 5
            elements.append(
                f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius}" class="data-point">'
                f'<title>{escape(point.label)}: {_html_text(point.value)}</title></circle>'
            )
        else:
            bar_width = max(12, (plot_width / count) * 0.58)
            bar_y = y
            bar_height = max(1.0, height - bottom - bar_y)
            elements.append(
                f'<rect x="{x - bar_width / 2:.2f}" y="{bar_y:.2f}" width="{bar_width:.2f}" '
                f'height="{bar_height:.2f}" class="data-bar"><title>{escape(point.label)}: '
                f'{_html_text(point.value)}</title></rect>'
            )
        elements.append(
            f'<text x="{x:.2f}" y="{height - 14}" class="axis-label" transform="rotate(-28 {x:.2f} {height - 14})">'
            f'{escape(point.label[:28])}</text>'
        )
        elements.append("</g>")
    elements.append("</svg>")
    return "".join(elements)


def _chart_table(chart: ChartData, *, namespace: str = "") -> str:
    rows: list[str] = []
    for series in chart.series:
        series_id = _series_dom_id(series.series_id, namespace)
        for point in series.points:
            rows.append(
                f'<tr data-series="{escape(series_id, quote=True)}">'
                f"<th scope=\"row\">{escape(series.label)}: {escape(point.label)}</th>"
                f"<td>{_html_text(point.value)}</td>"
                f"<td>{_html_text(point.secondary_value)}</td>"
                f"<td>{_html_text(point.contributing_record_count)}</td>"
                f"<td>{_html_text(point.missing_value_count)}</td>"
                "</tr>"
            )
    if not rows:
        rows.append(f'<tr><td colspan="5" class="empty-state">{escape(chart.empty_message)}</td></tr>')
    return (
        '<table class="chart-table"><caption>Accessible data for '
        f"{escape(chart.title)}</caption><thead><tr><th scope=\"col\">Series and category</th>"
        '<th scope="col">Value</th><th scope="col">Secondary value</th>'
        '<th scope="col">Contributing records</th><th scope="col">Missing values</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def _render_chart(chart: ChartData, *, namespace: str = "") -> str:
    chart_id = _slug(f"{namespace}-{chart.chart_id}" if namespace else chart.chart_id)
    controls = []
    for series in chart.series:
        series_id = _series_dom_id(series.series_id, namespace)
        controls.append(
            f'<label class="series-control"><input type="checkbox" checked '
            f'data-series-control="{escape(series_id, quote=True)}"> {escape(series.label)}</label>'
        )
    return (
        f'<details class="chart-section" id="chart-{escape(chart_id, quote=True)}" open>'
        f'<summary><span class="section-title">{escape(chart.title)}</span></summary>'
        f'<p class="methodology">{escape(chart.metadata.subtitle)}</p>'
        f'<p class="axis-note">X axis: {escape(chart.metadata.x_axis_label)} · Y axis: {escape(chart.metadata.y_axis_label)}. '
        f'{escape(chart.metadata.missing_value_behavior)}</p>'
        f'<div class="series-controls" aria-label="Show or hide chart series">{"".join(controls)}</div>'
        f'<div class="chart-visual">{_chart_svg(chart, namespace=namespace)}</div>'
        f'<div>{_chart_table(chart, namespace=namespace)}</div>'
        "</details>"
    )


def _render_dashboard(dashboard: AnalyticsDashboard, *, compact: bool, namespace: str = "") -> str:
    metadata = dashboard.metadata
    cards = (
        ("Eligible records", metadata.contributing_record_count),
        ("Scored records", f"{metadata.scored_record_count} / {metadata.contributing_record_count}"),
        ("Unique models", len(metadata.represented_models)),
        ("Unique benchmarks", len(metadata.represented_benchmarks)),
        ("Unique sessions", len(metadata.represented_sessions)),
        ("Hardware environments", len(metadata.represented_hardware_environments)),
        ("Import batches", len(metadata.represented_import_batches)),
        ("Mean score", metadata.mean_overall_score),
        ("Median score", metadata.median_overall_score),
        ("Mean tokens/s", metadata.mean_tokens_per_second),
    )
    card_html = "".join(
        f'<div class="card"><dt>{escape(label)}</dt><dd>{_html_text(value)}</dd></div>'
        for label, value in cards
        if not (label in {"Unique benchmarks", "Unique sessions", "Hardware environments"} and dashboard.source_record_family == "ScoreboardEntry")
    )
    represented = (
        f'<p><strong>Models:</strong> {escape(", ".join(metadata.represented_models) or "None represented")}</p>'
        f'<p><strong>Benchmarks:</strong> {escape(", ".join(metadata.represented_benchmarks) or "None represented")}</p>'
        f'<p><strong>Sessions:</strong> {escape(", ".join(metadata.represented_sessions) or "None represented")}</p>'
        f'<p><strong>Hardware:</strong> {escape(", ".join(metadata.represented_hardware_environments) or "None represented")}</p>'
        f'<p><strong>Import batches:</strong> {escape(", ".join(metadata.represented_import_batches) or "None represented")}</p>'
    )
    warnings = "".join(f"<li>{escape(warning)}</li>" for warning in metadata.coverage_warnings)
    omitted = "".join(
        f'<li><strong>{escape(item.title)}</strong>: {escape(item.reason)}</li>'
        for item in dashboard.omitted_charts
    )
    chart_html = "".join(_render_chart(chart, namespace=namespace) for chart in dashboard.charts)
    comparison_html = ""
    if dashboard.comparison_summaries:
        comparison_rows = "".join(
            "<tr>" + "".join(f"<td>{_html_text(value)}</td>" for value in summary.values()) + "</tr>"
            for summary in dashboard.comparison_summaries
        )
        comparison_html = (
            '<details class="comparison-section" open><summary><span class="section-title">Comparison summaries</span></summary>'
            '<table><caption>Engine-prepared comparison metadata</caption><tbody>'
            f"{comparison_rows}</tbody></table></details>"
        )
    table_html = ""
    if dashboard.table_columns:
        headers = "".join(f"<th scope=\"col\"><button type=\"button\" data-table-sort=\"{escape(column, quote=True)}\">{escape(column)}</button></th>" for column in dashboard.table_columns)
        rows = "".join(
            f'<tr data-table-row-search="{escape(" ".join(_as_text(row.values.get(column)) for column in dashboard.table_columns).casefold(), quote=True)}">'
            + "".join(f"<td>{_html_text(row.values.get(column))}</td>" for column in dashboard.table_columns)
            + "</tr>"
            for row in dashboard.table_rows
        )
        table_html = (
            '<details class="detail-table" open><summary><span class="section-title">Detailed records</span></summary>'
            '<label class="table-search">Search records <input type="search" data-table-search placeholder="Model, benchmark, batch, or note"></label>'
            f'<div class="table-scroll"><table><caption>Safe analytics fields only; prompts, raw model output, and attachment contents are excluded.</caption>'
            f'<thead><tr>{headers}</tr></thead><tbody>{rows or "<tr><td colspan=\"10\" class=\"empty-state\">No records are available.</td></tr>"}</tbody></table></div></details>'
        )
    compact_class = " compact" if compact else ""
    return (
        f'<section class="dashboard{compact_class}" id="dashboard-{_slug(dashboard.source_record_family)}">'
        f'<h2>{escape(dashboard.title)}</h2>'
        f'<p class="methodology">Generated at {escape(metadata.generated_at)}. '
        f'Date range: {escape(str(metadata.date_range[0] or "Unavailable"))} to {escape(str(metadata.date_range[1] or "Unavailable"))}.</p>'
        f'<dl class="cards">{card_html}</dl>'
        f'<div class="represented">{represented}</div>'
        f'<p><strong>Active filters:</strong> {escape("; ".join(f"{key}={value}" for key, value in metadata.active_filters.items()) or "None")}</p>'
        f'{f"<div class=\"coverage\"><h3>Coverage warnings</h3><ul>{warnings}</ul></div>" if warnings else ""}'
        f'{f"<div class=\"coverage\"><h3>Charts omitted</h3><ul>{omitted}</ul></div>" if omitted else ""}'
        f'<div class="charts">{chart_html or "<p class=\"empty-state\">No charts are available for this selection.</p>"}</div>'
        f'{comparison_html}{table_html}</section>'
    )


def render_html_analytics_report(report: HtmlAnalyticsReport) -> str:
    """Render one self-contained UTF-8 HTML document."""

    navigation = "".join(
        f'<li><a href="#dashboard-{escape(_slug(dashboard.source_record_family), quote=True)}">{escape(dashboard.title)}</a></li>'
        for dashboard in report.dashboards
    )
    body = "".join(
        _render_dashboard(
            dashboard,
            compact=report.options.compact_layout,
            namespace=dashboard.source_record_family,
        )
        for dashboard in report.dashboards
    )
    data = _safe_json_script(report)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'">
<title>{escape(report.title)}</title>
<style>
:root {{ color-scheme: light dark; --bg: #111827; --surface: #1f2937; --surface-2: #243044; --text: #e5e7eb; --muted: #a7b0bf; --border: #465268; --accent: #60a5fa; --accent-strong: #2563eb; }}
* {{ box-sizing: border-box; }} body {{ margin: 0; padding: 2rem; background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, sans-serif; }}
main {{ max-width: 1600px; margin: auto; }} h1, h2, h3 {{ line-height: 1.2; }} h1 {{ margin-bottom: .25rem; }} h2 {{ margin-top: 0; }} h3 {{ font-size: 1rem; }}
a {{ color: var(--accent); }} a:focus-visible, button:focus-visible, input:focus-visible, summary:focus-visible {{ outline: 3px solid #fbbf24; outline-offset: 3px; }}
.muted, .methodology, .axis-note, .empty-state {{ color: var(--muted); }} .methodology {{ max-width: 100ch; }}
.report-nav, .coverage, .series-controls, .table-search {{ background: var(--surface); border: 1px solid var(--border); border-radius: .5rem; padding: .8rem 1rem; }}
.report-nav ul {{ display: flex; flex-wrap: wrap; gap: .75rem 1.5rem; margin: 0; padding-left: 1.2rem; }}
.dashboard {{ margin-top: 2rem; scroll-margin-top: 1rem; }} .dashboard + .dashboard {{ border-top: 2px solid var(--border); padding-top: 2rem; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap: .75rem; padding: 0; margin: 1.25rem 0; }}
.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: .5rem; padding: .85rem; }} .card dt {{ color: var(--muted); font-size: .82rem; }} .card dd {{ margin: .25rem 0 0; font-size: 1.2rem; font-weight: 700; overflow-wrap: anywhere; }}
.represented {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: .25rem 1rem; }} .represented p {{ margin: .25rem 0; overflow-wrap: anywhere; }}
.coverage {{ margin: 1rem 0; }} .coverage h3 {{ margin: 0 0 .35rem; }} .coverage ul {{ margin: 0; }}
.chart-section, .comparison-section, .detail-table {{ margin-top: 1rem; background: var(--surface); border: 1px solid var(--border); border-radius: .5rem; overflow: hidden; }}
.chart-section > summary, .comparison-section > summary, .detail-table > summary {{ cursor: pointer; padding: .85rem 1rem; background: var(--surface-2); }} .section-title {{ font-size: 1.08rem; font-weight: 700; }}
.chart-section > :not(summary), .comparison-section > :not(summary), .detail-table > :not(summary) {{ margin-left: 1rem; margin-right: 1rem; }}
.series-controls {{ display: flex; flex-wrap: wrap; gap: .75rem; margin-bottom: .75rem; }} .series-control {{ cursor: pointer; }}
.chart-visual {{ overflow-x: auto; padding: .5rem 0; }} .chart-svg {{ display: block; min-width: 720px; width: 100%; height: 250px; }}
.axis {{ stroke: var(--muted); stroke-width: 1; }} .data-bar {{ fill: var(--accent-strong); }} .data-point {{ fill: var(--accent-strong); stroke: var(--text); stroke-width: 2; }} .axis-label, .unavailable-mark {{ fill: var(--muted); font-size: 11px; text-anchor: middle; }}
table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; background: var(--surface); }} th, td {{ border: 1px solid var(--border); padding: .55rem .65rem; text-align: left; vertical-align: top; overflow-wrap: anywhere; }} th {{ background: var(--surface-2); }} th button {{ all: unset; cursor: pointer; color: inherit; font-weight: 700; }}
.table-scroll {{ overflow-x: auto; }} .table-search {{ display: flex; gap: .5rem; align-items: center; margin-top: 1rem; }} input {{ max-width: 30rem; background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: .25rem; padding: .5rem; }}
@media (prefers-color-scheme: light) {{ :root {{ --bg: #f8fafc; --surface: #ffffff; --surface-2: #e2e8f0; --text: #172033; --muted: #526174; --border: #cbd5e1; --accent: #1d4ed8; --accent-strong: #1d4ed8; }} }}
@media (max-width: 800px) {{ body {{ padding: 1rem; }} }}
</style>
</head>
<body><main>
<header><h1>{escape(report.title)}</h1><p class="methodology">Standalone offline analytics. Generated at {escape(report.generated_at)}. BenchmarkRun and ScoreboardEntry remain separate dashboards.</p></header>
<nav class="report-nav" aria-label="Dashboard navigation"><ul>{navigation}</ul></nav>
<noscript><p class="coverage">JavaScript is disabled. The charts' accessible tables and detailed data remain available; filtering and sorting controls are disabled.</p></noscript>
{body or '<p class="empty-state">No dashboard was selected.</p>'}
<script type="application/json" id="benchpup-analytics-data">{data}</script>
<script>
(function () {{
  document.querySelectorAll('[data-series-control]').forEach(function (control) {{
    control.addEventListener('change', function () {{
      var key = control.getAttribute('data-series-control');
      document.querySelectorAll('[data-series="' + key + '"]').forEach(function (item) {{ item.hidden = !control.checked; }});
    }});
  }});
  document.querySelectorAll('[data-table-search]').forEach(function (input) {{
    input.addEventListener('input', function () {{
      var query = input.value.trim().toLowerCase();
      var table = input.closest('.detail-table');
      if (!table) return;
      table.querySelectorAll('[data-table-row-search]').forEach(function (row) {{
        row.hidden = !!query && !String(row.getAttribute('data-table-row-search') || '').includes(query);
      }});
    }});
  }});
  document.querySelectorAll('[data-table-sort]').forEach(function (button) {{
    button.addEventListener('click', function () {{
      var table = button.closest('table');
      if (!table || !table.tBodies.length) return;
      var index = Array.prototype.indexOf.call(button.closest('tr').children, button.closest('th'));
      var rows = Array.prototype.slice.call(table.tBodies[0].querySelectorAll('tr'));
      rows.sort(function (left, right) {{
        var a = (left.children[index] || {{}}).textContent || '';
        var b = (right.children[index] || {{}}).textContent || '';
        var an = Number(a), bn = Number(b);
        if (a.trim() !== '' && b.trim() !== '' && Number.isFinite(an) && Number.isFinite(bn)) return an - bn;
        return a.localeCompare(b, undefined, {{ numeric: true, sensitivity: 'base' }});
      }});
      rows.forEach(function (row) {{ table.tBodies[0].appendChild(row); }});
    }});
  }});
}}());
</script>
</main></body></html>
"""


def write_html_analytics_report(
    report: HtmlAnalyticsReport | str,
    destination: str | Path | None = None,
    *,
    overwrite: bool = False,
) -> ReportWriteResult:
    """Stage and atomically finalize one UTF-8 HTML analytics file."""

    if isinstance(report, HtmlAnalyticsReport):
        content = render_html_analytics_report(report)
        path = Path(destination) if destination is not None else report.options.output_path
    else:
        content = report
        if destination is None:
            raise ValueError("HTML analytics destination is required")
        path = Path(destination)
    if path is None:
        raise ValueError("HTML analytics destination is required")
    if path.exists() and path.is_dir():
        return ReportWriteResult(ReportWriteStatus.TEMP_WRITE_FAILED, path, "HTML analytics destination is a directory")
    if path.exists() and not overwrite:
        return ReportWriteResult(
            ReportWriteStatus.OVERWRITE_REQUIRED,
            path,
            "Existing HTML analytics report requires explicit overwrite confirmation",
        )
    temporary: Path | None = None
    finalizing = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.parent.is_dir():
            return ReportWriteResult(ReportWriteStatus.TEMP_WRITE_FAILED, path, "HTML analytics parent is not a directory")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        finalizing = True
        os.replace(temporary, path)
        temporary = None
        return ReportWriteResult(ReportWriteStatus.SUCCESS, path, "HTML analytics report written successfully")
    except OSError as error:
        return ReportWriteResult(
            ReportWriteStatus.FINALIZE_FAILED if finalizing else ReportWriteStatus.TEMP_WRITE_FAILED,
            path,
            "Could not write HTML analytics report",
            details=f"{type(error).__name__}: {error}",
        )
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def export_html_analytics(
    service: BenchmarkService,
    destination: str | Path,
    *,
    catalog: CatalogService | None = None,
    options: HtmlAnalyticsReportOptions | None = None,
    overwrite: bool = False,
    **kwargs: Any,
) -> ReportWriteResult:
    """Build and stage an analytics dashboard through the engine boundary."""

    active_options = options or HtmlAnalyticsReportOptions()
    active_options = replace(active_options, output_destination=destination, overwrite=overwrite)
    report = build_html_analytics_report(service, catalog, options=active_options, **kwargs)
    return write_html_analytics_report(report, destination, overwrite=overwrite)


build_html_analytics = build_html_analytics_report
render_html_analytics = render_html_analytics_report
write_html_analytics = write_html_analytics_report
export_html_analytics_report = export_html_analytics


__all__ = (
    "AnalyticsDashboard",
    "AnalyticsReportOptions",
    "AnalyticsSourceFamily",
    "AnalyticsTableRow",
    "ChartData",
    "ChartDataset",
    "ChartMetadata",
    "ChartPoint",
    "ChartSeries",
    "ChartType",
    "DashboardMetadata",
    "HtmlAnalyticsReport",
    "HtmlAnalyticsReportOptions",
    "HtmlReportOptions",
    "OmittedChart",
    "build_html_analytics",
    "build_html_analytics_report",
    "export_html_analytics",
    "export_html_analytics_report",
    "render_html_analytics",
    "render_html_analytics_report",
    "write_html_analytics",
    "write_html_analytics_report",
)
