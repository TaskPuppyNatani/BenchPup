"""UI-independent model and session comparison results.

ComparisonService deliberately consumes BenchmarkRun aggregates through
StatisticsService.  It aligns records and calculates interpretation-neutral
deltas, while descriptive calculations remain owned by statistics.py and
Markdown presentation remains owned by reporting.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .domain import BenchmarkSession, now
from .services import BenchmarkService, CatalogService
from .statistics import (
    BenchmarkRunGroupBy,
    BenchmarkRunStatisticsSummary,
    BenchmarkStatisticsFilters,
    CategoricalDistribution,
    NumericSummary,
    ReviewStatisticsSummary,
    ScoreboardStatisticsFilters,
    ScoreboardStatisticsSummary,
    StatisticsService,
    ScoreboardGroupBy,
    benchmark_snapshot_name,
    model_snapshot_name,
    normalize_benchmark_identity,
    normalize_model_identity,
)


def _freeze_mapping(value: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _canonical_label(current: str | None, candidate: str) -> str:
    if not current:
        return candidate
    return min((current, candidate), key=lambda item: (item.casefold(), item))


class ComparisonType(str, Enum):
    MODEL = "model"
    SESSION = "session"


class ComparisonDirection(str, Enum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    NEUTRAL = "neutral"


class ComparisonSourceFamily(str, Enum):
    """The authoritative historical record family behind a comparison."""

    BENCHMARK_RUN = "benchmark_run"
    SCOREBOARD = "scoreboard"


def _coerce_source_family(value: Any, context: str) -> ComparisonSourceFamily:
    try:
        return ComparisonSourceFamily(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{context} has an invalid comparison source family") from error


class ComparisonWarningCode(str, Enum):
    """Stable machine-readable reasons for incomplete comparison data."""

    MISSING_SUBJECT_IDENTITY = "missing_subject_identity"
    SUBJECT_HAS_NO_RECORDS = "subject_has_no_records"
    SUBJECT_HAS_NO_SCORE_VALUES = "subject_has_no_score_values"
    SUBJECT_HAS_NO_THROUGHPUT_VALUES = "subject_has_no_throughput_values"
    SUBJECT_HAS_NO_REVIEW_DATA = "subject_has_no_review_data"
    INSUFFICIENT_SUBJECTS = "insufficient_subjects"
    NO_SOURCE_DATA = "no_source_data"
    FILTERS_NO_RECORDS = "filters_no_records"
    SELECTED_SUBJECT_UNAVAILABLE = "selected_subject_unavailable"
    DUPLICATE_NORMALIZED_SUBJECT = "duplicate_normalized_subject"


class ComparisonResultState(str, Enum):
    """Typed outcome for comparison discovery and comparison requests."""

    READY = "ready"
    READY_WITH_MISSING_VALUES = "ready_with_missing_values"
    NO_SOURCE_DATA = "no_source_data"
    INSUFFICIENT_SUBJECTS = "insufficient_subjects"
    SELECTED_SUBJECTS_UNAVAILABLE = "selected_subjects_unavailable"
    FILTERS_NO_RECORDS = "filters_no_records"


@dataclass(frozen=True)
class ComparisonWarning:
    """A deterministic, typed warning suitable for future GUI consumers."""

    code: ComparisonWarningCode
    source_family: ComparisonSourceFamily
    subject_identity: str | None = None
    metric: str | None = None
    category: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_family",
            _coerce_source_family(self.source_family, "ComparisonWarning"),
        )

    @property
    def source(self) -> ComparisonSourceFamily:
        return self.source_family

    @property
    def subject(self) -> str | None:
        return self.subject_identity


@dataclass(frozen=True)
class ComparisonSubject:
    """One selectable historical subject discovered from one source family."""

    source_family: ComparisonSourceFamily
    identity: str
    label: str
    eligible_record_count: int = 0
    score_available: bool = False
    throughput_available: bool = False
    review_available: bool = False
    category_availability: Mapping[str, bool] = field(default_factory=dict)
    warnings: tuple[ComparisonWarning, ...] = ()

    def __post_init__(self) -> None:
        source_family = _coerce_source_family(self.source_family, "ComparisonSubject")
        object.__setattr__(self, "source_family", source_family)
        object.__setattr__(self, "category_availability", _freeze_mapping(self.category_availability))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        _validate_warning_sources(self.warnings, source_family, "ComparisonSubject")

    @property
    def source(self) -> ComparisonSourceFamily:
        return self.source_family

    @property
    def record_count(self) -> int:
        return self.eligible_record_count

    @property
    def has_score(self) -> bool:
        return self.score_available

    @property
    def has_throughput(self) -> bool:
        return self.throughput_available

    @property
    def has_review(self) -> bool:
        return self.review_available


@dataclass(frozen=True)
class ComparisonSubjectDiscovery:
    source_family: ComparisonSourceFamily
    subjects: tuple[ComparisonSubject, ...] = ()
    warnings: tuple[ComparisonWarning, ...] = ()
    state: ComparisonResultState = ComparisonResultState.READY
    excluded_record_count: int = 0

    def __post_init__(self) -> None:
        source_family = _coerce_source_family(self.source_family, "ComparisonSubjectDiscovery")
        object.__setattr__(self, "source_family", source_family)
        object.__setattr__(self, "subjects", tuple(self.subjects))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        _validate_subject_sources(self.subjects, source_family, "ComparisonSubjectDiscovery")
        _validate_warning_sources(self.warnings, source_family, "ComparisonSubjectDiscovery")

    @property
    def source(self) -> ComparisonSourceFamily:
        return self.source_family

    @property
    def available_subjects(self) -> tuple[ComparisonSubject, ...]:
        return self.subjects

    @property
    def models(self) -> tuple[ComparisonSubject, ...]:
        return self.subjects

    def __iter__(self):
        return iter(self.subjects)

    def __len__(self) -> int:
        return len(self.subjects)

    def __getitem__(self, index: int) -> ComparisonSubject:
        return self.subjects[index]


@dataclass(frozen=True)
class BenchmarkModelComparisonRequest:
    """Immutable request for a typed BenchmarkRun model comparison."""

    selected_models: tuple[str, ...] = ()
    filters: BenchmarkStatisticsFilters = field(default_factory=BenchmarkStatisticsFilters)
    title: str = "Model Comparison"
    generated_at: str | None = None

    def __post_init__(self) -> None:
        selected = tuple(str(value).strip() for value in self.selected_models)
        _validate_selected_subjects(selected, ComparisonSourceFamily.BENCHMARK_RUN)
        object.__setattr__(self, "selected_models", selected)
        object.__setattr__(self, "filters", replace(self.filters, model=""))

    @property
    def model_identities(self) -> tuple[str, ...]:
        return tuple(normalize_model_identity(value) for value in self.selected_models if value)


@dataclass(frozen=True)
class ScoreboardModelComparisonRequest:
    """Immutable request for a typed ScoreboardEntry model comparison."""

    selected_models: tuple[str, ...] = ()
    filters: ScoreboardStatisticsFilters = field(default_factory=ScoreboardStatisticsFilters)
    title: str = "Scoreboard Model Comparison"
    generated_at: str | None = None

    def __post_init__(self) -> None:
        selected = tuple(str(value).strip() for value in self.selected_models)
        _validate_selected_subjects(selected, ComparisonSourceFamily.SCOREBOARD)
        object.__setattr__(self, "selected_models", selected)
        object.__setattr__(self, "filters", replace(self.filters, model=""))

    @property
    def model_identities(self) -> tuple[str, ...]:
        return tuple(normalize_model_identity(value) for value in self.selected_models if value)


@dataclass(frozen=True)
class ComparisonMetadata:
    comparison_type: ComparisonType
    title: str
    generated_at: str
    contributing_record_count: int
    active_filters: Mapping[str, str] = field(default_factory=dict)
    selected_entities: tuple[str, ...] = ()
    source_family: ComparisonSourceFamily = ComparisonSourceFamily.BENCHMARK_RUN

    def __post_init__(self) -> None:
        object.__setattr__(self, "active_filters", _freeze_mapping(self.active_filters))
        object.__setattr__(self, "selected_entities", tuple(self.selected_entities))
        object.__setattr__(
            self,
            "source_family",
            _coerce_source_family(self.source_family, "ComparisonMetadata"),
        )

    @property
    def contributing_count(self) -> int:
        return self.contributing_record_count


@dataclass(frozen=True)
class MetricComparison:
    """One metric's values and, when pairwise, second-minus-first deltas."""

    metric_name: str
    values: Mapping[str, float | int | None] = field(default_factory=dict)
    absolute_delta: float | None = None
    percentage_delta: float | None = None
    direction: ComparisonDirection = ComparisonDirection.NEUTRAL
    baseline_entity: str | None = None
    comparison_entity: str | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", _freeze_mapping(self.values))

    @property
    def is_available(self) -> bool:
        return self.absolute_delta is not None

    @property
    def name(self) -> str:
        return self.metric_name

    @property
    def delta(self) -> float | None:
        return self.absolute_delta

    @property
    def percent_delta(self) -> float | None:
        return self.percentage_delta


@dataclass(frozen=True)
class CategoricalComparison:
    category: str
    counts: Mapping[str, int] = field(default_factory=dict)
    percentages: Mapping[str, float | None] = field(default_factory=dict)
    missing_counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", _freeze_mapping(self.counts))
        object.__setattr__(self, "percentages", _freeze_mapping(self.percentages))
        object.__setattr__(self, "missing_counts", _freeze_mapping(self.missing_counts))

    @property
    def name(self) -> str:
        return self.category


@dataclass(frozen=True)
class ComparisonSessionMetadata:
    id: int | None = None
    title: str = ""
    description: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    notes: str = ""
    is_deleted: bool = False

    @property
    def label(self) -> str:
        return self.title or (f"Session {self.id}" if self.id is not None else "Unknown session")


@dataclass(frozen=True)
class ComparisonEntitySummary:
    """Broad statistics for one selected model or session."""

    identity: str
    label: str
    summary: BenchmarkRunStatisticsSummary
    represented_benchmarks: tuple[str, ...] = ()
    represented_sessions: tuple[str, ...] = ()
    represented_models: tuple[str, ...] = ()
    represented_hardware: tuple[str, ...] = ()
    session: ComparisonSessionMetadata | None = None
    review: ReviewStatisticsSummary = field(default_factory=ReviewStatisticsSummary)
    warnings: tuple[ComparisonWarning, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.summary, BenchmarkRunStatisticsSummary):
            raise TypeError(
                "ComparisonEntitySummary.summary must be a BenchmarkRunStatisticsSummary"
            )
        if not isinstance(self.review, ReviewStatisticsSummary):
            raise TypeError(
                "ComparisonEntitySummary.review must be a ReviewStatisticsSummary"
            )
        object.__setattr__(self, "represented_benchmarks", tuple(self.represented_benchmarks))
        object.__setattr__(self, "represented_sessions", tuple(self.represented_sessions))
        object.__setattr__(self, "represented_models", tuple(self.represented_models))
        object.__setattr__(self, "represented_hardware", tuple(self.represented_hardware))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        _validate_warning_sources(
            self.warnings,
            ComparisonSourceFamily.BENCHMARK_RUN,
            "ComparisonEntitySummary",
        )

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

    @property
    def benchmark_type(self) -> CategoricalDistribution:
        return self.summary.benchmark_type

    @property
    def score_summary(self) -> NumericSummary:
        return self.overall_score

    @property
    def speed_summary(self) -> NumericSummary:
        return self.tokens_per_second

    @property
    def review_summary(self) -> ReviewStatisticsSummary:
        return self.review

    @property
    def score_available(self) -> bool:
        return self.overall_score.available_count > 0

    @property
    def throughput_available(self) -> bool:
        return self.tokens_per_second.available_count > 0

    @property
    def review_available(self) -> bool:
        return self.review.total_reviews > 0

    @property
    def category_availability(self) -> Mapping[str, bool]:
        return _freeze_mapping(
            {
                "hallucination": self.hallucination.observed_count > 0,
                "reliability": self.reliability.observed_count > 0,
                "benchmark_type": self.benchmark_type.observed_count > 0,
            }
        )


@dataclass(frozen=True)
class ScoreboardComparisonEntitySummary:
    """Descriptive statistics for one ScoreboardEntry model subject."""

    identity: str
    label: str
    summary: ScoreboardStatisticsSummary
    represented_import_batches: tuple[str, ...] = ()
    warnings: tuple[ComparisonWarning, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.summary, ScoreboardStatisticsSummary):
            raise TypeError(
                "ScoreboardComparisonEntitySummary.summary must be a ScoreboardStatisticsSummary"
            )
        object.__setattr__(self, "represented_import_batches", tuple(self.represented_import_batches))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        _validate_warning_sources(
            self.warnings,
            ComparisonSourceFamily.SCOREBOARD,
            "ScoreboardComparisonEntitySummary",
        )

    @property
    def source_family(self) -> ComparisonSourceFamily:
        return ComparisonSourceFamily.SCOREBOARD

    @property
    def record_count(self) -> int:
        return self.summary.total_eligible_entries

    @property
    def scored_count(self) -> int:
        return self.summary.scored_entries

    @property
    def unscored_count(self) -> int:
        return self.summary.unscored_entries

    @property
    def score(self) -> NumericSummary:
        return self.summary.score

    @property
    def score_summary(self) -> NumericSummary:
        return self.summary.score

    @property
    def tokens_per_second(self) -> NumericSummary:
        return self.summary.tokens_per_second

    @property
    def speed_summary(self) -> NumericSummary:
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

    @property
    def score_available(self) -> bool:
        return self.score.available_count > 0

    @property
    def throughput_available(self) -> bool:
        return self.tokens_per_second.available_count > 0

    @property
    def category_availability(self) -> Mapping[str, bool]:
        return _freeze_mapping(
            {
                "hallucination": self.hallucination.observed_count > 0,
                "consistency": self.consistency.observed_count > 0,
                "reliability": self.reliability.observed_count > 0,
            }
        )


@dataclass(frozen=True)
class AlignedEntityStatistics:
    record_count: int
    scored_count: int
    overall_score: NumericSummary
    tokens_per_second: NumericSummary


@dataclass(frozen=True)
class AlignedBenchmarkSummary:
    key: str
    label: str
    entities: Mapping[str, AlignedEntityStatistics] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "entities", _freeze_mapping(self.entities))

    @property
    def summaries(self) -> Mapping[str, AlignedEntityStatistics]:
        return self.entities


@dataclass(frozen=True)
class ModelComparisonAlignment:
    shared_benchmark_count: int = 0
    shared_benchmarks: tuple[str, ...] = ()
    aligned_benchmarks: tuple[AlignedBenchmarkSummary, ...] = ()
    non_overlapping_benchmarks: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    excluded_benchmark_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "shared_benchmarks", tuple(self.shared_benchmarks))
        object.__setattr__(self, "aligned_benchmarks", tuple(self.aligned_benchmarks))
        object.__setattr__(
            self,
            "non_overlapping_benchmarks",
            _freeze_mapping({key: tuple(value) for key, value in self.non_overlapping_benchmarks.items()}),
        )

    @property
    def excluded_benchmarks(self) -> Mapping[str, tuple[str, ...]]:
        return self.non_overlapping_benchmarks


@dataclass(frozen=True)
class AlignedModelBenchmarkSummary:
    model_key: str
    benchmark_key: str
    model_label: str
    benchmark_label: str
    entities: Mapping[str, AlignedEntityStatistics] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "entities", _freeze_mapping(self.entities))


@dataclass(frozen=True)
class SessionComparisonAlignment:
    shared_model_count: int = 0
    shared_models: tuple[str, ...] = ()
    shared_benchmark_count: int = 0
    shared_benchmarks: tuple[str, ...] = ()
    shared_model_benchmark_pair_count: int = 0
    shared_model_benchmark_pairs: tuple[tuple[str, str], ...] = ()
    aligned_models: tuple[AlignedBenchmarkSummary, ...] = ()
    aligned_benchmarks: tuple[AlignedBenchmarkSummary, ...] = ()
    aligned_model_benchmarks: tuple[AlignedModelBenchmarkSummary, ...] = ()
    non_overlapping_models: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    non_overlapping_benchmarks: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    non_overlapping_model_benchmark_pairs: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "shared_models", tuple(self.shared_models))
        object.__setattr__(self, "shared_benchmarks", tuple(self.shared_benchmarks))
        object.__setattr__(self, "shared_model_benchmark_pairs", tuple(self.shared_model_benchmark_pairs))
        object.__setattr__(self, "aligned_models", tuple(self.aligned_models))
        object.__setattr__(self, "aligned_benchmarks", tuple(self.aligned_benchmarks))
        object.__setattr__(self, "aligned_model_benchmarks", tuple(self.aligned_model_benchmarks))
        object.__setattr__(self, "non_overlapping_models", _freeze_mapping({key: tuple(value) for key, value in self.non_overlapping_models.items()}))
        object.__setattr__(self, "non_overlapping_benchmarks", _freeze_mapping({key: tuple(value) for key, value in self.non_overlapping_benchmarks.items()}))
        object.__setattr__(self, "non_overlapping_model_benchmark_pairs", _freeze_mapping({key: tuple(value) for key, value in self.non_overlapping_model_benchmark_pairs.items()}))

    @property
    def shared_pair_count(self) -> int:
        return self.shared_model_benchmark_pair_count


@dataclass(frozen=True)
class PairwiseComparison:
    baseline_entity: str
    comparison_entity: str
    metrics: tuple[MetricComparison, ...] = ()
    delta_direction: str = "second_minus_first"

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", tuple(self.metrics))

    @property
    def deltas(self) -> tuple[MetricComparison, ...]:
        return self.metrics

    def metric(self, name: str) -> MetricComparison | None:
        return next((item for item in self.metrics if item.metric_name == name), None)


@dataclass(frozen=True)
class ComparisonRankEntry:
    entity: str
    label: str
    rank: int | None
    mean_overall_score: float | None
    scored_count: int
    median_overall_score: float | None
    mean_tokens_per_second: float | None = None

    @property
    def mean_score(self) -> float | None:
        return self.mean_overall_score

    @property
    def median_score(self) -> float | None:
        return self.median_overall_score


@dataclass(frozen=True)
class ModelComparisonResult:
    metadata: ComparisonMetadata
    selected_models: tuple[str, ...]
    entities: tuple[ComparisonEntitySummary, ...]
    alignment: ModelComparisonAlignment
    ranking: tuple[ComparisonRankEntry, ...] = ()
    speed_ranking: tuple[ComparisonRankEntry, ...] = ()
    categorical_comparisons: tuple[CategoricalComparison, ...] = ()
    pairwise: PairwiseComparison | None = None
    state: ComparisonResultState = ComparisonResultState.READY
    warnings: tuple[ComparisonWarning, ...] = ()
    filters: BenchmarkStatisticsFilters = field(default_factory=BenchmarkStatisticsFilters)

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_models", tuple(self.selected_models))
        object.__setattr__(self, "entities", tuple(self.entities))
        object.__setattr__(self, "ranking", tuple(self.ranking))
        object.__setattr__(self, "speed_ranking", tuple(self.speed_ranking))
        object.__setattr__(self, "categorical_comparisons", tuple(self.categorical_comparisons))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        _validate_result_source(
            self.metadata,
            self.entities,
            self.warnings,
            expected_source=ComparisonSourceFamily.BENCHMARK_RUN,
            entity_type=ComparisonEntitySummary,
            context="ModelComparisonResult",
        )

    @property
    def model_summaries(self) -> tuple[ComparisonEntitySummary, ...]:
        return self.entities

    @property
    def shared_benchmark_count(self) -> int:
        return self.alignment.shared_benchmark_count

    @property
    def contributing_record_count(self) -> int:
        return self.metadata.contributing_record_count

    @property
    def categorical(self) -> tuple[CategoricalComparison, ...]:
        return self.categorical_comparisons

    @property
    def source_family(self) -> ComparisonSourceFamily:
        return self.metadata.source_family


@dataclass(frozen=True)
class SessionComparisonResult:
    metadata: ComparisonMetadata
    selected_sessions: tuple[ComparisonSessionMetadata, ...]
    entities: tuple[ComparisonEntitySummary, ...]
    alignment: SessionComparisonAlignment
    categorical_comparisons: tuple[CategoricalComparison, ...] = ()
    pairwise: PairwiseComparison | None = None
    state: ComparisonResultState = ComparisonResultState.READY
    warnings: tuple[ComparisonWarning, ...] = ()
    filters: BenchmarkStatisticsFilters = field(default_factory=BenchmarkStatisticsFilters)

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_sessions", tuple(self.selected_sessions))
        object.__setattr__(self, "entities", tuple(self.entities))
        object.__setattr__(self, "categorical_comparisons", tuple(self.categorical_comparisons))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        _validate_result_source(
            self.metadata,
            self.entities,
            self.warnings,
            expected_source=ComparisonSourceFamily.BENCHMARK_RUN,
            entity_type=ComparisonEntitySummary,
            context="SessionComparisonResult",
        )

    @property
    def session_summaries(self) -> tuple[ComparisonEntitySummary, ...]:
        return self.entities

    @property
    def shared_model_count(self) -> int:
        return self.alignment.shared_model_count

    @property
    def shared_benchmark_count(self) -> int:
        return self.alignment.shared_benchmark_count

    @property
    def shared_model_benchmark_pair_count(self) -> int:
        return self.alignment.shared_model_benchmark_pair_count

    @property
    def contributing_record_count(self) -> int:
        return self.metadata.contributing_record_count

    @property
    def categorical(self) -> tuple[CategoricalComparison, ...]:
        return self.categorical_comparisons

    @property
    def source_family(self) -> ComparisonSourceFamily:
        return self.metadata.source_family


@dataclass(frozen=True)
class ScoreboardModelComparisonResult:
    """Immutable, source-separated Scoreboard model comparison result."""

    metadata: ComparisonMetadata
    selected_models: tuple[str, ...] = ()
    entities: tuple[ScoreboardComparisonEntitySummary, ...] = ()
    categorical_comparisons: tuple[CategoricalComparison, ...] = ()
    pairwise: PairwiseComparison | None = None
    state: ComparisonResultState = ComparisonResultState.READY
    warnings: tuple[ComparisonWarning, ...] = ()
    filters: ScoreboardStatisticsFilters = field(default_factory=ScoreboardStatisticsFilters)

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_models", tuple(self.selected_models))
        object.__setattr__(self, "entities", tuple(self.entities))
        object.__setattr__(self, "categorical_comparisons", tuple(self.categorical_comparisons))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        _validate_result_source(
            self.metadata,
            self.entities,
            self.warnings,
            expected_source=ComparisonSourceFamily.SCOREBOARD,
            entity_type=ScoreboardComparisonEntitySummary,
            context="ScoreboardModelComparisonResult",
        )

    @property
    def source_family(self) -> ComparisonSourceFamily:
        return _coerce_source_family(self.metadata.source_family, "ScoreboardModelComparisonResult")

    @property
    def model_summaries(self) -> tuple[ScoreboardComparisonEntitySummary, ...]:
        return self.entities

    @property
    def contributing_record_count(self) -> int:
        return self.metadata.contributing_record_count

    @property
    def categorical(self) -> tuple[CategoricalComparison, ...]:
        return self.categorical_comparisons


def _timestamp_text(value: object) -> str:
    return str(value)


def _validate_selected_subjects(
    selected: Sequence[str],
    source_family: ComparisonSourceFamily,
) -> None:
    seen: set[str] = set()
    for value in selected:
        if not value:
            continue
        identity = normalize_model_identity(value)
        if identity == "unknown":
            continue
        if identity in seen:
            raise ValueError(
                f"Duplicate normalized {source_family.value} subject identity: {value}"
            )
        seen.add(identity)


def _validate_warning_sources(
    warnings: Sequence[Any],
    expected_source: ComparisonSourceFamily,
    context: str,
) -> None:
    for warning in warnings:
        if not isinstance(warning, ComparisonWarning):
            raise ValueError(f"{context} contains an invalid comparison warning")
        actual_source = _coerce_source_family(warning.source_family, f"{context} warning")
        if actual_source is not expected_source:
            raise ValueError(
                f"{context} warning must use {expected_source.value} source family"
            )


def _validate_subject_sources(
    subjects: Sequence[Any],
    expected_source: ComparisonSourceFamily,
    context: str,
) -> None:
    for subject in subjects:
        if not isinstance(subject, ComparisonSubject):
            raise ValueError(f"{context} contains an invalid comparison subject")
        actual_source = _coerce_source_family(subject.source_family, f"{context} subject")
        if actual_source is not expected_source:
            raise ValueError(
                f"{context} subject must use {expected_source.value} source family"
            )


def _validate_result_source(
    metadata: ComparisonMetadata,
    entities: Sequence[Any],
    warnings: Sequence[Any],
    *,
    expected_source: ComparisonSourceFamily,
    entity_type: type[Any],
    context: str,
) -> None:
    actual_source = _coerce_source_family(metadata.source_family, f"{context} metadata")
    if actual_source is not expected_source:
        raise ValueError(
            f"{context} metadata must use {expected_source.value} source family"
        )
    for entity in entities:
        if not isinstance(entity, entity_type):
            raise ValueError(f"{context} contains an invalid source-family entity")
    _validate_warning_sources(warnings, expected_source, context)


def _warning_sort_key(warning: ComparisonWarning) -> tuple[str, str, str, str, str, str]:
    return (
        warning.code.value,
        warning.source_family.value,
        warning.subject_identity or "",
        warning.metric or "",
        warning.category or "",
        warning.message,
    )


def _ordered_warnings(warnings: Sequence[ComparisonWarning]) -> tuple[ComparisonWarning, ...]:
    return tuple(sorted(set(warnings), key=_warning_sort_key))


def _warning(
    code: ComparisonWarningCode,
    source_family: ComparisonSourceFamily,
    message: str,
    *,
    subject_identity: str | None = None,
    metric: str | None = None,
    category: str | None = None,
) -> ComparisonWarning:
    return ComparisonWarning(
        code=code,
        source_family=source_family,
        subject_identity=subject_identity,
        metric=metric,
        category=category,
        message=message,
    )


def _filters_are_active(filters: BenchmarkStatisticsFilters | ScoreboardStatisticsFilters) -> bool:
    return filters != type(filters)()


def _filter_mapping(
    filters: BenchmarkStatisticsFilters | ScoreboardStatisticsFilters,
) -> Mapping[str, str]:
    if isinstance(filters, ScoreboardStatisticsFilters):
        values: dict[str, str] = {}
        fields: tuple[tuple[str, object], ...] = (
            ("batch_id", filters.batch_id),
            ("date_from", filters.date_from),
            ("date_to", filters.date_to),
            ("minimum_score", filters.minimum_score),
            ("maximum_score", filters.maximum_score),
            ("hallucination", filters.hallucination),
            ("consistency", filters.consistency),
            ("reliability", filters.reliability),
            ("include_deleted", filters.include_deleted),
        )
        for name, value in fields:
            if value not in (None, "", False) and not (
                isinstance(value, (tuple, frozenset, list, set)) and not value
            ):
                values[name] = _timestamp_text(value)
        return values

    values: dict[str, str] = {}
    fields: tuple[tuple[str, object], ...] = (
        ("benchmark", filters.benchmark),
        ("benchmark_type", filters.benchmark_type),
        ("session", filters.session),
        ("session_id", filters.session_id),
        ("hardware", filters.hardware),
        ("hardware_profile_id", filters.hardware_profile_id),
        ("date_from", filters.date_from),
        ("date_to", filters.date_to),
        ("minimum_score", filters.minimum_score),
        ("maximum_score", filters.maximum_score),
        ("hallucination", filters.hallucination),
        ("reliability", filters.reliability),
        ("include_run_ids", ",".join(str(value) for value in sorted(filters.include_run_ids))),
        ("exclude_run_ids", ",".join(str(value) for value in sorted(filters.exclude_run_ids))),
        ("include_deleted", filters.include_deleted),
    )
    for name, value in fields:
        if value not in (None, "", False) and not (
            isinstance(value, (tuple, frozenset, list, set)) and not value
        ):
            values[name] = _timestamp_text(value)
    return values


def _model_records(
    aggregates: Sequence[Any],
    key: str,
) -> tuple[Any, ...]:
    return tuple(
        aggregate
        for aggregate in aggregates
        if normalize_model_identity(model_snapshot_name(aggregate.run.model_snapshot)) == key
    )


def _session_id_for(aggregate: Any) -> int | None:
    if aggregate.run.session_id is not None:
        return aggregate.run.session_id
    return aggregate.session.id if aggregate.session is not None else None


def _session_key_for(aggregate: Any) -> str | None:
    session_id = _session_id_for(aggregate)
    if session_id is not None:
        return f"session:{session_id}"
    if aggregate.session is not None and aggregate.session.title.strip():
        return f"session-title:{normalize_model_identity(aggregate.session.title)}"
    return None


def _session_label_for(aggregate: Any) -> str:
    if aggregate.session is not None and aggregate.session.title.strip():
        return aggregate.session.title.strip()
    session_id = _session_id_for(aggregate)
    return f"Session {session_id}" if session_id is not None else "Unknown session"


def _benchmark_key_for(aggregate: Any) -> str:
    return normalize_benchmark_identity(aggregate.run.benchmark_snapshot)


def _benchmark_label_for(aggregate: Any) -> str:
    return benchmark_snapshot_name(aggregate.run.benchmark_snapshot) or "Unknown benchmark"


def _labels_for_groups(
    statistics: StatisticsService,
    records: Sequence[Any],
    group_by: BenchmarkRunGroupBy,
) -> tuple[str, ...]:
    groups = statistics.group_benchmark_runs(
        records,
        group_by=group_by,
        filters=BenchmarkStatisticsFilters(include_deleted=True),
    )
    return tuple(group.label for group in groups)


def _entity_summary(
    statistics: StatisticsService,
    identity: str,
    label: str,
    records: Sequence[Any],
    *,
    session: ComparisonSessionMetadata | None = None,
    warnings: Sequence[ComparisonWarning] = (),
) -> ComparisonEntitySummary:
    summary = statistics.benchmark_run_statistics(
        records,
        filters=BenchmarkStatisticsFilters(include_deleted=True),
    )
    review = statistics.review_statistics(
        records,
        filters=BenchmarkStatisticsFilters(include_deleted=True),
    )
    return ComparisonEntitySummary(
        identity=identity,
        label=label,
        summary=summary,
        represented_benchmarks=_labels_for_groups(statistics, records, BenchmarkRunGroupBy.BENCHMARK),
        represented_sessions=_labels_for_groups(statistics, records, BenchmarkRunGroupBy.SESSION),
        represented_models=_labels_for_groups(statistics, records, BenchmarkRunGroupBy.MODEL),
        represented_hardware=_labels_for_groups(statistics, records, BenchmarkRunGroupBy.HARDWARE),
        session=session,
        review=review,
        warnings=_ordered_warnings(warnings),
    )


def _scoreboard_batch_labels(records: Sequence[Any]) -> tuple[str, ...]:
    labels: set[str] = set()
    for aggregate in records:
        batch = getattr(aggregate, "batch", None)
        entry = getattr(aggregate, "entry", None)
        batch_id = getattr(entry, "import_batch_id", None)
        if batch is not None and getattr(batch, "name", ""):
            labels.add(str(batch.name).strip())
        elif batch_id is not None:
            labels.add(f"Import batch {batch_id}")
        else:
            labels.add("Unbatched scoreboard entries")
    return tuple(sorted((label for label in labels if label), key=lambda item: (item.casefold(), item)))


def _scoreboard_entity_summary(
    statistics: StatisticsService,
    identity: str,
    label: str,
    records: Sequence[Any],
    *,
    warnings: Sequence[ComparisonWarning] = (),
) -> ScoreboardComparisonEntitySummary:
    summary = statistics.scoreboard_statistics(
        records,
        filters=ScoreboardStatisticsFilters(include_deleted=True),
    )
    return ScoreboardComparisonEntitySummary(
        identity=identity,
        label=label,
        summary=summary,
        represented_import_batches=_scoreboard_batch_labels(records),
        warnings=_ordered_warnings(warnings),
    )


def _scoreboard_categorical_comparisons(
    entities: Sequence[ScoreboardComparisonEntitySummary],
) -> tuple[CategoricalComparison, ...]:
    rows: list[CategoricalComparison] = []
    distribution_fields = (
        ("hallucination", lambda entity: entity.hallucination),
        ("consistency", lambda entity: entity.consistency),
        ("reliability", lambda entity: entity.reliability),
    )
    for prefix, getter in distribution_fields:
        categories = sorted(
            {
                category
                for entity in entities
                for category in getter(entity).counts
            },
            key=lambda item: (item.casefold(), item),
        )
        for category in categories:
            counts = {entity.identity: getter(entity).counts.get(category, 0) for entity in entities}
            percentages = {
                entity.identity: getter(entity).percentages.get(category)
                for entity in entities
            }
            missing = {entity.identity: getter(entity).missing_count for entity in entities}
            rows.append(CategoricalComparison(f"{prefix}:{category}", counts, percentages, missing))
    return tuple(rows)


def _scoreboard_pairwise_metric(
    name: str,
    first: ScoreboardComparisonEntitySummary,
    second: ScoreboardComparisonEntitySummary,
    first_value: float | int | None,
    second_value: float | int | None,
    direction: ComparisonDirection,
) -> MetricComparison:
    values: Mapping[str, float | int | None] = {
        first.identity: first_value,
        second.identity: second_value,
    }
    if first_value is None or second_value is None:
        absolute = None
        percentage = None
        reason = "one or both values unavailable"
    else:
        absolute = float(second_value) - float(first_value)
        if float(first_value) == 0.0:
            percentage = None
            reason = "baseline is zero"
        else:
            percentage = absolute / float(first_value) * 100.0
            reason = None
    return MetricComparison(
        metric_name=name,
        values=values,
        absolute_delta=absolute,
        percentage_delta=percentage,
        direction=direction,
        baseline_entity=first.identity,
        comparison_entity=second.identity,
        unavailable_reason=reason,
    )


def _scoreboard_pairwise(
    first: ScoreboardComparisonEntitySummary,
    second: ScoreboardComparisonEntitySummary,
) -> PairwiseComparison:
    metrics = (
        _scoreboard_pairwise_metric(
            "mean_score",
            first,
            second,
            first.score.mean,
            second.score.mean,
            ComparisonDirection.HIGHER_IS_BETTER,
        ),
        _scoreboard_pairwise_metric(
            "median_score",
            first,
            second,
            first.score.median,
            second.score.median,
            ComparisonDirection.HIGHER_IS_BETTER,
        ),
        _scoreboard_pairwise_metric(
            "mean_tokens_per_second",
            first,
            second,
            first.tokens_per_second.mean,
            second.tokens_per_second.mean,
            ComparisonDirection.HIGHER_IS_BETTER,
        ),
        _scoreboard_pairwise_metric(
            "scored_entry_count",
            first,
            second,
            first.scored_count,
            second.scored_count,
            ComparisonDirection.NEUTRAL,
        ),
    )
    return PairwiseComparison(first.identity, second.identity, metrics)


def _aligned_statistics(statistics: StatisticsService, records: Sequence[Any]) -> AlignedEntityStatistics:
    summary = statistics.benchmark_run_statistics(
        records,
        filters=BenchmarkStatisticsFilters(include_deleted=True),
    )
    return AlignedEntityStatistics(
        record_count=summary.total_eligible_runs,
        scored_count=summary.scored_runs,
        overall_score=summary.overall_score,
        tokens_per_second=summary.tokens_per_second,
    )


def _benchmark_alignment(
    statistics: StatisticsService,
    entity_records: Mapping[str, Sequence[Any]],
) -> ModelComparisonAlignment:
    benchmark_records: dict[str, dict[str, list[Any]]] = {}
    keys_by_entity: dict[str, set[str]] = {}
    labels: dict[str, str] = {}
    for entity, records in entity_records.items():
        keys_by_entity[entity] = set()
        for aggregate in records:
            key = _benchmark_key_for(aggregate)
            keys_by_entity[entity].add(key)
            labels[key] = _canonical_label(labels.get(key), _benchmark_label_for(aggregate))
            benchmark_records.setdefault(key, {}).setdefault(entity, []).append(aggregate)
    shared = set.intersection(*(set(values) for values in keys_by_entity.values())) if keys_by_entity else set()
    aligned: list[AlignedBenchmarkSummary] = []
    for key in sorted(shared, key=lambda item: ((labels.get(item) or item).casefold(), labels.get(item) or item, item)):
        summaries = {
            entity: _aligned_statistics(statistics, benchmark_records[key].get(entity, ()))
            for entity in entity_records
        }
        aligned.append(AlignedBenchmarkSummary(key, labels.get(key, "Unknown benchmark"), summaries))
    non_overlapping: dict[str, tuple[str, ...]] = {}
    for entity, keys in keys_by_entity.items():
        non_overlapping[entity] = tuple(
            sorted(
                (labels.get(key) or "Unknown benchmark" for key in keys - shared),
                key=lambda item: (item.casefold(), item),
            )
        )
    excluded_count = len(set().union(*(keys - shared for keys in keys_by_entity.values()))) if keys_by_entity else 0
    return ModelComparisonAlignment(
        shared_benchmark_count=len(shared),
        shared_benchmarks=tuple(
            sorted((labels.get(key) or "Unknown benchmark" for key in shared), key=lambda item: (item.casefold(), item))
        ),
        aligned_benchmarks=tuple(aligned),
        non_overlapping_benchmarks=non_overlapping,
        excluded_benchmark_count=excluded_count,
    )


def _session_alignment(
    statistics: StatisticsService,
    entity_records: Mapping[str, Sequence[Any]],
) -> SessionComparisonAlignment:
    model_records: dict[str, dict[str, list[Any]]] = {}
    benchmark_records: dict[str, dict[str, list[Any]]] = {}
    pair_records: dict[tuple[str, str], dict[str, list[Any]]] = {}
    model_labels: dict[str, str] = {}
    benchmark_labels: dict[str, str] = {}
    models_by_entity: dict[str, set[str]] = {}
    benchmarks_by_entity: dict[str, set[str]] = {}
    pairs_by_entity: dict[str, set[tuple[str, str]]] = {}
    for entity, records in entity_records.items():
        models_by_entity[entity] = set()
        benchmarks_by_entity[entity] = set()
        pairs_by_entity[entity] = set()
        for aggregate in records:
            model_key = normalize_model_identity(model_snapshot_name(aggregate.run.model_snapshot))
            benchmark_key = _benchmark_key_for(aggregate)
            models_by_entity[entity].add(model_key)
            benchmarks_by_entity[entity].add(benchmark_key)
            pairs_by_entity[entity].add((model_key, benchmark_key))
            model_labels[model_key] = _canonical_label(model_labels.get(model_key), model_snapshot_name(aggregate.run.model_snapshot) or "Unknown model")
            benchmark_labels[benchmark_key] = _canonical_label(benchmark_labels.get(benchmark_key), _benchmark_label_for(aggregate))
            model_records.setdefault(model_key, {}).setdefault(entity, []).append(aggregate)
            benchmark_records.setdefault(benchmark_key, {}).setdefault(entity, []).append(aggregate)
            pair_records.setdefault((model_key, benchmark_key), {}).setdefault(entity, []).append(aggregate)
    shared_models = set.intersection(*(set(values) for values in models_by_entity.values())) if models_by_entity else set()
    shared_benchmarks = set.intersection(*(set(values) for values in benchmarks_by_entity.values())) if benchmarks_by_entity else set()
    shared_pairs = set.intersection(*(set(values) for values in pairs_by_entity.values())) if pairs_by_entity else set()

    def aligned_groups(
        records_by_key: Mapping[str, Mapping[str, Sequence[Any]]],
        shared: set[str],
        labels: Mapping[str, str],
    ) -> tuple[AlignedBenchmarkSummary, ...]:
        return tuple(
            AlignedBenchmarkSummary(
                key,
                labels.get(key, key),
                {
                    entity: _aligned_statistics(statistics, records_by_key[key].get(entity, ()))
                    for entity in entity_records
                },
            )
            for key in sorted(shared, key=lambda item: ((labels.get(item) or item).casefold(), labels.get(item) or item, item))
        )

    def non_overlapping(
        values_by_entity: Mapping[str, set[str]],
        shared: set[str],
        labels: Mapping[str, str],
    ) -> dict[str, tuple[str, ...]]:
        return {
            entity: tuple(sorted((labels.get(key) or key for key in values - shared), key=lambda item: (item.casefold(), item)))
            for entity, values in values_by_entity.items()
        }

    aligned_pairs: list[AlignedModelBenchmarkSummary] = []
    for model_key, benchmark_key in sorted(
        shared_pairs,
        key=lambda item: (
            (model_labels.get(item[0]) or item[0]).casefold(),
            (benchmark_labels.get(item[1]) or item[1]).casefold(),
            item,
        ),
    ):
        key = (model_key, benchmark_key)
        aligned_pairs.append(
            AlignedModelBenchmarkSummary(
                model_key,
                benchmark_key,
                model_labels.get(model_key, "Unknown model"),
                benchmark_labels.get(benchmark_key, "Unknown benchmark"),
                {
                    entity: _aligned_statistics(statistics, pair_records[key].get(entity, ()))
                    for entity in entity_records
                },
            )
        )

    pair_non_overlapping = {
        entity: tuple(
            sorted(
                (
                    (model_labels.get(pair[0], "Unknown model"), benchmark_labels.get(pair[1], "Unknown benchmark"))
                    for pair in pairs - shared_pairs
                ),
                key=lambda item: (item[0].casefold(), item[1].casefold(), item),
            )
        )
        for entity, pairs in pairs_by_entity.items()
    }
    return SessionComparisonAlignment(
        shared_model_count=len(shared_models),
        shared_models=tuple(sorted((model_labels.get(key) or key for key in shared_models), key=lambda item: (item.casefold(), item))),
        shared_benchmark_count=len(shared_benchmarks),
        shared_benchmarks=tuple(sorted((benchmark_labels.get(key) or key for key in shared_benchmarks), key=lambda item: (item.casefold(), item))),
        shared_model_benchmark_pair_count=len(shared_pairs),
        shared_model_benchmark_pairs=tuple(
            sorted(
                (
                    (model_labels.get(model_key) or model_key, benchmark_labels.get(benchmark_key) or benchmark_key)
                    for model_key, benchmark_key in shared_pairs
                ),
                key=lambda item: (item[0].casefold(), item[1].casefold(), item),
            )
        ),
        aligned_models=aligned_groups(model_records, shared_models, model_labels),
        aligned_benchmarks=aligned_groups(benchmark_records, shared_benchmarks, benchmark_labels),
        aligned_model_benchmarks=tuple(aligned_pairs),
        non_overlapping_models=non_overlapping(models_by_entity, shared_models, model_labels),
        non_overlapping_benchmarks=non_overlapping(benchmarks_by_entity, shared_benchmarks, benchmark_labels),
        non_overlapping_model_benchmark_pairs=pair_non_overlapping,
    )


def _metric_value(value: float | int | None) -> float | int | None:
    return value


def _pairwise_metric(
    name: str,
    first: ComparisonEntitySummary,
    second: ComparisonEntitySummary,
    first_value: float | int | None,
    second_value: float | int | None,
    direction: ComparisonDirection,
) -> MetricComparison:
    values: Mapping[str, float | int | None] = {
        first.identity: _metric_value(first_value),
        second.identity: _metric_value(second_value),
    }
    if first_value is None or second_value is None:
        reason = "one or both values unavailable"
        absolute = None
        percentage = None
    else:
        absolute = float(second_value) - float(first_value)
        if float(first_value) == 0.0:
            percentage = None
            reason = "baseline is zero"
        else:
            percentage = (absolute / float(first_value)) * 100.0
            reason = None
    return MetricComparison(
        metric_name=name,
        values=values,
        absolute_delta=absolute,
        percentage_delta=percentage,
        direction=direction,
        baseline_entity=first.identity,
        comparison_entity=second.identity,
        unavailable_reason=reason,
    )


def _low_percentage(distribution: CategoricalDistribution, level: str) -> float | None:
    if distribution.observed_count == 0:
        return None
    return distribution.percentages.get(level, 0.0)


def _categorical_comparisons(
    entities: Sequence[ComparisonEntitySummary],
) -> tuple[CategoricalComparison, ...]:
    """Flatten descriptive category distributions into comparison-ready rows."""

    rows: list[CategoricalComparison] = []
    distribution_fields = (
        ("hallucination", lambda entity: entity.hallucination),
        ("reliability", lambda entity: entity.reliability),
        ("benchmark_type", lambda entity: entity.benchmark_type),
    )
    for prefix, getter in distribution_fields:
        categories = sorted(
            {
                category
                for entity in entities
                for category in getter(entity).counts
            },
            key=lambda item: (item.casefold(), item),
        )
        for category in categories:
            counts = {entity.identity: getter(entity).counts.get(category, 0) for entity in entities}
            percentages = {
                entity.identity: getter(entity).percentages.get(category)
                for entity in entities
            }
            missing = {entity.identity: getter(entity).missing_count for entity in entities}
            rows.append(CategoricalComparison(f"{prefix}:{category}", counts, percentages, missing))
    return tuple(rows)


def _pairwise(first: ComparisonEntitySummary, second: ComparisonEntitySummary) -> PairwiseComparison:
    metrics = (
        _pairwise_metric("mean_overall_score", first, second, first.overall_score.mean, second.overall_score.mean, ComparisonDirection.HIGHER_IS_BETTER),
        _pairwise_metric("median_overall_score", first, second, first.overall_score.median, second.overall_score.median, ComparisonDirection.HIGHER_IS_BETTER),
        _pairwise_metric("mean_tokens_per_second", first, second, first.tokens_per_second.mean, second.tokens_per_second.mean, ComparisonDirection.HIGHER_IS_BETTER),
        _pairwise_metric("scored_run_count", first, second, first.scored_count, second.scored_count, ComparisonDirection.NEUTRAL),
        _pairwise_metric("low_hallucination_percentage", first, second, _low_percentage(first.hallucination, "Low"), _low_percentage(second.hallucination, "Low"), ComparisonDirection.HIGHER_IS_BETTER),
        _pairwise_metric("high_reliability_percentage", first, second, _low_percentage(first.reliability, "High"), _low_percentage(second.reliability, "High"), ComparisonDirection.HIGHER_IS_BETTER),
    )
    return PairwiseComparison(first.identity, second.identity, metrics)


def _rank_models(entities: Sequence[ComparisonEntitySummary]) -> tuple[ComparisonRankEntry, ...]:
    ordered = sorted(
        entities,
        key=lambda item: (
            item.overall_score.mean is None,
            -(item.overall_score.mean or 0.0) if item.overall_score.mean is not None else 0.0,
            -item.scored_count,
            -(item.overall_score.median or 0.0) if item.overall_score.median is not None else 0.0,
            item.label.casefold(),
            item.label,
            item.identity,
        ),
    )
    result: list[ComparisonRankEntry] = []
    rank = 0
    for item in ordered:
        if item.overall_score.mean is not None and item.scored_count:
            rank += 1
            displayed_rank: int | None = rank
        else:
            displayed_rank = None
        result.append(
            ComparisonRankEntry(
                entity=item.identity,
                label=item.label,
                rank=displayed_rank,
                mean_overall_score=item.overall_score.mean,
                scored_count=item.scored_count,
                median_overall_score=item.overall_score.median,
                mean_tokens_per_second=item.tokens_per_second.mean,
            )
        )
    return tuple(result)


def _rank_speed(entities: Sequence[ComparisonEntitySummary]) -> tuple[ComparisonRankEntry, ...]:
    ordered = sorted(
        entities,
        key=lambda item: (
            item.tokens_per_second.mean is None,
            -(item.tokens_per_second.mean or 0.0) if item.tokens_per_second.mean is not None else 0.0,
            item.label.casefold(),
            item.label,
            item.identity,
        ),
    )
    result: list[ComparisonRankEntry] = []
    rank = 0
    for item in ordered:
        if item.tokens_per_second.mean is not None:
            rank += 1
            displayed_rank: int | None = rank
        else:
            displayed_rank = None
        result.append(
            ComparisonRankEntry(
                entity=item.identity,
                label=item.label,
                rank=displayed_rank,
                mean_overall_score=item.overall_score.mean,
                scored_count=item.scored_count,
                median_overall_score=item.overall_score.median,
                mean_tokens_per_second=item.tokens_per_second.mean,
            )
        )
    return tuple(result)


def _session_metadata(session: BenchmarkSession) -> ComparisonSessionMetadata:
    return ComparisonSessionMetadata(
        id=session.id,
        title=session.title,
        description=session.description,
        started_at=session.started_at,
        completed_at=session.completed_at,
        notes=session.notes,
        is_deleted=session.is_deleted,
    )


def _session_identity(session: BenchmarkSession) -> str:
    if session.id is not None:
        return f"session:{session.id}"
    return f"session-title:{normalize_model_identity(session.title)}"


def _benchmark_subject_warnings(
    identity: str,
    summary: BenchmarkRunStatisticsSummary,
    *,
    subject_label: str = "subject",
) -> tuple[ComparisonWarning, ...]:
    warnings: list[ComparisonWarning] = []
    if summary.total_eligible_runs == 0:
        warnings.append(
            _warning(
                ComparisonWarningCode.SUBJECT_HAS_NO_RECORDS,
                ComparisonSourceFamily.BENCHMARK_RUN,
                f"The selected BenchmarkRun {subject_label} has no eligible records.",
                subject_identity=identity,
            )
        )
    if summary.overall_score.available_count == 0:
        warnings.append(
            _warning(
                ComparisonWarningCode.SUBJECT_HAS_NO_SCORE_VALUES,
                ComparisonSourceFamily.BENCHMARK_RUN,
                f"The BenchmarkRun {subject_label} has no numeric score values.",
                subject_identity=identity,
                metric="overall_score",
            )
        )
    if summary.tokens_per_second.available_count == 0:
        warnings.append(
            _warning(
                ComparisonWarningCode.SUBJECT_HAS_NO_THROUGHPUT_VALUES,
                ComparisonSourceFamily.BENCHMARK_RUN,
                f"The BenchmarkRun {subject_label} has no numeric throughput values.",
                subject_identity=identity,
                metric="tokens_per_second",
            )
        )
    if summary.reviewed_runs == 0:
        warnings.append(
            _warning(
                ComparisonWarningCode.SUBJECT_HAS_NO_REVIEW_DATA,
                ComparisonSourceFamily.BENCHMARK_RUN,
                f"The BenchmarkRun {subject_label} has no review data.",
                subject_identity=identity,
            )
        )
    return _ordered_warnings(warnings)


def _scoreboard_subject_warnings(
    identity: str,
    summary: ScoreboardStatisticsSummary,
) -> tuple[ComparisonWarning, ...]:
    warnings: list[ComparisonWarning] = []
    if summary.total_eligible_entries == 0:
        warnings.append(
            _warning(
                ComparisonWarningCode.SUBJECT_HAS_NO_RECORDS,
                ComparisonSourceFamily.SCOREBOARD,
                "The selected Scoreboard subject has no eligible records.",
                subject_identity=identity,
            )
        )
    if summary.score.available_count == 0:
        warnings.append(
            _warning(
                ComparisonWarningCode.SUBJECT_HAS_NO_SCORE_VALUES,
                ComparisonSourceFamily.SCOREBOARD,
                "The Scoreboard subject has no numeric score values.",
                subject_identity=identity,
                metric="score",
            )
        )
    if summary.tokens_per_second.available_count == 0:
        warnings.append(
            _warning(
                ComparisonWarningCode.SUBJECT_HAS_NO_THROUGHPUT_VALUES,
                ComparisonSourceFamily.SCOREBOARD,
                "The Scoreboard subject has no numeric throughput values.",
                subject_identity=identity,
                metric="tokens_per_second",
            )
        )
    return _ordered_warnings(warnings)


def _requested_model_keys(
    model_names: Sequence[str] | None,
    actual_labels: Mapping[str, str],
    source_family: ComparisonSourceFamily,
) -> tuple[tuple[tuple[str, str], ...], tuple[ComparisonWarning, ...]]:
    if model_names is None:
        return (
            tuple(
                (key, actual_labels[key])
                for key in sorted(
                    actual_labels,
                    key=lambda item: (
                        actual_labels[item].casefold(),
                        actual_labels[item],
                        item,
                    ),
                )
            ),
            (),
        )
    selected: list[tuple[str, str]] = []
    warnings: list[ComparisonWarning] = []
    seen: set[str] = set()
    for raw_value in model_names:
        label = str(raw_value).strip()
        identity = normalize_model_identity(label)
        if not label or identity == "unknown":
            warnings.append(
                _warning(
                    ComparisonWarningCode.MISSING_SUBJECT_IDENTITY,
                    source_family,
                    "A selected model subject has no meaningful identity.",
                    subject_identity=label or None,
                )
            )
            continue
        if identity in seen:
            warnings.append(
                _warning(
                    ComparisonWarningCode.DUPLICATE_NORMALIZED_SUBJECT,
                    source_family,
                    "Duplicate selected model identities were collapsed after normalization.",
                    subject_identity=identity,
                )
            )
            continue
        seen.add(identity)
        selected.append((identity, actual_labels.get(identity, label)))
    return tuple(selected), _ordered_warnings(warnings)


def _comparison_state(
    source_family: ComparisonSourceFamily,
    entities: Sequence[Any],
    *,
    filters: BenchmarkStatisticsFilters | ScoreboardStatisticsFilters,
    selected_explicitly: bool,
    subject_label: str = "subjects",
    source_record_count: int | None = None,
    filtered_record_count: int | None = None,
) -> tuple[ComparisonResultState, tuple[ComparisonWarning, ...]]:
    warnings: list[ComparisonWarning] = []
    if selected_explicitly and len(entities) < 2:
        warnings.append(
            _warning(
                ComparisonWarningCode.INSUFFICIENT_SUBJECTS,
                source_family,
                f"At least two distinct {subject_label} are required for comparison.",
            )
        )
        return ComparisonResultState.INSUFFICIENT_SUBJECTS, _ordered_warnings(warnings)
    if source_record_count is not None and source_record_count <= 0:
        warnings.append(
            _warning(
                ComparisonWarningCode.NO_SOURCE_DATA,
                source_family,
                "No eligible records are available for comparison.",
            )
        )
        return ComparisonResultState.NO_SOURCE_DATA, _ordered_warnings(warnings)
    if (
        source_record_count is not None
        and source_record_count > 0
        and filtered_record_count is not None
        and filtered_record_count <= 0
    ):
        warnings.append(
            _warning(
                ComparisonWarningCode.FILTERS_NO_RECORDS,
                source_family,
                "The selected filters produced no eligible records.",
            )
        )
        return ComparisonResultState.FILTERS_NO_RECORDS, _ordered_warnings(warnings)
    if not entities:
        if _filters_are_active(filters):
            warnings.append(
                _warning(
                    ComparisonWarningCode.FILTERS_NO_RECORDS,
                    source_family,
                    "The selected filters produced no eligible records.",
                )
            )
            return ComparisonResultState.FILTERS_NO_RECORDS, _ordered_warnings(warnings)
        code = (
            ComparisonWarningCode.SELECTED_SUBJECT_UNAVAILABLE
            if selected_explicitly
            else ComparisonWarningCode.NO_SOURCE_DATA
        )
        warnings.append(
            _warning(
                code,
                source_family,
                f"The selected {subject_label} have no eligible records."
                if selected_explicitly
                else "No eligible records are available for comparison.",
            )
        )
        return (
            ComparisonResultState.SELECTED_SUBJECTS_UNAVAILABLE
            if selected_explicitly
            else ComparisonResultState.NO_SOURCE_DATA,
            _ordered_warnings(warnings),
        )
    if len(entities) < 2:
        warnings.append(
            _warning(
                ComparisonWarningCode.INSUFFICIENT_SUBJECTS,
                source_family,
                f"At least two distinct {subject_label} are required for comparison.",
            )
        )
        return ComparisonResultState.INSUFFICIENT_SUBJECTS, _ordered_warnings(warnings)

    counts = [int(entity.record_count) for entity in entities]
    if all(count == 0 for count in counts):
        if (
            selected_explicitly
            and filtered_record_count is not None
            and filtered_record_count > 0
        ):
            warnings.append(
                _warning(
                    ComparisonWarningCode.SELECTED_SUBJECT_UNAVAILABLE,
                    source_family,
                    f"The selected {subject_label} have no eligible records.",
                )
            )
            return ComparisonResultState.SELECTED_SUBJECTS_UNAVAILABLE, _ordered_warnings(warnings)
        if _filters_are_active(filters):
            warnings.append(
                _warning(
                    ComparisonWarningCode.FILTERS_NO_RECORDS,
                    source_family,
                    "The selected filters produced no eligible records.",
                )
            )
            return ComparisonResultState.FILTERS_NO_RECORDS, _ordered_warnings(warnings)
        if selected_explicitly:
            warnings.append(
                _warning(
                    ComparisonWarningCode.SELECTED_SUBJECT_UNAVAILABLE,
                    source_family,
                    f"The selected {subject_label} have no eligible records.",
                )
            )
            return ComparisonResultState.SELECTED_SUBJECTS_UNAVAILABLE, _ordered_warnings(warnings)
        warnings.append(
            _warning(
                ComparisonWarningCode.NO_SOURCE_DATA,
                source_family,
                "No eligible records are available for comparison.",
            )
        )
        return ComparisonResultState.NO_SOURCE_DATA, _ordered_warnings(warnings)
    if any(count == 0 for count in counts):
        warnings.append(
            _warning(
                ComparisonWarningCode.SELECTED_SUBJECT_UNAVAILABLE,
                source_family,
                f"One or more selected {subject_label} have no eligible records.",
            )
        )
        return ComparisonResultState.SELECTED_SUBJECTS_UNAVAILABLE, _ordered_warnings(warnings)
    if any(
        getattr(entity.summary, "score", getattr(entity.summary, "overall_score", NumericSummary())).missing_count > 0
        or entity.summary.tokens_per_second.missing_count > 0
        or (
            source_family is ComparisonSourceFamily.BENCHMARK_RUN
            and (
                entity.review.total_reviews == 0
                or any(
                    summary.missing_count > 0
                    for summary in (
                        entity.review.accuracy_score,
                        entity.review.depth_score,
                        entity.review.signal_noise_score,
                        entity.review.actionability_score,
                        entity.review.seniority_score,
                        entity.review.overall_score,
                    )
                )
            )
        )
        for entity in entities
    ):
        return ComparisonResultState.READY_WITH_MISSING_VALUES, ()
    return ComparisonResultState.READY, ()


class ComparisonService:
    """Application-facing facade for model and session comparisons."""

    def __init__(
        self,
        service: BenchmarkService,
        catalog: CatalogService | None = None,
        statistics: StatisticsService | None = None,
    ):
        self.service = service
        self.catalog = catalog or service.catalog
        self.statistics = statistics or StatisticsService(service, self.catalog)

    def _eligible_runs(
        self,
        runs: Sequence[Any] | None,
        filters: BenchmarkStatisticsFilters,
        *,
        clear_session: bool = False,
    ) -> tuple[Any, ...]:
        self._validate_benchmark_source(runs)
        selection_filters = self._benchmark_selection_filters(filters, clear_session=clear_session)
        return self.statistics.select_benchmark_runs(runs, filters=selection_filters)

    @staticmethod
    def _benchmark_selection_filters(
        filters: BenchmarkStatisticsFilters,
        *,
        clear_session: bool = False,
    ) -> BenchmarkStatisticsFilters:
        """Return the immutable filters actually applied to BenchmarkRun selection."""

        selection_filters = replace(filters, model="")
        if clear_session:
            selection_filters = replace(selection_filters, session="", session_id=None)
        return selection_filters

    def _benchmark_source_count(self, runs: Sequence[Any] | None) -> int:
        """Count BenchmarkRun source records before comparison filters are applied."""

        return len(
            self.statistics.select_benchmark_runs(
                runs,
                filters=BenchmarkStatisticsFilters(include_deleted=True),
            )
        )

    def _scoreboard_source_count(
        self,
        entries: Sequence[Any] | None,
        batches: Sequence[Any] | None,
    ) -> int:
        """Count ScoreboardEntry source records before comparison filters are applied."""

        return len(
            self.statistics.select_scoreboard_entries(
                entries,
                batches=batches,
                filters=ScoreboardStatisticsFilters(include_deleted=True),
            )
        )

    @staticmethod
    def _selected_model_keys(
        model_names: Sequence[str] | None,
        aggregates: Sequence[Any],
    ) -> tuple[tuple[str, str], ...]:
        actual_labels: dict[str, str] = {}
        for aggregate in aggregates:
            label = model_snapshot_name(aggregate.run.model_snapshot)
            key = normalize_model_identity(label)
            if key != "unknown":
                actual_labels[key] = _canonical_label(actual_labels.get(key), label)
        if model_names is None:
            return tuple((key, actual_labels[key]) for key in sorted(actual_labels, key=lambda item: (actual_labels[item].casefold(), actual_labels[item], item)))
        selected: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw_name in model_names:
            label = str(raw_name).strip()
            key = normalize_model_identity(label)
            if not label or key == "unknown" or key in seen:
                continue
            seen.add(key)
            selected.append((key, actual_labels.get(key, label)))
        return tuple(selected)

    @staticmethod
    def _validate_benchmark_source(runs: Sequence[Any] | None) -> None:
        if runs is None:
            return
        for value in runs:
            run = getattr(value, "run", value)
            if not hasattr(run, "model_snapshot") or not hasattr(run, "benchmark_snapshot"):
                raise TypeError("BenchmarkRun comparison requires BenchmarkRun records")

    def discover_benchmark_model_subjects(
        self,
        runs: Sequence[Any] | None = None,
        *,
        filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
    ) -> ComparisonSubjectDiscovery:
        """Discover selectable model identities from historical snapshots."""

        self._validate_benchmark_source(runs)
        selection_filters = replace(filters, model="")
        groups = self.statistics.group_benchmark_runs(
            runs,
            group_by=BenchmarkRunGroupBy.MODEL,
            filters=selection_filters,
        )
        source_record_count = self._benchmark_source_count(runs)
        filtered_record_count = sum(group.record_count for group in groups)
        subjects: list[ComparisonSubject] = []
        warnings: list[ComparisonWarning] = []
        excluded = 0
        for group in groups:
            if group.key == "unknown":
                excluded += group.record_count
                warnings.append(
                    _warning(
                        ComparisonWarningCode.MISSING_SUBJECT_IDENTITY,
                        ComparisonSourceFamily.BENCHMARK_RUN,
                        "BenchmarkRun records without a meaningful model snapshot identity were excluded.",
                    )
                )
                continue
            group_warnings = _benchmark_subject_warnings(group.key, group.summary)
            subjects.append(
                ComparisonSubject(
                    source_family=ComparisonSourceFamily.BENCHMARK_RUN,
                    identity=group.key,
                    label=group.label,
                    eligible_record_count=group.record_count,
                    score_available=group.overall_score.available_count > 0,
                    throughput_available=group.tokens_per_second.available_count > 0,
                    review_available=group.summary.reviewed_runs > 0,
                    category_availability={
                        "hallucination": group.hallucination.observed_count > 0,
                        "reliability": group.reliability.observed_count > 0,
                        "benchmark_type": group.summary.benchmark_type.observed_count > 0,
                    },
                    warnings=group_warnings,
                )
            )
            warnings.extend(group_warnings)
        if not subjects:
            if source_record_count <= 0:
                code = ComparisonWarningCode.NO_SOURCE_DATA
            elif filtered_record_count <= 0:
                code = ComparisonWarningCode.FILTERS_NO_RECORDS
            else:
                # Records exist but none has a selectable model identity.
                code = ComparisonWarningCode.NO_SOURCE_DATA
            warnings.append(
                _warning(
                    code,
                    ComparisonSourceFamily.BENCHMARK_RUN,
                    "No selectable BenchmarkRun model subjects are available."
                    if code is ComparisonWarningCode.NO_SOURCE_DATA
                    else "The selected filters produced no selectable BenchmarkRun model subjects.",
                )
            )
            state = (
                ComparisonResultState.FILTERS_NO_RECORDS
                if code is ComparisonWarningCode.FILTERS_NO_RECORDS
                else ComparisonResultState.NO_SOURCE_DATA
            )
        else:
            state = ComparisonResultState.READY
        return ComparisonSubjectDiscovery(
            source_family=ComparisonSourceFamily.BENCHMARK_RUN,
            subjects=tuple(subjects),
            warnings=_ordered_warnings(warnings),
            state=state,
            excluded_record_count=excluded,
        )

    def available_benchmark_model_subjects(
        self,
        runs: Sequence[Any] | None = None,
        *,
        filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
    ) -> tuple[ComparisonSubject, ...]:
        return self.discover_benchmark_model_subjects(runs, filters=filters).subjects

    def benchmark_model_subjects(
        self,
        runs: Sequence[Any] | None = None,
        *,
        filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
    ) -> ComparisonSubjectDiscovery:
        return self.discover_benchmark_model_subjects(runs, filters=filters)

    def discover_scoreboard_model_subjects(
        self,
        entries: Sequence[Any] | None = None,
        *,
        batches: Sequence[Any] | None = None,
        filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters(),
    ) -> ComparisonSubjectDiscovery:
        """Discover selectable model identities from ScoreboardEntry records."""

        self._validate_scoreboard_source(entries)
        selection_filters = replace(filters, model="")
        groups = self.statistics.group_scoreboard_entries(
            entries,
            batches=batches,
            group_by=ScoreboardGroupBy.MODEL,
            filters=selection_filters,
        )
        source_record_count = self._scoreboard_source_count(entries, batches)
        filtered_record_count = sum(group.record_count for group in groups)
        subjects: list[ComparisonSubject] = []
        warnings: list[ComparisonWarning] = []
        excluded = 0
        for group in groups:
            if group.key == "unknown":
                excluded += group.record_count
                warnings.append(
                    _warning(
                        ComparisonWarningCode.MISSING_SUBJECT_IDENTITY,
                        ComparisonSourceFamily.SCOREBOARD,
                        "ScoreboardEntry records without a meaningful model identity were excluded.",
                    )
                )
                continue
            group_warnings = _scoreboard_subject_warnings(group.key, group.summary)
            subjects.append(
                ComparisonSubject(
                    source_family=ComparisonSourceFamily.SCOREBOARD,
                    identity=group.key,
                    label=group.label,
                    eligible_record_count=group.record_count,
                    score_available=group.score.available_count > 0,
                    throughput_available=group.tokens_per_second.available_count > 0,
                    category_availability={
                        "hallucination": group.hallucination.observed_count > 0,
                        "consistency": group.consistency.observed_count > 0,
                        "reliability": group.reliability.observed_count > 0,
                    },
                    warnings=group_warnings,
                )
            )
            warnings.extend(group_warnings)
        if not subjects:
            if source_record_count <= 0:
                code = ComparisonWarningCode.NO_SOURCE_DATA
            elif filtered_record_count <= 0:
                code = ComparisonWarningCode.FILTERS_NO_RECORDS
            else:
                # Records exist but none has a selectable model identity.
                code = ComparisonWarningCode.NO_SOURCE_DATA
            warnings.append(
                _warning(
                    code,
                    ComparisonSourceFamily.SCOREBOARD,
                    "No selectable Scoreboard model subjects are available."
                    if code is ComparisonWarningCode.NO_SOURCE_DATA
                    else "The selected filters produced no selectable Scoreboard model subjects.",
                )
            )
            state = (
                ComparisonResultState.FILTERS_NO_RECORDS
                if code is ComparisonWarningCode.FILTERS_NO_RECORDS
                else ComparisonResultState.NO_SOURCE_DATA
            )
        else:
            state = ComparisonResultState.READY
        return ComparisonSubjectDiscovery(
            source_family=ComparisonSourceFamily.SCOREBOARD,
            subjects=tuple(subjects),
            warnings=_ordered_warnings(warnings),
            state=state,
            excluded_record_count=excluded,
        )

    def available_scoreboard_model_subjects(
        self,
        entries: Sequence[Any] | None = None,
        *,
        batches: Sequence[Any] | None = None,
        filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters(),
    ) -> tuple[ComparisonSubject, ...]:
        return self.discover_scoreboard_model_subjects(
            entries,
            batches=batches,
            filters=filters,
        ).subjects

    def scoreboard_model_subjects(
        self,
        entries: Sequence[Any] | None = None,
        *,
        batches: Sequence[Any] | None = None,
        filters: ScoreboardStatisticsFilters = ScoreboardStatisticsFilters(),
    ) -> ComparisonSubjectDiscovery:
        return self.discover_scoreboard_model_subjects(
            entries,
            batches=batches,
            filters=filters,
        )

    def compare_benchmark_models(
        self,
        request: BenchmarkModelComparisonRequest | Sequence[str],
        runs: Sequence[Any] | None = None,
    ) -> ModelComparisonResult:
        """Return a typed BenchmarkRun model comparison without database writes."""

        if not isinstance(request, BenchmarkModelComparisonRequest):
            request = BenchmarkModelComparisonRequest(tuple(request))
        self._validate_benchmark_source(runs)
        effective_filters = self._benchmark_selection_filters(request.filters)
        aggregates = self._eligible_runs(runs, request.filters)
        source_record_count = self._benchmark_source_count(runs)
        actual_labels: dict[str, str] = {}
        for aggregate in aggregates:
            label = model_snapshot_name(aggregate.run.model_snapshot)
            key = normalize_model_identity(label)
            if key != "unknown":
                actual_labels[key] = _canonical_label(actual_labels.get(key), label)
        requested = request.selected_models if request.selected_models else None
        selected, request_warnings = _requested_model_keys(
            requested,
            actual_labels,
            ComparisonSourceFamily.BENCHMARK_RUN,
        )
        entity_records = {key: _model_records(aggregates, key) for key, _ in selected}
        entities: list[ComparisonEntitySummary] = []
        warnings: list[ComparisonWarning] = list(request_warnings)
        for key, label in selected:
            entity_warnings = list(
                _benchmark_subject_warnings(
                    key,
                    self.statistics.benchmark_run_statistics(
                        entity_records[key],
                        filters=BenchmarkStatisticsFilters(include_deleted=True),
                    ),
                )
            )
            if key not in actual_labels and request.selected_models:
                entity_warnings.append(
                    _warning(
                        ComparisonWarningCode.SELECTED_SUBJECT_UNAVAILABLE,
                        ComparisonSourceFamily.BENCHMARK_RUN,
                        "The selected BenchmarkRun model was not present after filtering.",
                        subject_identity=key,
                    )
                )
            entity = _entity_summary(
                self.statistics,
                key,
                label,
                entity_records[key],
                warnings=entity_warnings,
            )
            entities.append(entity)
            warnings.extend(entity.warnings)
        state, state_warnings = _comparison_state(
            ComparisonSourceFamily.BENCHMARK_RUN,
            entities,
            filters=effective_filters,
            selected_explicitly=bool(request.selected_models),
            source_record_count=source_record_count,
            filtered_record_count=len(aggregates),
        )
        warnings.extend(state_warnings)
        alignment = _benchmark_alignment(self.statistics, entity_records)
        metadata = ComparisonMetadata(
            comparison_type=ComparisonType.MODEL,
            title=request.title,
            generated_at=request.generated_at or now(),
            contributing_record_count=sum(entity.record_count for entity in entities),
            active_filters=_filter_mapping(request.filters),
            selected_entities=tuple(entity.label for entity in entities),
            source_family=ComparisonSourceFamily.BENCHMARK_RUN,
        )
        pairwise = _pairwise(entities[0], entities[1]) if len(entities) == 2 else None
        return ModelComparisonResult(
            metadata=metadata,
            selected_models=tuple(entity.label for entity in entities),
            entities=tuple(entities),
            alignment=alignment,
            ranking=_rank_models(entities),
            speed_ranking=_rank_speed(entities),
            categorical_comparisons=_categorical_comparisons(entities),
            pairwise=pairwise,
            state=state,
            warnings=_ordered_warnings(warnings),
            filters=request.filters,
        )

    @staticmethod
    def _validate_scoreboard_source(entries: Sequence[Any] | None) -> None:
        if entries is None:
            return
        for value in entries:
            entry = getattr(value, "entry", value)
            if not hasattr(entry, "model_name") or not hasattr(entry, "import_batch_id"):
                raise TypeError("Scoreboard comparison requires ScoreboardEntry records")

    def benchmark_model_comparison(
        self,
        request: BenchmarkModelComparisonRequest | Sequence[str],
        runs: Sequence[Any] | None = None,
    ) -> ModelComparisonResult:
        return self.compare_benchmark_models(request, runs)

    def compare_scoreboard_models(
        self,
        request: ScoreboardModelComparisonRequest | Sequence[str],
        entries: Sequence[Any] | None = None,
        *,
        batches: Sequence[Any] | None = None,
    ) -> ScoreboardModelComparisonResult:
        """Compare ScoreboardEntry model subjects without mixing source families."""

        if not isinstance(request, ScoreboardModelComparisonRequest):
            request = ScoreboardModelComparisonRequest(tuple(request))
        self._validate_scoreboard_source(entries)
        selection_filters = replace(request.filters, model="")
        aggregates = self.statistics.select_scoreboard_entries(
            entries,
            batches=batches,
            filters=selection_filters,
        )
        source_record_count = self._scoreboard_source_count(entries, batches)
        actual_labels: dict[str, str] = {}
        for aggregate in aggregates:
            label = str(aggregate.entry.model_name or "").strip()
            key = normalize_model_identity(label)
            if key != "unknown":
                actual_labels[key] = _canonical_label(actual_labels.get(key), label)
        requested = request.selected_models if request.selected_models else None
        selected, request_warnings = _requested_model_keys(
            requested,
            actual_labels,
            ComparisonSourceFamily.SCOREBOARD,
        )
        entity_records = {
            key: tuple(
                aggregate
                for aggregate in aggregates
                if normalize_model_identity(str(aggregate.entry.model_name or "").strip()) == key
            )
            for key, _ in selected
        }
        entities: list[ScoreboardComparisonEntitySummary] = []
        warnings: list[ComparisonWarning] = list(request_warnings)
        for key, label in selected:
            summary = self.statistics.scoreboard_statistics(
                entity_records[key],
                filters=ScoreboardStatisticsFilters(include_deleted=True),
            )
            entity_warnings = list(_scoreboard_subject_warnings(key, summary))
            if key not in actual_labels and request.selected_models:
                entity_warnings.append(
                    _warning(
                        ComparisonWarningCode.SELECTED_SUBJECT_UNAVAILABLE,
                        ComparisonSourceFamily.SCOREBOARD,
                        "The selected Scoreboard model was not present after filtering.",
                        subject_identity=key,
                    )
                )
            entity = _scoreboard_entity_summary(
                self.statistics,
                key,
                label,
                entity_records[key],
                warnings=entity_warnings,
            )
            entities.append(entity)
            warnings.extend(entity.warnings)
        state, state_warnings = _comparison_state(
            ComparisonSourceFamily.SCOREBOARD,
            entities,
            filters=selection_filters,
            selected_explicitly=bool(request.selected_models),
            source_record_count=source_record_count,
            filtered_record_count=len(aggregates),
        )
        warnings.extend(state_warnings)
        metadata = ComparisonMetadata(
            comparison_type=ComparisonType.MODEL,
            title=request.title,
            generated_at=request.generated_at or now(),
            contributing_record_count=sum(entity.record_count for entity in entities),
            active_filters=_filter_mapping(request.filters),
            selected_entities=tuple(entity.label for entity in entities),
            source_family=ComparisonSourceFamily.SCOREBOARD,
        )
        pairwise = _scoreboard_pairwise(entities[0], entities[1]) if len(entities) == 2 else None
        return ScoreboardModelComparisonResult(
            metadata=metadata,
            selected_models=tuple(entity.identity for entity in entities),
            entities=tuple(entities),
            categorical_comparisons=_scoreboard_categorical_comparisons(entities),
            pairwise=pairwise,
            state=state,
            warnings=_ordered_warnings(warnings),
            filters=request.filters,
        )

    def scoreboard_model_comparison(
        self,
        request: ScoreboardModelComparisonRequest | Sequence[str],
        entries: Sequence[Any] | None = None,
        *,
        batches: Sequence[Any] | None = None,
    ) -> ScoreboardModelComparisonResult:
        return self.compare_scoreboard_models(request, entries, batches=batches)

    def compare_models(
        self,
        models: Sequence[str] | None = None,
        runs: Sequence[Any] | None = None,
        *,
        filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
        title: str = "Model Comparison",
        generated_at: str | None = None,
    ) -> ModelComparisonResult:
        effective_filters = self._benchmark_selection_filters(filters)
        aggregates = self._eligible_runs(runs, filters)
        source_record_count = self._benchmark_source_count(runs)
        selected = self._selected_model_keys(models, aggregates)
        if len(selected) < 2:
            raise ValueError("At least two distinct models are required for comparison")
        entity_records = {key: _model_records(aggregates, key) for key, _ in selected}
        entities_list: list[ComparisonEntitySummary] = []
        warnings: list[ComparisonWarning] = []
        for key, label in selected:
            summary = self.statistics.benchmark_run_statistics(
                entity_records[key],
                filters=BenchmarkStatisticsFilters(include_deleted=True),
            )
            entity = _entity_summary(
                self.statistics,
                key,
                label,
                entity_records[key],
                warnings=_benchmark_subject_warnings(key, summary),
            )
            entities_list.append(entity)
            warnings.extend(entity.warnings)
        entities = tuple(entities_list)
        state, state_warnings = _comparison_state(
            ComparisonSourceFamily.BENCHMARK_RUN,
            entities,
            filters=effective_filters,
            selected_explicitly=models is not None,
            source_record_count=source_record_count,
            filtered_record_count=len(aggregates),
        )
        warnings.extend(state_warnings)
        alignment = _benchmark_alignment(self.statistics, entity_records)
        metadata = ComparisonMetadata(
            comparison_type=ComparisonType.MODEL,
            title=title,
            generated_at=generated_at or now(),
            contributing_record_count=sum(entity.record_count for entity in entities),
            active_filters=_filter_mapping(filters),
            selected_entities=tuple(entity.label for entity in entities),
            source_family=ComparisonSourceFamily.BENCHMARK_RUN,
        )
        pairwise = _pairwise(entities[0], entities[1]) if len(entities) == 2 else None
        return ModelComparisonResult(
            metadata=metadata,
            selected_models=tuple(entity.label for entity in entities),
            entities=entities,
            alignment=alignment,
            ranking=_rank_models(entities),
            speed_ranking=_rank_speed(entities),
            categorical_comparisons=_categorical_comparisons(entities),
            pairwise=pairwise,
            state=state,
            warnings=_ordered_warnings(warnings),
            filters=filters,
        )

    def _resolve_sessions(
        self,
        sessions: Sequence[int | BenchmarkSession] | None,
        filters: BenchmarkStatisticsFilters,
        aggregates: Sequence[Any],
    ) -> tuple[BenchmarkSession, ...]:
        if sessions is None:
            candidates = list(self.catalog.sessions.list(include_deleted=filters.include_deleted))
            if not candidates:
                by_key: dict[str, BenchmarkSession] = {}
                for aggregate in aggregates:
                    if aggregate.session is not None:
                        by_key.setdefault(_session_identity(aggregate.session), aggregate.session)
                candidates = list(by_key.values())
            candidates.sort(key=lambda item: ((item.title or "").casefold(), item.title or "", item.id or 0))
        else:
            candidates = []
            seen: set[str] = set()
            for value in sessions:
                session = self.catalog.sessions.get(value) if isinstance(value, int) else value
                if session is None:
                    raise ValueError(f"Session {value} was not found")
                key = _session_identity(session)
                if key not in seen:
                    seen.add(key)
                    candidates.append(session)
        if not filters.include_deleted:
            candidates = [session for session in candidates if not session.is_deleted]
        return tuple(candidates)

    @staticmethod
    def _session_records(aggregates: Sequence[Any], session: BenchmarkSession) -> tuple[Any, ...]:
        target_key = _session_identity(session)
        return tuple(aggregate for aggregate in aggregates if _session_key_for(aggregate) == target_key)

    def compare_sessions(
        self,
        sessions: Sequence[int | BenchmarkSession] | None = None,
        runs: Sequence[Any] | None = None,
        *,
        filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(),
        title: str = "Session Comparison",
        generated_at: str | None = None,
    ) -> SessionComparisonResult:
        effective_filters = self._benchmark_selection_filters(filters, clear_session=True)
        aggregates = self._eligible_runs(runs, filters, clear_session=True)
        source_record_count = self._benchmark_source_count(runs)
        selected_sessions = self._resolve_sessions(sessions, filters, aggregates)
        entity_records: dict[str, tuple[Any, ...]] = {
            _session_identity(session): self._session_records(aggregates, session)
            for session in selected_sessions
        }
        session_metadata = tuple(_session_metadata(session) for session in selected_sessions)
        entities_list: list[ComparisonEntitySummary] = []
        warnings: list[ComparisonWarning] = []
        for session in selected_sessions:
            identity = _session_identity(session)
            records = entity_records[identity]
            summary = self.statistics.benchmark_run_statistics(
                records,
                filters=BenchmarkStatisticsFilters(include_deleted=True),
            )
            entity = _entity_summary(
                self.statistics,
                identity,
                session.title or f"Session {session.id}",
                records,
                session=_session_metadata(session),
                warnings=_benchmark_subject_warnings(
                    identity,
                    summary,
                    subject_label="session",
                ),
            )
            entities_list.append(entity)
            warnings.extend(entity.warnings)
        entities = tuple(entities_list)
        alignment = _session_alignment(self.statistics, entity_records)
        metadata = ComparisonMetadata(
            comparison_type=ComparisonType.SESSION,
            title=title,
            generated_at=generated_at or now(),
            contributing_record_count=sum(entity.record_count for entity in entities),
            active_filters=_filter_mapping(filters),
            selected_entities=tuple(entity.label for entity in entities),
        )
        state, state_warnings = _comparison_state(
            ComparisonSourceFamily.BENCHMARK_RUN,
            entities,
            filters=effective_filters,
            selected_explicitly=sessions is not None,
            subject_label="sessions",
            source_record_count=source_record_count,
            filtered_record_count=len(aggregates),
        )
        warnings.extend(state_warnings)
        pairwise = _pairwise(entities[0], entities[1]) if len(entities) == 2 else None
        return SessionComparisonResult(
            metadata=metadata,
            selected_sessions=session_metadata,
            entities=entities,
            alignment=alignment,
            categorical_comparisons=_categorical_comparisons(entities),
            pairwise=pairwise,
            state=state,
            warnings=_ordered_warnings(warnings),
            filters=filters,
        )

    def model_comparison(self, models: Sequence[str] | None = None, runs: Sequence[Any] | None = None, *, filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(), title: str = "Model Comparison", generated_at: str | None = None) -> ModelComparisonResult:
        return self.compare_models(models, runs, filters=filters, title=title, generated_at=generated_at)

    def session_comparison(self, sessions: Sequence[int | BenchmarkSession] | None = None, runs: Sequence[Any] | None = None, *, filters: BenchmarkStatisticsFilters = BenchmarkStatisticsFilters(), title: str = "Session Comparison", generated_at: str | None = None) -> SessionComparisonResult:
        return self.compare_sessions(sessions, runs, filters=filters, title=title, generated_at=generated_at)


__all__ = (
    "AlignedBenchmarkSummary",
    "AlignedEntityStatistics",
    "AlignedModelBenchmarkSummary",
    "CategoricalComparison",
    "ComparisonCategory",
    "ComparisonDirection",
    "ComparisonEntitySummary",
    "ComparisonMetadata",
    "ComparisonMetric",
    "ComparisonOutcome",
    "ComparisonRankEntry",
    "ComparisonResultState",
    "ComparisonService",
    "ComparisonSessionMetadata",
    "ComparisonSourceFamily",
    "ComparisonState",
    "ComparisonSubject",
    "ComparisonSubjectDiscovery",
    "ComparisonType",
    "ComparisonWarning",
    "ComparisonWarningCode",
    "BenchmarkModelComparisonRequest",
    "BenchmarkRunModelComparisonRequest",
    "MetricComparison",
    "ModelComparisonAlignment",
    "ModelComparisonResult",
    "ModelComparisonReport",
    "PairwiseComparison",
    "ScoreboardModelComparisonRequest",
    "ScoreboardComparisonRequest",
    "ScoreboardComparisonEntitySummary",
    "ScoreboardModelComparisonResult",
    "ScoreboardComparisonResult",
    "SessionComparisonAlignment",
    "SessionComparisonResult",
    "SessionComparisonReport",
)


# Friendly public aliases mirror the terminology used by the reporting layer.
ComparisonMetric = MetricComparison
ComparisonCategory = CategoricalComparison
ModelComparisonReport = ModelComparisonResult
SessionComparisonReport = SessionComparisonResult
ScoreboardComparisonResult = ScoreboardModelComparisonResult
ComparisonOutcome = ComparisonResultState
ComparisonState = ComparisonResultState
BenchmarkRunModelComparisonRequest = BenchmarkModelComparisonRequest
ScoreboardComparisonRequest = ScoreboardModelComparisonRequest
