"""UI-independent descriptive statistics for BenchPup records.

This module deliberately keeps :class:`BenchmarkRun` and
:class:`ScoreboardEntry` as separate source families.  It consumes the
selection boundaries exposed by ``ReportingService`` but owns general-purpose
numeric, categorical, grouped, and time-bucket summaries independently from
report rendering.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Sequence, TypeAlias


TimestampValue: TypeAlias = str | datetime | date | None
DateBoundary: TypeAlias = str | datetime | date | None


def _freeze_mapping(value: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _first_value(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _category_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


@dataclass(frozen=True)
class NumericSummary:
    """Descriptive statistics over one numeric field.

    Quartiles use Tukey's median-of-halves method: for an odd population the
    median is excluded from both halves; for an even population the halves are
    equal.  Standard deviation is retained at full precision: population
    deviation divides by ``n`` and sample deviation divides by ``n - 1``.
    """

    total_count: int = 0
    available_count: int = 0
    missing_count: int = 0
    mean: float | None = None
    median: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    population_standard_deviation: float | None = None
    sample_standard_deviation: float | None = None
    lower_quartile: float | None = None
    upper_quartile: float | None = None
    interquartile_range: float | None = None

    @property
    def count(self) -> int:
        return self.total_count

    @property
    def available_value_count(self) -> int:
        return self.available_count

    @property
    def missing_value_count(self) -> int:
        return self.missing_count

    @property
    def average(self) -> float | None:
        return self.mean

    @property
    def standard_deviation(self) -> float | None:
        """Return the population standard deviation for display-oriented callers."""

        return self.population_standard_deviation

    @property
    def stddev(self) -> float | None:
        return self.population_standard_deviation

    @property
    def minimum_value(self) -> float | None:
        return self.minimum

    @property
    def maximum_value(self) -> float | None:
        return self.maximum

    @property
    def population_stddev(self) -> float | None:
        return self.population_standard_deviation

    @property
    def sample_stddev(self) -> float | None:
        return self.sample_standard_deviation

    @property
    def q1(self) -> float | None:
        return self.lower_quartile

    @property
    def q3(self) -> float | None:
        return self.upper_quartile

    @property
    def iqr(self) -> float | None:
        return self.interquartile_range


def numeric_summary(values: Iterable[Any], *, total_count: int | None = None) -> NumericSummary:
    """Return deterministic descriptive statistics without zero-filling."""

    normalized: tuple[float | None, ...] = tuple(_numeric_value(item) for item in values)
    observed_values: list[float] = [value for value in normalized if value is not None]
    observed: list[float] = sorted(observed_values)
    total = len(normalized) if total_count is None else total_count
    if total < 0:
        raise ValueError("total_count cannot be negative")
    if total < len(observed):
        raise ValueError("total_count cannot be less than available values")
    missing = total - len(observed)
    if not observed:
        return NumericSummary(total_count=total, missing_count=missing)

    mean = sum(observed) / len(observed)
    variance = sum((value - mean) ** 2 for value in observed) / len(observed)
    sample = None
    if len(observed) >= 2:
        sample = math.sqrt(sum((value - mean) ** 2 for value in observed) / (len(observed) - 1))
    middle = len(observed) // 2
    lower_half = observed[:middle]
    upper_half = observed[middle + 1:] if len(observed) % 2 else observed[middle:]
    lower_value = _median(lower_half) if lower_half else None
    upper_value = _median(upper_half) if upper_half else None
    lower = observed[0] if lower_value is None else lower_value
    upper = observed[-1] if upper_value is None else upper_value
    return NumericSummary(
        total_count=total,
        available_count=len(observed),
        missing_count=missing,
        mean=mean,
        median=_median(observed),
        minimum=observed[0],
        maximum=observed[-1],
        population_standard_deviation=math.sqrt(variance),
        sample_standard_deviation=sample,
        lower_quartile=lower,
        upper_quartile=upper,
        interquartile_range=upper - lower,
    )


@dataclass(frozen=True)
class CategoricalDistribution:
    """Counts and percentages for observed categories.

    Percentages are expressed from 0 to 100 and use only non-missing
    observations as their denominator.  Missing values are counted separately
    and are not inserted into ``counts``.
    """

    total_count: int = 0
    observed_count: int = 0
    missing_count: int = 0
    counts: Mapping[str, int] = field(default_factory=dict)
    percentages: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", _freeze_mapping(self.counts))
        object.__setattr__(self, "percentages", _freeze_mapping(self.percentages))

    @property
    def total_observed_values(self) -> int:
        return self.observed_count

    @property
    def total_observed(self) -> int:
        return self.observed_count

    @property
    def missing_value_count(self) -> int:
        return self.missing_count

    @property
    def category_counts(self) -> Mapping[str, int]:
        return self.counts

    @property
    def category_percentages(self) -> Mapping[str, float]:
        return self.percentages


def categorical_distribution(values: Iterable[Any], *, total_count: int | None = None) -> CategoricalDistribution:
    normalized = tuple(_category_value(item) for item in values)
    total = len(normalized) if total_count is None else total_count
    if total < 0:
        raise ValueError("total_count cannot be negative")
    observed = tuple(value for value in normalized if value is not None)
    if total < len(observed):
        raise ValueError("total_count cannot be less than observed values")
    counts = Counter(observed)
    observed_count = len(observed)
    denominator = float(observed_count) if observed_count else 1.0
    ordered_counts = dict(sorted(counts.items(), key=lambda item: (item[0].casefold(), item[0])))
    percentages = {
        key: (count / denominator) * 100.0
        for key, count in ordered_counts.items()
    }
    return CategoricalDistribution(
        total_count=total,
        observed_count=observed_count,
        missing_count=total - observed_count,
        counts=ordered_counts,
        percentages=percentages,
    )


@dataclass(frozen=True)
class BenchmarkStatisticsFilters:
    """Selection criteria for historical ``BenchmarkRun`` statistics."""

    model: str = ""
    benchmark: str = ""
    benchmark_type: str = ""
    session: str = ""
    session_id: int | None = None
    hardware: str = ""
    hardware_profile_id: int | None = None
    date_from: DateBoundary = None
    date_to: DateBoundary = None
    created_from: DateBoundary = None
    created_to: DateBoundary = None
    min_score: float | None = None
    max_score: float | None = None
    minimum_score: float | None = None
    maximum_score: float | None = None
    min_overall: float | None = None
    max_overall: float | None = None
    hallucination: str = ""
    hallucination_level: str = ""
    reliability: str = ""
    reliability_level: str = ""
    include_run_ids: frozenset[int] = frozenset()
    exclude_run_ids: frozenset[int] = frozenset()
    include_deleted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "include_run_ids", frozenset(self.include_run_ids))
        object.__setattr__(self, "exclude_run_ids", frozenset(self.exclude_run_ids))
        start = _first_value(self.date_from, self.created_from)
        end = _first_value(self.date_to, self.created_to)
        for name in ("date_from", "created_from"):
            object.__setattr__(self, name, start)
        for name in ("date_to", "created_to"):
            object.__setattr__(self, name, end)
        minimum = _first_value(self.min_score, self.minimum_score, self.min_overall)
        maximum = _first_value(self.max_score, self.maximum_score, self.max_overall)
        for name in ("min_score", "minimum_score", "min_overall"):
            object.__setattr__(self, name, minimum)
        for name in ("max_score", "maximum_score", "max_overall"):
            object.__setattr__(self, name, maximum)
        hallucination = _first_value(self.hallucination, self.hallucination_level) or ""
        reliability = _first_value(self.reliability, self.reliability_level) or ""
        object.__setattr__(self, "hallucination", hallucination)
        object.__setattr__(self, "hallucination_level", hallucination)
        object.__setattr__(self, "reliability", reliability)
        object.__setattr__(self, "reliability_level", reliability)


@dataclass(frozen=True)
class ScoreboardStatisticsFilters:
    """Selection criteria for historical ``ScoreboardEntry`` statistics."""

    model: str = ""
    batch_id: int | None = None
    date_from: DateBoundary = None
    date_to: DateBoundary = None
    imported_from: DateBoundary = None
    imported_to: DateBoundary = None
    min_score: float | None = None
    max_score: float | None = None
    minimum_score: float | None = None
    maximum_score: float | None = None
    hallucination: str = ""
    consistency: str = ""
    reliability: str = ""
    include_deleted: bool = False

    def __post_init__(self) -> None:
        start = _first_value(self.date_from, self.imported_from)
        end = _first_value(self.date_to, self.imported_to)
        for name in ("date_from", "imported_from"):
            object.__setattr__(self, name, start)
        for name in ("date_to", "imported_to"):
            object.__setattr__(self, name, end)
        minimum = _first_value(self.min_score, self.minimum_score)
        maximum = _first_value(self.max_score, self.maximum_score)
        for name in ("min_score", "minimum_score"):
            object.__setattr__(self, name, minimum)
        for name in ("max_score", "maximum_score"):
            object.__setattr__(self, name, maximum)


@dataclass(frozen=True)
class BenchmarkRunStatisticsSummary:
    total_eligible_runs: int = 0
    scored_runs: int = 0
    unscored_runs: int = 0
    unique_model_count: int = 0
    unique_benchmark_count: int = 0
    unique_session_count: int = 0
    unique_hardware_environment_count: int = 0
    overall_score: NumericSummary = field(default_factory=NumericSummary)
    tokens_per_second: NumericSummary = field(default_factory=NumericSummary)
    hallucination: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    reliability: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    benchmark_type: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    created_at_min: str | None = None
    created_at_max: str | None = None
    reviewed_runs: int = 0
    unreviewed_runs: int = 0
    known_hardware_environment_count: int = 0
    missing_hardware_count: int = 0

    @property
    def total_runs(self) -> int:
        return self.total_eligible_runs

    @property
    def score_summary(self) -> NumericSummary:
        return self.overall_score

    @property
    def overall_score_summary(self) -> NumericSummary:
        return self.overall_score

    @property
    def tokens_per_second_summary(self) -> NumericSummary:
        return self.tokens_per_second

    @property
    def hallucination_distribution(self) -> CategoricalDistribution:
        return self.hallucination

    @property
    def reliability_distribution(self) -> CategoricalDistribution:
        return self.reliability

    @property
    def benchmark_type_distribution(self) -> CategoricalDistribution:
        return self.benchmark_type

    @property
    def earliest_created_at(self) -> str | None:
        return self.created_at_min

    @property
    def latest_created_at(self) -> str | None:
        return self.created_at_max

    @property
    def review_count(self) -> int:
        return self.reviewed_runs

    @property
    def known_hardware_count(self) -> int:
        return self.known_hardware_environment_count


@dataclass(frozen=True)
class ScoreboardStatisticsSummary:
    total_eligible_entries: int = 0
    scored_entries: int = 0
    unscored_entries: int = 0
    unique_model_count: int = 0
    unique_import_batch_count: int = 0
    score: NumericSummary = field(default_factory=NumericSummary)
    tokens_per_second: NumericSummary = field(default_factory=NumericSummary)
    hallucination: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    consistency: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    reliability: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    imported_at_min: str | None = None
    imported_at_max: str | None = None

    @property
    def total_entries(self) -> int:
        return self.total_eligible_entries

    @property
    def score_summary(self) -> NumericSummary:
        return self.score

    @property
    def score_statistics(self) -> NumericSummary:
        return self.score

    @property
    def tokens_per_second_summary(self) -> NumericSummary:
        return self.tokens_per_second

    @property
    def hallucination_distribution(self) -> CategoricalDistribution:
        return self.hallucination

    @property
    def consistency_distribution(self) -> CategoricalDistribution:
        return self.consistency

    @property
    def reliability_distribution(self) -> CategoricalDistribution:
        return self.reliability

    @property
    def earliest_imported_at(self) -> str | None:
        return self.imported_at_min

    @property
    def latest_imported_at(self) -> str | None:
        return self.imported_at_max


@dataclass(frozen=True)
class ReviewStatisticsSummary:
    """Typed summaries for the fields stored on ``ReviewScore`` records."""

    total_reviews: int = 0
    accuracy_score: NumericSummary = field(default_factory=NumericSummary)
    depth_score: NumericSummary = field(default_factory=NumericSummary)
    signal_noise_score: NumericSummary = field(default_factory=NumericSummary)
    actionability_score: NumericSummary = field(default_factory=NumericSummary)
    seniority_score: NumericSummary = field(default_factory=NumericSummary)
    overall_score: NumericSummary = field(default_factory=NumericSummary)
    hallucination: CategoricalDistribution = field(default_factory=CategoricalDistribution)
    reliability: CategoricalDistribution = field(default_factory=CategoricalDistribution)

    @property
    def review_count(self) -> int:
        return self.total_reviews

    @property
    def accuracy(self) -> NumericSummary:
        return self.accuracy_score

    @property
    def depth(self) -> NumericSummary:
        return self.depth_score

    @property
    def signal_to_noise_score(self) -> NumericSummary:
        return self.signal_noise_score

    @property
    def signal_to_noise(self) -> NumericSummary:
        return self.signal_noise_score

    @property
    def actionability(self) -> NumericSummary:
        return self.actionability_score

    @property
    def seniority(self) -> NumericSummary:
        return self.seniority_score

    @property
    def score_summary(self) -> NumericSummary:
        return self.overall_score

    @property
    def hallucination_distribution(self) -> CategoricalDistribution:
        return self.hallucination

    @property
    def reliability_distribution(self) -> CategoricalDistribution:
        return self.reliability


@dataclass(frozen=True)
class StatisticsAvailability:
    """Engine-owned availability flags for optional analytical values."""

    benchmark_runs: bool = False
    scoreboard_entries: bool = False
    benchmark_reviews: bool = False
    benchmark_scores: bool = False
    benchmark_speed: bool = False
    benchmark_hardware: bool = False
    scoreboard_scores: bool = False
    scoreboard_speed: bool = False

    @property
    def has_benchmark_runs(self) -> bool:
        return self.benchmark_runs

    @property
    def has_scoreboard_entries(self) -> bool:
        return self.scoreboard_entries

    @property
    def has_reviews(self) -> bool:
        return self.benchmark_reviews

    @property
    def has_benchmark_score(self) -> bool:
        return self.benchmark_scores

    @property
    def has_benchmark_speed(self) -> bool:
        return self.benchmark_speed

    @property
    def has_hardware(self) -> bool:
        return self.benchmark_hardware


@dataclass(frozen=True)
class StatisticsOverview:
    """One immutable, source-separated statistics result for consumers."""

    benchmark_runs: BenchmarkRunStatisticsSummary = field(default_factory=BenchmarkRunStatisticsSummary)
    scoreboard_entries: ScoreboardStatisticsSummary = field(default_factory=ScoreboardStatisticsSummary)
    reviews: ReviewStatisticsSummary = field(default_factory=ReviewStatisticsSummary)
    availability: StatisticsAvailability = field(default_factory=StatisticsAvailability)

    @property
    def benchmark_summary(self) -> BenchmarkRunStatisticsSummary:
        return self.benchmark_runs

    @property
    def scoreboard_summary(self) -> ScoreboardStatisticsSummary:
        return self.scoreboard_entries

    @property
    def review_summary(self) -> ReviewStatisticsSummary:
        return self.reviews


@dataclass(frozen=True)
class BenchmarkRunStatisticsGroup:
    key: str
    label: str
    summary: BenchmarkRunStatisticsSummary

    @property
    def record_count(self) -> int:
        return self.summary.total_eligible_runs

    @property
    def scored_count(self) -> int:
        return self.summary.scored_runs

    @property
    def overall_score(self) -> NumericSummary:
        return self.summary.overall_score

    @property
    def tokens_per_second(self) -> NumericSummary:
        return self.summary.tokens_per_second

    @property
    def hallucination(self) -> CategoricalDistribution:
        return self.summary.hallucination

    @property
    def reliability(self) -> CategoricalDistribution:
        return self.summary.reliability


@dataclass(frozen=True)
class ScoreboardStatisticsGroup:
    key: str
    label: str
    summary: ScoreboardStatisticsSummary

    @property
    def record_count(self) -> int:
        return self.summary.total_eligible_entries

    @property
    def scored_count(self) -> int:
        return self.summary.scored_entries

    @property
    def score(self) -> NumericSummary:
        return self.summary.score

    @property
    def tokens_per_second(self) -> NumericSummary:
        return self.summary.tokens_per_second

    @property
    def hallucination(self) -> CategoricalDistribution:
        return self.summary.hallucination

    @property
    def consistency(self) -> CategoricalDistribution:
        return self.summary.consistency

    @property
    def reliability(self) -> CategoricalDistribution:
        return self.summary.reliability


class BenchmarkRunGroupBy(str, Enum):
    MODEL = "model"
    BENCHMARK = "benchmark"
    BENCHMARK_TYPE = "benchmark_type"
    SESSION = "session"
    HARDWARE = "hardware"
    HARDWARE_ENVIRONMENT = "hardware"


class ScoreboardGroupBy(str, Enum):
    MODEL = "model"
    IMPORT_BATCH = "import_batch"
    BATCH = "import_batch"


class TimeBucketGranularity(str, Enum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


@dataclass(frozen=True)
class TimeBucketValue:
    timestamp: TimestampValue
    score: float | None = None
    tokens_per_second: float | None = None


@dataclass(frozen=True)
class TimeBucketSummary:
    bucket_start: str
    label: str
    record_count: int
    scored_count: int
    score_summary: NumericSummary
    speed_summary: NumericSummary

    @property
    def count(self) -> int:
        return self.record_count

    @property
    def overall_score(self) -> NumericSummary:
        return self.score_summary

    @property
    def tokens_per_second(self) -> NumericSummary:
        return self.speed_summary


@dataclass(frozen=True)
class TimeBucketResult:
    granularity: str
    buckets: tuple[TimeBucketSummary, ...] = ()
    invalid_timestamp_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "buckets", tuple(self.buckets))

    @property
    def invalid_count(self) -> int:
        return self.invalid_timestamp_count

    @property
    def missing_timestamp_count(self) -> int:
        return self.invalid_timestamp_count

    def __iter__(self) -> Iterator[TimeBucketSummary]:
        return iter(self.buckets)

    def __len__(self) -> int:
        return len(self.buckets)

    def __getitem__(self, index: int) -> TimeBucketSummary:
        return self.buckets[index]


def _parse_utc_timestamp(value: TimestampValue) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _boundary(value: DateBoundary, *, end: bool = False) -> datetime | None:
    parsed = _parse_utc_timestamp(value)
    if parsed is None:
        return None
    if end and isinstance(value, (date,)) and not isinstance(value, datetime):
        return parsed + timedelta(days=1) - timedelta(microseconds=1)
    if end and isinstance(value, str) and len(value.strip()) <= 10 and "T" not in value:
        return parsed + timedelta(days=1) - timedelta(microseconds=1)
    return parsed


def _timestamp_in_range(value: TimestampValue, start: DateBoundary, end: DateBoundary) -> bool:
    if start is None and end is None:
        return True
    parsed = _parse_utc_timestamp(value)
    if parsed is None:
        return False
    lower = _boundary(start)
    upper = _boundary(end, end=True)
    return not (lower is not None and parsed < lower) and not (upper is not None and parsed > upper)


def _normalize_granularity(value: str | TimeBucketGranularity) -> TimeBucketGranularity:
    normalized = value.value if isinstance(value, TimeBucketGranularity) else str(value).strip().casefold()
    try:
        return TimeBucketGranularity(normalized)
    except ValueError as error:
        raise ValueError(f"Unsupported time-bucket granularity: {value}") from error


def time_bucket_for(timestamp: TimestampValue, granularity: str | TimeBucketGranularity) -> tuple[str, str] | None:
    """Return a UTC bucket start and label, or ``None`` for invalid input."""

    parsed = _parse_utc_timestamp(timestamp)
    if parsed is None:
        return None
    active = _normalize_granularity(granularity)
    start = datetime.combine(parsed.date(), time.min, tzinfo=timezone.utc)
    if active is TimeBucketGranularity.WEEK:
        start -= timedelta(days=start.weekday())
        iso = start.isocalendar()
        label = f"{iso.year}-W{iso.week:02d}"
    elif active is TimeBucketGranularity.MONTH:
        start = start.replace(day=1)
        label = start.strftime("%Y-%m")
    else:
        label = start.strftime("%Y-%m-%d")
    return start.isoformat(), label


def parse_utc_timestamp(timestamp: TimestampValue) -> datetime | None:
    """Return the shared UTC-normalized timestamp used by statistics helpers.

    Trend analysis uses this public wrapper for date-range metadata while
    ``time_bucket_for`` remains the single bucket-convention implementation.
    Invalid and missing values return ``None``.
    """

    return _parse_utc_timestamp(timestamp)


def build_time_buckets(
    values: Sequence[TimeBucketValue],
    *,
    granularity: str | TimeBucketGranularity = TimeBucketGranularity.DAY,
) -> TimeBucketResult:
    active = _normalize_granularity(granularity)
    grouped: dict[str, tuple[str, list[TimeBucketValue]]] = {}
    invalid = 0
    for value in values:
        bucket = time_bucket_for(value.timestamp, active)
        if bucket is None:
            invalid += 1
            continue
        start, label = bucket
        grouped.setdefault(start, (label, []))[1].append(value)
    summaries = []
    for start in sorted(grouped):
        label, records = grouped[start]
        scores = [record.score for record in records]
        speeds = [record.tokens_per_second for record in records]
        score_summary = numeric_summary(scores, total_count=len(records))
        summaries.append(
            TimeBucketSummary(
                bucket_start=start,
                label=label,
                record_count=len(records),
                scored_count=score_summary.available_count,
                score_summary=score_summary,
                speed_summary=numeric_summary(speeds, total_count=len(records)),
            )
        )
    return TimeBucketResult(active.value, tuple(summaries), invalid)


def _contains(value: Any, needle: str) -> bool:
    return needle.casefold() in str(value or "").casefold()


def _model_name(run: Any) -> str:
    return model_snapshot_name(run.model_snapshot)


def _benchmark_name(run: Any) -> str:
    return benchmark_snapshot_name(run.benchmark_snapshot)


def _benchmark_type(run: Any) -> str:
    return str(run.benchmark_snapshot.get("benchmark_type") or "").strip()


def _canonical_identity(value: Any) -> str | None:
    text = str(value or "").strip()
    return text.casefold() if text else None


def model_snapshot_name(snapshot: Mapping[str, Any]) -> str:
    """Return the historical model label from a run snapshot."""

    return str(snapshot.get("model_name") or snapshot.get("name") or "").strip()


def benchmark_snapshot_name(snapshot: Mapping[str, Any]) -> str:
    """Return the historical benchmark label using reporting's fallback order."""

    return str(
        snapshot.get("name")
        or snapshot.get("file_path")
        or snapshot.get("benchmark_file")
        or ""
    ).strip()


def normalize_model_identity(value: Any) -> str:
    """Normalize model identities using the existing case-insensitive policy."""

    return _canonical_identity(value) or "unknown"


def normalize_benchmark_identity(snapshot: Mapping[str, Any]) -> str:
    """Normalize a historical benchmark snapshot identity."""

    return _canonical_identity(benchmark_snapshot_name(snapshot)) or "unknown"


def _unique_count(values: Iterable[Any]) -> int:
    return len({identity for identity in (_canonical_identity(value) for value in values) if identity is not None})


def _snapshot_value_is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, Mapping):
        return any(_snapshot_value_is_present(item) for item in value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_snapshot_value_is_present(item) for item in value)
    return bool(str(value).strip())


def _hardware_snapshot_is_known(snapshot: Mapping[str, Any]) -> bool:
    return _snapshot_value_is_present(snapshot)


def _timestamp_bounds(values: Iterable[TimestampValue]) -> tuple[str | None, str | None]:
    parsed = sorted(
        timestamp
        for timestamp in (_parse_utc_timestamp(value) for value in values)
        if timestamp is not None
    )
    return (parsed[0].isoformat(), parsed[-1].isoformat()) if parsed else (None, None)


def _benchmark_hardware_key(snapshot: Mapping[str, Any]) -> str:
    from .reporting import normalize_hardware_snapshot

    return normalize_hardware_snapshot(snapshot)


def _benchmark_hardware_label(snapshot: Mapping[str, Any]) -> str:
    from .reporting import hardware_snapshot_label

    return hardware_snapshot_label(snapshot)


def _benchmark_summary(aggregates: Sequence[Any]) -> BenchmarkRunStatisticsSummary:
    from .reporting import BenchmarkRunAggregate

    records = tuple(aggregate for aggregate in aggregates if isinstance(aggregate, BenchmarkRunAggregate))
    overall = [aggregate.score.overall_score if aggregate.score else None for aggregate in records]
    speeds = [aggregate.run.model_snapshot.get("tokens_per_second") for aggregate in records]
    hallucination = [aggregate.score.hallucination_level if aggregate.score else None for aggregate in records]
    reliability = [aggregate.score.reliability_level if aggregate.score else None for aggregate in records]
    types = [_benchmark_type(aggregate.run) for aggregate in records]
    created = [aggregate.run.created_at for aggregate in records]
    hardware_snapshots = [aggregate.run.hardware_snapshot for aggregate in records]
    hardware_keys = [_benchmark_hardware_key(snapshot) for snapshot in hardware_snapshots]
    known_hardware_keys = [
        _benchmark_hardware_key(snapshot)
        for snapshot in hardware_snapshots
        if _hardware_snapshot_is_known(snapshot)
    ]
    session_keys = [
        aggregate.run.session_id
        if aggregate.run.session_id is not None
        else (aggregate.session.id if aggregate.session else None)
        for aggregate in records
    ]
    minimum, maximum = _timestamp_bounds(created)
    score_summary = numeric_summary(overall, total_count=len(records))
    reviewed_runs = sum(aggregate.score is not None for aggregate in records)
    return BenchmarkRunStatisticsSummary(
        total_eligible_runs=len(records),
        scored_runs=score_summary.available_count,
        unscored_runs=score_summary.missing_count,
        unique_model_count=_unique_count(_model_name(aggregate.run) for aggregate in records),
        unique_benchmark_count=_unique_count(_benchmark_name(aggregate.run) for aggregate in records),
        unique_session_count=_unique_count(session_keys),
        unique_hardware_environment_count=_unique_count(hardware_keys),
        overall_score=score_summary,
        tokens_per_second=numeric_summary(speeds, total_count=len(records)),
        hallucination=categorical_distribution(hallucination, total_count=len(records)),
        reliability=categorical_distribution(reliability, total_count=len(records)),
        benchmark_type=categorical_distribution(types, total_count=len(records)),
        created_at_min=minimum,
        created_at_max=maximum,
        reviewed_runs=reviewed_runs,
        unreviewed_runs=len(records) - reviewed_runs,
        known_hardware_environment_count=_unique_count(known_hardware_keys),
        missing_hardware_count=len(records) - len(known_hardware_keys),
    )


def _scoreboard_summary(aggregates: Sequence[Any]) -> ScoreboardStatisticsSummary:
    from .reporting import ScoreboardEntryAggregate

    records = tuple(aggregate for aggregate in aggregates if isinstance(aggregate, ScoreboardEntryAggregate))
    scores = [aggregate.entry.score for aggregate in records]
    speeds = [aggregate.entry.tokens_per_second for aggregate in records]
    hallucination = [aggregate.entry.hallucination_level for aggregate in records]
    consistency = [aggregate.entry.consistency for aggregate in records]
    reliability = [aggregate.entry.reliability_score for aggregate in records]
    imported = [aggregate.entry.imported_at for aggregate in records]
    score_summary = numeric_summary(scores, total_count=len(records))
    minimum, maximum = _timestamp_bounds(imported)
    return ScoreboardStatisticsSummary(
        total_eligible_entries=len(records),
        scored_entries=score_summary.available_count,
        unscored_entries=score_summary.missing_count,
        unique_model_count=_unique_count(aggregate.entry.model_name for aggregate in records),
        unique_import_batch_count=_unique_count(
            aggregate.entry.import_batch_id for aggregate in records if aggregate.entry.import_batch_id is not None
        ),
        score=score_summary,
        tokens_per_second=numeric_summary(speeds, total_count=len(records)),
        hallucination=categorical_distribution(hallucination, total_count=len(records)),
        consistency=categorical_distribution(consistency, total_count=len(records)),
        reliability=categorical_distribution(reliability, total_count=len(records)),
        imported_at_min=minimum,
        imported_at_max=maximum,
    )


def _review_summary(aggregates: Sequence[Any]) -> ReviewStatisticsSummary:
    from .reporting import BenchmarkRunAggregate

    reviews = tuple(
        aggregate.score
        for aggregate in aggregates
        if isinstance(aggregate, BenchmarkRunAggregate) and aggregate.score is not None
    )
    total = len(reviews)
    return ReviewStatisticsSummary(
        total_reviews=total,
        accuracy_score=numeric_summary((review.accuracy_score for review in reviews), total_count=total),
        depth_score=numeric_summary((review.depth_score for review in reviews), total_count=total),
        signal_noise_score=numeric_summary((review.signal_noise_score for review in reviews), total_count=total),
        actionability_score=numeric_summary((review.actionability_score for review in reviews), total_count=total),
        seniority_score=numeric_summary((review.seniority_score for review in reviews), total_count=total),
        overall_score=numeric_summary((review.overall_score for review in reviews), total_count=total),
        hallucination=categorical_distribution((review.hallucination_level for review in reviews), total_count=total),
        reliability=categorical_distribution((review.reliability_level for review in reviews), total_count=total),
    )


def _statistics_availability(
    benchmark: BenchmarkRunStatisticsSummary,
    scoreboard: ScoreboardStatisticsSummary,
    reviews: ReviewStatisticsSummary,
) -> StatisticsAvailability:
    return StatisticsAvailability(
        benchmark_runs=benchmark.total_eligible_runs > 0,
        scoreboard_entries=scoreboard.total_eligible_entries > 0,
        benchmark_reviews=reviews.total_reviews > 0,
        benchmark_scores=benchmark.overall_score.available_count > 0,
        benchmark_speed=benchmark.tokens_per_second.available_count > 0,
        benchmark_hardware=benchmark.known_hardware_environment_count > 0,
        scoreboard_scores=scoreboard.score.available_count > 0,
        scoreboard_speed=scoreboard.tokens_per_second.available_count > 0,
    )


def _normalize_benchmark_group(value: str | BenchmarkRunGroupBy) -> BenchmarkRunGroupBy:
    normalized = value.value if isinstance(value, BenchmarkRunGroupBy) else str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized == "hardware_environment":
        normalized = "hardware"
    try:
        return BenchmarkRunGroupBy(normalized)
    except ValueError as error:
        raise ValueError(f"Unsupported BenchmarkRun grouping: {value}") from error


def _normalize_scoreboard_group(value: str | ScoreboardGroupBy) -> ScoreboardGroupBy:
    normalized = value.value if isinstance(value, ScoreboardGroupBy) else str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized == "batch":
        normalized = "import_batch"
    try:
        return ScoreboardGroupBy(normalized)
    except ValueError as error:
        raise ValueError(f"Unsupported ScoreboardEntry grouping: {value}") from error


def _benchmark_group_identity(aggregate: Any, group_by: BenchmarkRunGroupBy) -> tuple[str, str]:
    run = aggregate.run
    if group_by is BenchmarkRunGroupBy.MODEL:
        label = _model_name(run)
        return (_canonical_identity(label) or "unknown", label or "Unknown model")
    if group_by is BenchmarkRunGroupBy.BENCHMARK:
        label = _benchmark_name(run)
        return (_canonical_identity(label) or "unknown", label or "Unknown benchmark")
    if group_by is BenchmarkRunGroupBy.BENCHMARK_TYPE:
        label = _benchmark_type(run)
        return (_canonical_identity(label) or "unknown", label or "Unknown benchmark type")
    if group_by is BenchmarkRunGroupBy.SESSION:
        session_id = run.session_id if run.session_id is not None else (aggregate.session.id if aggregate.session else None)
        if session_id is None:
            return "unknown", "Unknown session"
        label = aggregate.session.title if aggregate.session and aggregate.session.title else f"Session {session_id}"
        return f"session:{session_id}", label
    key = _benchmark_hardware_key(run.hardware_snapshot)
    return key, _benchmark_hardware_label(run.hardware_snapshot)


def _scoreboard_group_identity(aggregate: Any, group_by: ScoreboardGroupBy) -> tuple[str, str]:
    entry = aggregate.entry
    if group_by is ScoreboardGroupBy.MODEL:
        label = str(entry.model_name or "").strip()
        return (_canonical_identity(label) or "unknown", label or "Unknown model")
    batch_id = entry.import_batch_id
    if batch_id is None:
        return "unknown", "Unknown import batch"
    label = aggregate.batch.name if aggregate.batch and aggregate.batch.name else f"Import batch {batch_id}"
    return f"batch:{batch_id}", label


def _preferred_group_label(current: str, candidate: str) -> str:
    """Choose a stable label when equivalent keys arrive with different casing."""

    return min((current, candidate), key=lambda value: (value.casefold(), value))


class StatisticsService:
    """Application-facing facade for descriptive statistics only."""

    def __init__(self, service: Any, catalog: Any | None = None):
        from .reporting import ReportingService

        self.service = service
        self.catalog = catalog or service.catalog
        self.reporting = ReportingService(service, self.catalog)

    @staticmethod
    def _benchmark_base_filters(filters: BenchmarkStatisticsFilters) -> Any:
        from .reporting import BenchmarkReportFilters

        return BenchmarkReportFilters(
            benchmark_type=filters.benchmark_type,
            benchmark=filters.benchmark,
            session_id=filters.session_id,
            session=filters.session,
            hardware_profile_id=filters.hardware_profile_id,
            hardware=filters.hardware,
            model=filters.model,
            include_run_ids=filters.include_run_ids,
            exclude_run_ids=filters.exclude_run_ids,
            include_deleted=filters.include_deleted,
        )

    @staticmethod
    def _scoreboard_base_filters(filters: ScoreboardStatisticsFilters) -> Any:
        from .reporting import ScoreboardReportFilters

        return ScoreboardReportFilters(
            batch_id=filters.batch_id,
            model=filters.model,
            include_deleted=filters.include_deleted,
        )

    @staticmethod
    def _benchmark_extra_matches(aggregate: Any, filters: BenchmarkStatisticsFilters) -> bool:
        run = aggregate.run
        if not _timestamp_in_range(run.created_at, filters.date_from, filters.date_to):
            return False
        score = _numeric_value(aggregate.score.overall_score if aggregate.score else None)
        if filters.minimum_score is not None and (score is None or score < filters.minimum_score):
            return False
        if filters.maximum_score is not None and (score is None or score > filters.maximum_score):
            return False
        if filters.hallucination and not aggregate.score:
            return False
        if filters.hallucination and not _contains(aggregate.score.hallucination_level, filters.hallucination):
            return False
        if filters.reliability and not aggregate.score:
            return False
        if filters.reliability and not _contains(aggregate.score.reliability_level, filters.reliability):
            return False
        return True

    @staticmethod
    def _scoreboard_extra_matches(aggregate: Any, filters: ScoreboardStatisticsFilters) -> bool:
        entry = aggregate.entry
        if not _timestamp_in_range(entry.imported_at, filters.date_from, filters.date_to):
            return False
        score = _numeric_value(entry.score)
        if filters.minimum_score is not None and (score is None or score < filters.minimum_score):
            return False
        if filters.maximum_score is not None and (score is None or score > filters.maximum_score):
            return False
        return (
            (not filters.hallucination or _contains(entry.hallucination_level, filters.hallucination))
            and (not filters.consistency or _contains(entry.consistency, filters.consistency))
            and (not filters.reliability or _contains(entry.reliability_score, filters.reliability))
        )

    def select_benchmark_runs(
        self,
        runs: Sequence[Any] | None = None,
        *,
        filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
    ) -> tuple[Any, ...]:
        aggregates = self.reporting.select_benchmark_runs(
            runs,
            filters=self._benchmark_base_filters(filters),
        )
        return tuple(aggregate for aggregate in aggregates if self._benchmark_extra_matches(aggregate, filters))

    def select_scoreboard_entries(
        self,
        entries: Sequence[Any] | None = None,
        *,
        batches: Sequence[Any] | None = None,
        filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters(),
    ) -> tuple[Any, ...]:
        aggregates = self.reporting.select_scoreboard_entries(
            entries,
            batches=batches,
            filters=self._scoreboard_base_filters(filters),
        )
        return tuple(aggregate for aggregate in aggregates if self._scoreboard_extra_matches(aggregate, filters))

    def benchmark_run_statistics(
        self,
        runs: Sequence[Any] | None = None,
        *,
        filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
    ) -> BenchmarkRunStatisticsSummary:
        return _benchmark_summary(self.select_benchmark_runs(runs, filters=filters))

    def review_statistics(
        self,
        runs: Sequence[Any] | None = None,
        *,
        filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
    ) -> ReviewStatisticsSummary:
        """Return typed review summaries for the selected BenchmarkRun records.

        ``runs`` may already contain the aggregate records selected by another
        engine service.  In that case selection remains in-memory, while the
        review calculation continues to use the same authoritative helper as
        :meth:`statistics_overview`.
        """

        return _review_summary(self.select_benchmark_runs(runs, filters=filters))

    def review_summary(
        self,
        runs: Sequence[Any] | None = None,
        *,
        filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
    ) -> ReviewStatisticsSummary:
        """Compatibility-friendly alias for :meth:`review_statistics`."""

        return self.review_statistics(runs, filters=filters)

    def summarize_reviews(
        self,
        runs: Sequence[Any] | None = None,
        *,
        filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
    ) -> ReviewStatisticsSummary:
        """Return the review summary using the descriptive-statistics API."""

        return self.review_statistics(runs, filters=filters)

    def scoreboard_statistics(
        self,
        entries: Sequence[Any] | None = None,
        *,
        batches: Sequence[Any] | None = None,
        filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters(),
    ) -> ScoreboardStatisticsSummary:
        return _scoreboard_summary(self.select_scoreboard_entries(entries, batches=batches, filters=filters))

    def statistics_overview(
        self,
        runs: Sequence[Any] | None = None,
        entries: Sequence[Any] | None = None,
        *,
        batches: Sequence[Any] | None = None,
        benchmark_filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
        scoreboard_filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters(),
    ) -> StatisticsOverview:
        """Return one read-only result covering both source families.

        BenchmarkRun and ScoreboardEntry records are selected independently so
        neither source can contribute values to the other's summaries.  Each
        family is selected once and all derived values are calculated from the
        resulting typed aggregates.
        """

        selected_runs = self.select_benchmark_runs(runs, filters=benchmark_filters)
        selected_entries = self.select_scoreboard_entries(
            entries,
            batches=batches,
            filters=scoreboard_filters,
        )
        benchmark = _benchmark_summary(selected_runs)
        scoreboard = _scoreboard_summary(selected_entries)
        reviews = _review_summary(selected_runs)
        return StatisticsOverview(
            benchmark_runs=benchmark,
            scoreboard_entries=scoreboard,
            reviews=reviews,
            availability=_statistics_availability(benchmark, scoreboard, reviews),
        )

    def overview(
        self,
        runs: Sequence[Any] | None = None,
        entries: Sequence[Any] | None = None,
        *,
        batches: Sequence[Any] | None = None,
        benchmark_filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
        scoreboard_filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters(),
    ) -> StatisticsOverview:
        """Compatibility-friendly shorthand for :meth:`statistics_overview`."""

        return self.statistics_overview(
            runs,
            entries,
            batches=batches,
            benchmark_filters=benchmark_filters,
            scoreboard_filters=scoreboard_filters,
        )

    def summarize_benchmark_runs(self, runs: Sequence[Any] | None = None, *, filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters()) -> BenchmarkRunStatisticsSummary:
        return self.benchmark_run_statistics(runs, filters=filters)

    def summarize_scoreboard_entries(self, entries: Sequence[Any] | None = None, *, batches: Sequence[Any] | None = None, filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters()) -> ScoreboardStatisticsSummary:
        return self.scoreboard_statistics(entries, batches=batches, filters=filters)

    def benchmark_statistics(self, runs: Sequence[Any] | None = None, *, filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters()) -> BenchmarkRunStatisticsSummary:
        return self.benchmark_run_statistics(runs, filters=filters)

    def statistics_for_benchmark_runs(self, runs: Sequence[Any] | None = None, *, filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters()) -> BenchmarkRunStatisticsSummary:
        return self.benchmark_run_statistics(runs, filters=filters)

    def statistics_for_scoreboard_entries(self, entries: Sequence[Any] | None = None, *, batches: Sequence[Any] | None = None, filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters()) -> ScoreboardStatisticsSummary:
        return self.scoreboard_statistics(entries, batches=batches, filters=filters)

    def group_benchmark_runs(
        self,
        runs: Sequence[Any] | None = None,
        *,
        group_by: str | BenchmarkRunGroupBy = BenchmarkRunGroupBy.MODEL,
        filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
    ) -> tuple[BenchmarkRunStatisticsGroup, ...]:
        active = _normalize_benchmark_group(group_by)
        grouped: dict[str, tuple[str, list[Any]]] = {}
        for aggregate in self.select_benchmark_runs(runs, filters=filters):
            key, label = _benchmark_group_identity(aggregate, active)
            if key not in grouped:
                grouped[key] = (label, [])
            else:
                existing_label, records = grouped[key]
                grouped[key] = (_preferred_group_label(existing_label, label), records)
            grouped[key][1].append(aggregate)
        return tuple(
            BenchmarkRunStatisticsGroup(key=key, label=label, summary=_benchmark_summary(records))
            for key, (label, records) in sorted(
                grouped.items(), key=lambda item: (item[1][0].casefold(), item[1][0], item[0])
            )
        )

    def group_scoreboard_entries(
        self,
        entries: Sequence[Any] | None = None,
        *,
        batches: Sequence[Any] | None = None,
        group_by: str | ScoreboardGroupBy = ScoreboardGroupBy.MODEL,
        filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters(),
    ) -> tuple[ScoreboardStatisticsGroup, ...]:
        active = _normalize_scoreboard_group(group_by)
        grouped: dict[str, tuple[str, list[Any]]] = {}
        for aggregate in self.select_scoreboard_entries(entries, batches=batches, filters=filters):
            key, label = _scoreboard_group_identity(aggregate, active)
            if key not in grouped:
                grouped[key] = (label, [])
            else:
                existing_label, records = grouped[key]
                grouped[key] = (_preferred_group_label(existing_label, label), records)
            grouped[key][1].append(aggregate)
        return tuple(
            ScoreboardStatisticsGroup(key=key, label=label, summary=_scoreboard_summary(records))
            for key, (label, records) in sorted(
                grouped.items(), key=lambda item: (item[1][0].casefold(), item[1][0], item[0])
            )
        )

    def grouped_benchmark_statistics(self, runs: Sequence[Any] | None = None, *, group_by: str | BenchmarkRunGroupBy = BenchmarkRunGroupBy.MODEL, filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters()) -> tuple[BenchmarkRunStatisticsGroup, ...]:
        return self.group_benchmark_runs(runs, group_by=group_by, filters=filters)

    def grouped_scoreboard_statistics(self, entries: Sequence[Any] | None = None, *, batches: Sequence[Any] | None = None, group_by: str | ScoreboardGroupBy = ScoreboardGroupBy.MODEL, filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters()) -> tuple[ScoreboardStatisticsGroup, ...]:
        return self.group_scoreboard_entries(entries, batches=batches, group_by=group_by, filters=filters)

    def group_benchmark_statistics(self, runs: Sequence[Any] | None = None, *, group_by: str | BenchmarkRunGroupBy = BenchmarkRunGroupBy.MODEL, filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters()) -> tuple[BenchmarkRunStatisticsGroup, ...]:
        return self.group_benchmark_runs(runs, group_by=group_by, filters=filters)

    def group_scoreboard_statistics(self, entries: Sequence[Any] | None = None, *, batches: Sequence[Any] | None = None, group_by: str | ScoreboardGroupBy = ScoreboardGroupBy.MODEL, filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters()) -> tuple[ScoreboardStatisticsGroup, ...]:
        return self.group_scoreboard_entries(entries, batches=batches, group_by=group_by, filters=filters)

    def benchmark_run_time_buckets(
        self,
        runs: Sequence[Any] | None = None,
        *,
        granularity: str | TimeBucketGranularity = TimeBucketGranularity.DAY,
        filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
    ) -> TimeBucketResult:
        values = [
            TimeBucketValue(
                timestamp=aggregate.run.created_at,
                score=aggregate.score.overall_score if aggregate.score else None,
                tokens_per_second=aggregate.run.model_snapshot.get("tokens_per_second"),
            )
            for aggregate in self.select_benchmark_runs(runs, filters=filters)
        ]
        return build_time_buckets(values, granularity=granularity)

    def scoreboard_time_buckets(
        self,
        entries: Sequence[Any] | None = None,
        *,
        batches: Sequence[Any] | None = None,
        granularity: str | TimeBucketGranularity = TimeBucketGranularity.DAY,
        filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters(),
    ) -> TimeBucketResult:
        values = [
            TimeBucketValue(
                timestamp=aggregate.entry.imported_at,
                score=aggregate.entry.score,
                tokens_per_second=aggregate.entry.tokens_per_second,
            )
            for aggregate in self.select_scoreboard_entries(entries, batches=batches, filters=filters)
        ]
        return build_time_buckets(values, granularity=granularity)

    def time_buckets(
        self,
        values: Sequence[TimeBucketValue],
        *,
        granularity: str | TimeBucketGranularity = TimeBucketGranularity.DAY,
    ) -> TimeBucketResult:
        return build_time_buckets(values, granularity=granularity)


summarize_numeric = numeric_summary
summarize_categorical = categorical_distribution

BenchmarkStatisticsSummary = BenchmarkRunStatisticsSummary
BenchmarkRunStatistics = BenchmarkRunStatisticsSummary
BenchmarkRunStatisticsFilters = BenchmarkStatisticsFilters
ScoreboardStatistics = ScoreboardStatisticsSummary
ScoreboardEntryStatisticsFilters = ScoreboardStatisticsFilters


__all__ = (
    "BenchmarkRunGroupBy",
    "BenchmarkRunStatisticsGroup",
    "BenchmarkRunStatisticsSummary",
    "BenchmarkRunStatistics",
    "BenchmarkRunStatisticsFilters",
    "BenchmarkStatisticsFilters",
    "BenchmarkStatisticsSummary",
    "CategoricalDistribution",
    "NumericSummary",
    "ReviewStatisticsSummary",
    "ScoreboardGroupBy",
    "ScoreboardStatisticsFilters",
    "ScoreboardStatisticsGroup",
    "ScoreboardStatisticsSummary",
    "ScoreboardStatistics",
    "ScoreboardEntryStatisticsFilters",
    "StatisticsAvailability",
    "StatisticsOverview",
    "StatisticsService",
    "TimeBucketGranularity",
    "TimeBucketResult",
    "TimeBucketSummary",
    "TimeBucketValue",
    "build_time_buckets",
    "categorical_distribution",
    "numeric_summary",
    "benchmark_snapshot_name",
    "model_snapshot_name",
    "normalize_benchmark_identity",
    "normalize_model_identity",
    "summarize_categorical",
    "summarize_numeric",
    "parse_utc_timestamp",
    "time_bucket_for",
)
