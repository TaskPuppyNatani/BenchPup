"""UI-independent reports and leaderboards for BenchPup records.

The reporting layer owns selection, aggregation, report models, and Markdown
rendering.  It deliberately keeps :class:`BenchmarkRun` and
:class:`ScoreboardEntry` as separate source record families.
"""

from __future__ import annotations

import os
import tempfile
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence, TypeAlias, cast
from types import MappingProxyType

from .domain import (
    BenchmarkRun,
    BenchmarkSession,
    ReviewScore,
    RunAttachment,
    ScoreboardEntry,
    ScoreboardImportBatch,
    now,
)
from .comparisons import ModelComparisonResult, SessionComparisonResult
from .services import BenchmarkService, CatalogService
from .statistics import numeric_summary
from .trends import TrendReport, TrendSeries

if TYPE_CHECKING:
    from .html_reporting import HtmlAnalyticsReport, HtmlAnalyticsReportOptions


class ReportType(str, Enum):
    """Stable identifiers for the report families produced by this module."""

    BENCHMARK_RUNS = "benchmark_runs"
    SCOREBOARD = "scoreboard"
    MODEL_LEADERBOARD = "model_leaderboard"
    SESSION = "session"
    SESSION_REPORT = "session"
    SESSION_REPORTS = "session"
    HARDWARE = "hardware"
    HARDWARE_REPORT = "hardware"
    HARDWARE_REPORTS = "hardware"


class ReportTemplateId(str, Enum):
    """Stable identifiers for the built-in presentation templates."""

    CONCISE = "concise"
    STANDARD = "standard"
    FULL_AUDIT = "full_audit"


class ReportWriteStatus(str, Enum):
    """Result states for safe Markdown output."""

    SUCCESS = "success"
    OVERWRITE_REQUIRED = "overwrite_required"
    TEMP_WRITE_FAILED = "temp_write_failed"
    FINALIZE_FAILED = "finalize_failed"


@dataclass
class ReportTemplateOptions:
    """Mutable, isolated presentation and inclusion options for one report."""

    template_id: str = ReportTemplateId.STANDARD.value
    include_record_details: bool = True
    include_model_details: bool = False
    include_hardware_details: bool = False
    include_prompt_text: bool = False
    include_raw_model_output: bool = False
    include_attachment_metadata: bool = False


@dataclass(frozen=True)
class ReportTemplate:
    """Immutable built-in report template definition."""

    template_id: str
    name: str
    description: str
    include_record_details: bool = True
    include_model_details: bool = False
    include_hardware_details: bool = False
    include_prompt_text: bool = False
    include_raw_model_output: bool = False
    include_attachment_metadata: bool = False

    def apply(self) -> ReportTemplateOptions:
        """Return a fresh options object without exposing the definition."""

        return ReportTemplateOptions(
            template_id=self.template_id,
            include_record_details=self.include_record_details,
            include_model_details=self.include_model_details,
            include_hardware_details=self.include_hardware_details,
            include_prompt_text=self.include_prompt_text,
            include_raw_model_output=self.include_raw_model_output,
            include_attachment_metadata=self.include_attachment_metadata,
        )


_BUILT_IN_REPORT_TEMPLATES: tuple[ReportTemplate, ...] = (
    ReportTemplate(
        ReportTemplateId.CONCISE.value,
        "Concise",
        "Summary metadata and compact report tables.",
        include_record_details=False,
        include_model_details=False,
    ),
    ReportTemplate(
        ReportTemplateId.STANDARD.value,
        "Standard",
        "Summary, tables, verdicts, strengths, and weaknesses.",
        include_record_details=True,
        include_model_details=False,
    ),
    ReportTemplate(
        ReportTemplateId.FULL_AUDIT.value,
        "Full Audit",
        "All standard details with opt-in prompt, output, and attachment metadata.",
        include_record_details=True,
        include_model_details=True,
        include_hardware_details=True,
    ),
)


def available_report_templates() -> tuple[ReportTemplate, ...]:
    """Return immutable built-in definitions in their display order."""

    return _BUILT_IN_REPORT_TEMPLATES


def get_report_template(template: str | ReportTemplateId) -> ReportTemplate | None:
    template_id = template.value if isinstance(template, ReportTemplateId) else str(template)
    return next((item for item in _BUILT_IN_REPORT_TEMPLATES if item.template_id == template_id), None)


def apply_report_template(template: str | ReportTemplateId | ReportTemplate) -> ReportTemplateOptions:
    """Apply one built-in definition and return an independent options object."""

    definition = template if isinstance(template, ReportTemplate) else get_report_template(template)
    if definition is None:
        raise ValueError(f"Unknown report template: {template}")
    return definition.apply()


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return deepcopy(value)


def _freeze_mapping(value: Mapping[Any, Any] | None = None) -> Mapping[Any, Any]:
    return cast(Mapping[Any, Any], _freeze_value(dict(value or {})))


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    lowered = str(value).strip().casefold()
    if lowered in {"true", "yes", "1"}:
        return True
    if lowered in {"false", "no", "0"}:
        return False
    return None


def _value(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def _contains(value: Any, needle: str) -> bool:
    return needle.casefold() in _text(value).casefold()


def _display(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _markdown_cell(value: Any) -> str:
    return _display(value).replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _format_distribution(distribution: Mapping[Any, int]) -> str:
    if not distribution:
        return "—"
    def sort_key(item: tuple[Any, int]) -> tuple[int, float | str]:
        key = item[0]
        if isinstance(key, (int, float)) and not isinstance(key, bool):
            return (0, float(key))
        return (1, str(key).casefold())

    values = sorted(distribution.items(), key=sort_key)
    return ", ".join(f"{_display(key)} × {count}" for key, count in values)


@dataclass(frozen=True)
class ReportMetadata:
    """Common metadata shared by all structured report results."""

    title: str
    report_type: str
    source_record_type: str
    selection_summary: str
    record_count: int
    generated_at: str = field(default_factory=now)
    filters: Mapping[str, str] = field(default_factory=dict)
    template_id: str = ReportTemplateId.STANDARD.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "filters", cast(Mapping[str, str], _freeze_mapping(self.filters)))


@dataclass(frozen=True)
class ScoreStatistics:
    """Descriptive statistics for a numeric score field.

    ``count`` is the number of source records in the section.  ``scored_count``
    is the number with a numeric score.  Missing scores are omitted from every
    numeric calculation rather than being converted to zero.
    """

    count: int = 0
    scored_count: int = 0
    average: float | None = None
    median: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    score_distribution: Mapping[float, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "score_distribution", cast(Mapping[float, int], _freeze_mapping(self.score_distribution)))

    @property
    def average_score(self) -> float | None:
        return self.average

    @property
    def median_score(self) -> float | None:
        return self.median

    @property
    def minimum_score(self) -> float | None:
        return self.minimum

    @property
    def maximum_score(self) -> float | None:
        return self.maximum


def _score_statistics(scores: Sequence[float | None], count: int | None = None) -> ScoreStatistics:
    source_count = len(scores) if count is None else count
    summary = numeric_summary(scores, total_count=source_count)
    numeric = [float(value) for value in scores if value is not None]
    distribution = Counter(numeric)
    return ScoreStatistics(
        count=summary.total_count,
        scored_count=summary.available_count,
        average=summary.mean,
        median=summary.median,
        minimum=summary.minimum,
        maximum=summary.maximum,
        score_distribution=distribution,
    )


@dataclass(frozen=True)
class BenchmarkReportFilters:
    """Optional selection criteria for detailed runs and leaderboards."""

    benchmark_type: str = ""
    benchmark: str = ""
    session_id: int | None = None
    session: str = ""
    hardware_profile_id: int | None = None
    hardware: str = ""
    model: str = ""
    include_run_ids: frozenset[int] = frozenset()
    exclude_run_ids: frozenset[int] = frozenset()
    include_deleted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "include_run_ids", frozenset(self.include_run_ids))
        object.__setattr__(self, "exclude_run_ids", frozenset(self.exclude_run_ids))


@dataclass(frozen=True)
class ScoreboardReportFilters:
    """Optional selection criteria for historical scoreboard entries."""

    batch_id: int | None = None
    model: str = ""
    include_deleted: bool = False


@dataclass(frozen=True)
class BenchmarkRunAggregate:
    """A safely isolated run, review, session, and attachment aggregate."""

    run: BenchmarkRun
    score: ReviewScore | None = None
    session: BenchmarkSession | None = None
    attachments: tuple[RunAttachment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "run", deepcopy(self.run))
        object.__setattr__(self, "score", deepcopy(self.score))
        object.__setattr__(self, "session", deepcopy(self.session))
        object.__setattr__(self, "attachments", tuple(deepcopy(item) for item in self.attachments))

    @property
    def review_score(self) -> ReviewScore | None:
        return self.score


@dataclass(frozen=True)
class ScoreboardEntryAggregate:
    """A safely isolated historical entry and optional import batch."""

    entry: ScoreboardEntry
    batch: ScoreboardImportBatch | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry", deepcopy(self.entry))
        object.__setattr__(self, "batch", deepcopy(self.batch))

    @property
    def model_name(self) -> str:
        return self.entry.model_name

    @property
    def score(self) -> float | None:
        return self.entry.score


@dataclass(frozen=True)
class ModelReportSummary:
    name: str = ""
    model_name: str = ""
    model_family: str = ""
    model_size: str = ""
    quantization: str = ""
    backend: str = ""
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    thinking_enabled: bool | None = None
    flash_attention: bool | None = None
    moe_experts: str = ""
    context_length: int | None = None
    tokens_per_second: float | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", _freeze_mapping(self.extra))

    @property
    def display_name(self) -> str:
        return self.model_name or self.name or "Unknown model"


@dataclass(frozen=True)
class BenchmarkReportSummary:
    name: str = ""
    file_path: str = ""
    benchmark_type: str = ""
    tags: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", _freeze_mapping(self.extra))

    @property
    def benchmark_file(self) -> str:
        return self.file_path


@dataclass(frozen=True)
class PromptReportSummary:
    name: str = ""
    version: str = ""
    prompt_hash: str = ""
    benchmark_type: str = ""
    notes: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", _freeze_mapping(self.extra))


@dataclass(frozen=True)
class SessionReportSummary:
    id: int | None = None
    title: str = ""
    description: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    notes: str = ""


@dataclass(frozen=True)
class HardwareReportSummary:
    name: str = ""
    computer_name: str = ""
    cpu: str = ""
    gpu: str = ""
    vram_gb: float | None = None
    ram_gb: float | None = None
    operating_system: str = ""
    backend_versions: Mapping[str, str] = field(default_factory=dict)
    notes: str = ""
    import_source: str = ""
    imported_at: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend_versions", cast(Mapping[str, str], _freeze_mapping(self.backend_versions)))
        object.__setattr__(self, "extra", _freeze_mapping(self.extra))


@dataclass(frozen=True)
class ReviewScoreSummary:
    accuracy_score: float | None = None
    hallucination_level: str = ""
    reliability_level: str = ""
    depth_score: float | None = None
    signal_noise_score: float | None = None
    actionability_score: float | None = None
    seniority_score: float | None = None
    overall_score: float | None = None
    strengths: str = ""
    weaknesses: str = ""
    verdict: str = ""
    notes: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class AttachmentReportMetadata:
    attachment_id: int | None
    attachment_type: str
    file_path: str
    original_filename: str
    notes: str
    created_at: str


@dataclass(frozen=True)
class BenchmarkRunReportItem:
    run_id: int | None
    created_at: str
    model: ModelReportSummary
    benchmark: BenchmarkReportSummary
    prompt: PromptReportSummary
    session: SessionReportSummary | None
    hardware: HardwareReportSummary | None
    review: ReviewScoreSummary | None
    prompt_text: str | None = None
    raw_model_output: str | None = None
    attachments: tuple[AttachmentReportMetadata, ...] = ()

    @property
    def model_name(self) -> str:
        return self.model.display_name

    @property
    def benchmark_name(self) -> str:
        return self.benchmark.name or self.benchmark.file_path or "Custom benchmark"

    @property
    def benchmark_file(self) -> str:
        return self.benchmark.file_path

    @property
    def recorded_at(self) -> str:
        return self.created_at

    @property
    def review_score(self) -> ReviewScoreSummary | None:
        return self.review


@dataclass(frozen=True)
class BenchmarkModelSection:
    model_name: str
    records: tuple[BenchmarkRunReportItem, ...]
    summary: ScoreStatistics


@dataclass(frozen=True)
class BenchmarkRunReport:
    metadata: ReportMetadata
    summary: ScoreStatistics
    records: tuple[BenchmarkRunReportItem, ...]
    model_sections: tuple[BenchmarkModelSection, ...]

    @property
    def grouped_models(self) -> Mapping[str, tuple[BenchmarkRunReportItem, ...]]:
        return MappingProxyType({section.model_name: section.records for section in self.model_sections})


@dataclass(frozen=True)
class SessionReport:
    """Structured report for one benchmark session and its eligible runs."""

    metadata: ReportMetadata
    session: SessionReportSummary
    summary: ScoreStatistics
    records: tuple[BenchmarkRunReportItem, ...]
    represented_models: tuple[str, ...] = ()
    represented_benchmarks: tuple[str, ...] = ()
    represented_hardware: tuple[str, ...] = ()
    average_tokens_per_second: float | None = None
    hallucination_distribution: Mapping[str, int] = field(default_factory=dict)
    reliability_distribution: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "hallucination_distribution", cast(Mapping[str, int], _freeze_mapping(self.hallucination_distribution)))
        object.__setattr__(self, "reliability_distribution", cast(Mapping[str, int], _freeze_mapping(self.reliability_distribution)))

    @property
    def run_count(self) -> int:
        return self.summary.count

    @property
    def session_id(self) -> int | None:
        return self.session.id

    @property
    def scored_run_count(self) -> int:
        return self.summary.scored_count

    @property
    def average_overall_score(self) -> float | None:
        return self.summary.average

    @property
    def median_overall_score(self) -> float | None:
        return self.summary.median

    @property
    def minimum_overall_score(self) -> float | None:
        return self.summary.minimum

    @property
    def maximum_overall_score(self) -> float | None:
        return self.summary.maximum

    @property
    def score_distribution(self) -> Mapping[float, int]:
        return self.summary.score_distribution


@dataclass(frozen=True)
class HardwareReportGroup:
    """Aggregated runs sharing one normalized historical hardware snapshot."""

    key: str
    hardware: HardwareReportSummary | None
    records: tuple[BenchmarkRunReportItem, ...]
    summary: ScoreStatistics
    represented_models: tuple[str, ...] = ()
    represented_benchmarks: tuple[str, ...] = ()
    average_tokens_per_second: float | None = None
    hallucination_distribution: Mapping[str, int] = field(default_factory=dict)
    reliability_distribution: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "hallucination_distribution", cast(Mapping[str, int], _freeze_mapping(self.hallucination_distribution)))
        object.__setattr__(self, "reliability_distribution", cast(Mapping[str, int], _freeze_mapping(self.reliability_distribution)))

    @property
    def label(self) -> str:
        return _hardware_label(self.hardware)

    @property
    def hardware_summary(self) -> HardwareReportSummary | None:
        return self.hardware

    @property
    def run_count(self) -> int:
        return self.summary.count

    @property
    def scored_run_count(self) -> int:
        return self.summary.scored_count

    @property
    def average_overall_score(self) -> float | None:
        return self.summary.average

    @property
    def median_overall_score(self) -> float | None:
        return self.summary.median

    @property
    def minimum_overall_score(self) -> float | None:
        return self.summary.minimum

    @property
    def maximum_overall_score(self) -> float | None:
        return self.summary.maximum

    @property
    def score_distribution(self) -> Mapping[float, int]:
        return self.summary.score_distribution


@dataclass(frozen=True)
class HardwareReport:
    """Structured report grouped by authoritative historical hardware details."""

    metadata: ReportMetadata
    summary: ScoreStatistics
    groups: tuple[HardwareReportGroup, ...]
    include_hardware_details: bool = False

    @property
    def represented_models(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(model for group in self.groups for model in group.represented_models))

    @property
    def represented_benchmarks(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(benchmark for group in self.groups for benchmark in group.represented_benchmarks))

    @property
    def represented_hardware(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(group.label for group in self.groups))

    @property
    def hardware_groups(self) -> tuple[HardwareReportGroup, ...]:
        return self.groups

    @property
    def fastest_group(self) -> HardwareReportGroup | None:
        candidates = [group for group in self.groups if group.average_tokens_per_second is not None]
        return min(
            candidates,
            key=lambda group: (-(group.average_tokens_per_second or 0.0), group.label.casefold(), group.label),
            default=None,
        )

    @property
    def highest_average_score_group(self) -> HardwareReportGroup | None:
        candidates = [group for group in self.groups if group.summary.average is not None]
        return min(
            candidates,
            key=lambda group: (-(group.summary.average or 0.0), group.label.casefold(), group.label),
            default=None,
        )


@dataclass(frozen=True)
class ScoreboardBatchSection:
    batch: ScoreboardImportBatch | None
    entries: tuple[ScoreboardEntryAggregate, ...]
    summary: ScoreStatistics

    @property
    def label(self) -> str:
        return self.batch.name if self.batch else "Unbatched scoreboard entries"


@dataclass(frozen=True)
class ScoreboardReport:
    metadata: ReportMetadata
    summary: ScoreStatistics
    entries: tuple[ScoreboardEntryAggregate, ...]
    batch_sections: tuple[ScoreboardBatchSection, ...]


@dataclass(frozen=True)
class ModelLeaderboardEntry:
    model_name: str
    run_count: int
    scored_run_count: int
    average_overall_score: float | None
    median_overall_score: float | None
    minimum_overall_score: float | None
    maximum_overall_score: float | None
    score_distribution: Mapping[float, int]
    average_tokens_per_second: float | None
    hallucination_distribution: Mapping[str, int]
    reliability_distribution: Mapping[str, int]
    rank: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "score_distribution", cast(Mapping[float, int], _freeze_mapping(self.score_distribution)))
        object.__setattr__(self, "hallucination_distribution", cast(Mapping[str, int], _freeze_mapping(self.hallucination_distribution)))
        object.__setattr__(self, "reliability_distribution", cast(Mapping[str, int], _freeze_mapping(self.reliability_distribution)))

    @property
    def summary(self) -> ScoreStatistics:
        return ScoreStatistics(
            count=self.run_count,
            scored_count=self.scored_run_count,
            average=self.average_overall_score,
            median=self.median_overall_score,
            minimum=self.minimum_overall_score,
            maximum=self.maximum_overall_score,
            score_distribution=self.score_distribution,
        )


@dataclass(frozen=True)
class ModelLeaderboardReport:
    metadata: ReportMetadata
    models: tuple[ModelLeaderboardEntry, ...]

    @property
    def entries(self) -> tuple[ModelLeaderboardEntry, ...]:
        return self.models


@dataclass(frozen=True)
class ReportWriteResult:
    status: ReportWriteStatus
    path: Path
    message: str = ""
    details: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status is ReportWriteStatus.SUCCESS


ReportDocument: TypeAlias = (
    BenchmarkRunReport
    | ScoreboardReport
    | ModelLeaderboardReport
    | SessionReport
    | HardwareReport
    | ModelComparisonResult
    | SessionComparisonResult
    | TrendReport
)
BenchmarkSource: TypeAlias = BenchmarkRun | BenchmarkRunAggregate
ScoreboardSource: TypeAlias = ScoreboardEntry | ScoreboardEntryAggregate


def _model_summary(snapshot: Mapping[str, Any]) -> ModelReportSummary:
    known = {
        "name", "model_name", "model_family", "model_size", "quantization", "backend",
        "temperature", "top_p", "top_k", "min_p", "thinking_enabled", "flash_attention",
        "moe_experts", "context_length", "tokens_per_second",
    }
    return ModelReportSummary(
        name=_text(snapshot.get("name")),
        model_name=_text(snapshot.get("model_name")),
        model_family=_text(snapshot.get("model_family")),
        model_size=_text(snapshot.get("model_size")),
        quantization=_text(snapshot.get("quantization")),
        backend=_text(snapshot.get("backend")),
        temperature=_float(snapshot.get("temperature")),
        top_p=_float(snapshot.get("top_p")),
        top_k=_int(snapshot.get("top_k")),
        min_p=_float(snapshot.get("min_p")),
        thinking_enabled=_bool(snapshot.get("thinking_enabled")),
        flash_attention=_bool(snapshot.get("flash_attention")),
        moe_experts=_text(snapshot.get("moe_experts")),
        context_length=_int(snapshot.get("context_length")),
        tokens_per_second=_float(snapshot.get("tokens_per_second")),
        extra={key: value for key, value in snapshot.items() if key not in known},
    )


def _benchmark_summary(snapshot: Mapping[str, Any]) -> BenchmarkReportSummary:
    known = {"name", "file_path", "benchmark_file", "benchmark_type", "default_prompt", "tags"}
    return BenchmarkReportSummary(
        name=_text(snapshot.get("name")),
        file_path=_text(_value(snapshot, "file_path", "benchmark_file")),
        benchmark_type=_text(snapshot.get("benchmark_type")),
        tags=_text(snapshot.get("tags")),
        extra={key: value for key, value in snapshot.items() if key not in known},
    )


def _prompt_summary(snapshot: Mapping[str, Any], prompt_name: str = "") -> PromptReportSummary:
    known = {"name", "version", "prompt_text", "prompt_hash", "benchmark_type", "notes"}
    return PromptReportSummary(
        name=_text(_value(snapshot, "name", default=prompt_name)),
        version=_text(snapshot.get("version")),
        prompt_hash=_text(snapshot.get("prompt_hash")),
        benchmark_type=_text(snapshot.get("benchmark_type")),
        notes=_text(snapshot.get("notes")),
        extra={key: value for key, value in snapshot.items() if key not in known},
    )


def _session_summary(session: BenchmarkSession | None, run_session_id: int | None) -> SessionReportSummary | None:
    if session is None and run_session_id is None:
        return None
    if session is None:
        return SessionReportSummary(id=run_session_id)
    return SessionReportSummary(
        id=session.id,
        title=session.title,
        description=session.description,
        started_at=session.started_at,
        completed_at=session.completed_at,
        notes=session.notes,
    )


def _hardware_summary(snapshot: Mapping[str, Any]) -> HardwareReportSummary | None:
    if not snapshot:
        return None
    known = {
        "name", "computer_name", "cpu", "gpu", "vram_gb", "ram_gb", "operating_system",
        "backend_versions", "notes", "import_source", "imported_at",
    }
    versions = snapshot.get("backend_versions")
    return HardwareReportSummary(
        name=_text(snapshot.get("name")),
        computer_name=_text(snapshot.get("computer_name")),
        cpu=_text(snapshot.get("cpu")),
        gpu=_text(snapshot.get("gpu")),
        vram_gb=_float(snapshot.get("vram_gb")),
        ram_gb=_float(snapshot.get("ram_gb")),
        operating_system=_text(snapshot.get("operating_system")),
        backend_versions={str(key): _text(value) for key, value in versions.items()} if isinstance(versions, Mapping) else {},
        notes=_text(snapshot.get("notes")),
        import_source=_text(snapshot.get("import_source")),
        imported_at=_text(snapshot.get("imported_at"), default="") or None,
        extra={key: value for key, value in snapshot.items() if key not in known},
    )


def _normalized_snapshot_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return ",".join(
            f"{str(key).strip().casefold()}={_normalized_snapshot_value(item)}"
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]).casefold())
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return ",".join(sorted(_normalized_snapshot_value(item) for item in value))
    return " ".join(_text(value).strip().casefold().split())


def _hardware_group_key(snapshot: Mapping[str, Any]) -> str:
    """Create a deterministic key from a run's historical snapshot only."""

    if not snapshot:
        return "unknown"
    return "|".join(
        f"{str(key).strip().casefold()}={_normalized_snapshot_value(value)}"
        for key, value in sorted(snapshot.items(), key=lambda pair: str(pair[0]).casefold())
    ) or "unknown"


def _hardware_label(hardware: HardwareReportSummary | None) -> str:
    if hardware is None:
        return "Unknown hardware"
    values = [
        hardware.name,
        hardware.computer_name,
        hardware.cpu,
        hardware.gpu,
        f"{_display(hardware.vram_gb)} GB VRAM" if hardware.vram_gb is not None else "",
        f"{_display(hardware.ram_gb)} GB RAM" if hardware.ram_gb is not None else "",
        hardware.operating_system,
    ]
    label = ", ".join(value for value in values if value)
    if label:
        return label
    if hardware.backend_versions:
        return "Backend versions: " + ", ".join(
            f"{key}={value}" for key, value in sorted(hardware.backend_versions.items(), key=lambda pair: pair[0].casefold())
        )
    return "Unknown hardware"


def normalize_hardware_snapshot(snapshot: Mapping[str, Any]) -> str:
    """Return the stable grouping key used for historical hardware snapshots."""

    return _hardware_group_key(snapshot)


def hardware_snapshot_label(snapshot: Mapping[str, Any]) -> str:
    """Return the human-readable label used for a historical hardware snapshot."""

    return _hardware_label(_hardware_summary(snapshot))


def _record_scores(records: Sequence[BenchmarkRunReportItem]) -> list[float | None]:
    return [item.review.overall_score if item.review else None for item in records]


def _record_speeds(records: Sequence[BenchmarkRunReportItem]) -> list[float]:
    return [
        speed
        for item in records
        if (speed := item.model.tokens_per_second) is not None
    ]


def _record_hallucination_distribution(records: Sequence[BenchmarkRunReportItem]) -> Mapping[str, int]:
    return Counter(
        item.review.hallucination_level
        for item in records
        if item.review and item.review.hallucination_level
    )


def _record_reliability_distribution(records: Sequence[BenchmarkRunReportItem]) -> Mapping[str, int]:
    return Counter(
        item.review.reliability_level
        for item in records
        if item.review and item.review.reliability_level
    )


def _average_values(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _review_summary(score: ReviewScore | None) -> ReviewScoreSummary | None:
    if score is None:
        return None
    return ReviewScoreSummary(
        accuracy_score=score.accuracy_score,
        hallucination_level=score.hallucination_level,
        reliability_level=score.reliability_level,
        depth_score=score.depth_score,
        signal_noise_score=score.signal_noise_score,
        actionability_score=score.actionability_score,
        seniority_score=score.seniority_score,
        overall_score=score.overall_score,
        strengths=score.strengths,
        weaknesses=score.weaknesses,
        verdict=score.verdict,
        notes=score.notes,
        created_at=score.created_at,
    )


def _aggregate_item(
    aggregate: BenchmarkRunAggregate,
    *,
    include_prompt_text: bool,
    include_raw_model_output: bool,
    include_attachment_metadata: bool,
) -> BenchmarkRunReportItem:
    run = aggregate.run
    prompt_text = run.prompt_text or _text(run.prompt_snapshot.get("prompt_text"))
    attachments = tuple(
        AttachmentReportMetadata(
            attachment_id=attachment.id,
            attachment_type=attachment.attachment_type,
            file_path=attachment.file_path,
            original_filename=attachment.original_filename,
            notes=attachment.notes,
            created_at=attachment.created_at,
        )
        for attachment in aggregate.attachments
    ) if include_attachment_metadata else ()
    return BenchmarkRunReportItem(
        run_id=run.id,
        created_at=run.created_at,
        model=_model_summary(run.model_snapshot),
        benchmark=_benchmark_summary(run.benchmark_snapshot),
        prompt=_prompt_summary(run.prompt_snapshot, run.prompt_name),
        session=_session_summary(aggregate.session, run.session_id),
        hardware=_hardware_summary(run.hardware_snapshot),
        review=_review_summary(aggregate.score),
        prompt_text=prompt_text if include_prompt_text else None,
        raw_model_output=run.raw_model_output if include_raw_model_output else None,
        attachments=attachments,
    )


def _filter_mapping(filters: BenchmarkReportFilters | ScoreboardReportFilters) -> Mapping[str, str]:
    values: dict[str, str] = {}
    if isinstance(filters, BenchmarkReportFilters):
        for name in ("benchmark_type", "benchmark", "session", "hardware", "model"):
            value = getattr(filters, name)
            if value:
                values[name] = value
        for name in ("session_id", "hardware_profile_id"):
            value = getattr(filters, name)
            if value is not None:
                values[name] = str(value)
        if filters.include_run_ids:
            values["include_run_ids"] = ",".join(str(value) for value in sorted(filters.include_run_ids))
        if filters.exclude_run_ids:
            values["exclude_run_ids"] = ",".join(str(value) for value in sorted(filters.exclude_run_ids))
        if filters.include_deleted:
            values["include_deleted"] = "true"
    else:
        if filters.batch_id is not None:
            values["batch_id"] = str(filters.batch_id)
        if filters.model:
            values["model"] = filters.model
        if filters.include_deleted:
            values["include_deleted"] = "true"
    return values


def _selection_summary(filters: BenchmarkReportFilters | ScoreboardReportFilters) -> str:
    if not _filter_mapping(filters):
        return "All non-deleted records"
    return "; ".join(f"{key}={value}" for key, value in _filter_mapping(filters).items())


def _run_matches(aggregate: BenchmarkRunAggregate, filters: BenchmarkReportFilters) -> bool:
    run = aggregate.run
    if run.is_deleted and not filters.include_deleted:
        return False
    if filters.include_run_ids and run.id not in filters.include_run_ids:
        return False
    if run.id in filters.exclude_run_ids:
        return False
    benchmark = run.benchmark_snapshot
    if filters.benchmark_type and not _contains(benchmark.get("benchmark_type"), filters.benchmark_type):
        return False
    if filters.benchmark and not any(
        _contains(benchmark.get(key), filters.benchmark)
        for key in ("name", "file_path", "benchmark_file")
    ):
        return False
    if filters.session_id is not None and run.session_id != filters.session_id:
        return False
    if filters.session:
        session_text = " ".join(
            (_text(aggregate.session.title) if aggregate.session else "", _text(run.session_id))
        )
        if not _contains(session_text, filters.session):
            return False
    if filters.hardware_profile_id is not None and run.hardware_profile_id != filters.hardware_profile_id:
        return False
    if filters.hardware:
        hardware_text = " ".join(_text(value) for value in run.hardware_snapshot.values())
        if not (_contains(hardware_text, filters.hardware) or _contains(_hardware_group_key(run.hardware_snapshot), filters.hardware)):
            return False
    if filters.model and not _contains(run.model_snapshot.get("model_name") or run.model_snapshot.get("name"), filters.model):
        return False
    return True


def _scoreboard_matches(aggregate: ScoreboardEntryAggregate, filters: ScoreboardReportFilters) -> bool:
    if aggregate.entry.is_deleted and not filters.include_deleted:
        return False
    if aggregate.batch and aggregate.batch.is_deleted and not filters.include_deleted:
        return False
    if filters.batch_id is not None and aggregate.entry.import_batch_id != filters.batch_id:
        return False
    return not filters.model or _contains(aggregate.entry.model_name, filters.model)


def _coerce_run_aggregate(value: BenchmarkSource, service: BenchmarkService | None, catalog: CatalogService | None) -> BenchmarkRunAggregate:
    if isinstance(value, BenchmarkRunAggregate):
        return value
    score: ReviewScore | None = None
    attachments: tuple[RunAttachment, ...] = ()
    session: BenchmarkSession | None = None
    if service is not None and value.id is not None:
        _, score, loaded = service.get_run(value.id)
        attachments = tuple(loaded)
        session = (catalog or service.catalog).sessions.get(value.session_id) if value.session_id else None
    return BenchmarkRunAggregate(value, score, session, attachments)


def _coerce_scoreboard_aggregate(
    value: ScoreboardSource,
    batches: Mapping[int | None, ScoreboardImportBatch],
) -> ScoreboardEntryAggregate:
    if isinstance(value, ScoreboardEntryAggregate):
        if value.batch is None and value.entry.import_batch_id in batches:
            return ScoreboardEntryAggregate(value.entry, batches.get(value.entry.import_batch_id))
        return value
    return ScoreboardEntryAggregate(value, batches.get(value.import_batch_id))


def _run_source(
    runs: Sequence[BenchmarkSource] | None,
    service: BenchmarkService | None,
    catalog: CatalogService | None,
) -> tuple[BenchmarkRunAggregate, ...]:
    if runs is None:
        if service is None:
            return ()
        runs = service.runs.list(include_deleted=True)
    return tuple(_coerce_run_aggregate(run, service, catalog) for run in runs)


def _scoreboard_source(
    entries: Sequence[ScoreboardSource] | None,
    batches: Sequence[ScoreboardImportBatch] | None,
    catalog: CatalogService | None,
) -> tuple[ScoreboardEntryAggregate, ...]:
    if entries is None:
        entries = catalog.scoreboard_entries.list(include_deleted=True) if catalog else ()
    if batches is None:
        batches = catalog.scoreboard_import_batches.list(include_deleted=True) if catalog else ()
    batch_map = {batch.id: batch for batch in batches}
    return tuple(_coerce_scoreboard_aggregate(entry, batch_map) for entry in entries)


def build_benchmark_run_report(
    runs: Sequence[BenchmarkSource] | None = None,
    *,
    service: BenchmarkService | None = None,
    catalog: CatalogService | None = None,
    filters: BenchmarkReportFilters = BenchmarkReportFilters(),
    include_prompt_text: bool = False,
    include_raw_model_output: bool = False,
    include_attachment_metadata: bool = False,
    title: str = "Benchmark Run Report",
    generated_at: str | None = None,
    template_options: ReportTemplateOptions | None = None,
) -> BenchmarkRunReport:
    options = template_options or ReportTemplateOptions(
        template_id="standard",
        include_prompt_text=include_prompt_text,
        include_raw_model_output=include_raw_model_output,
        include_attachment_metadata=include_attachment_metadata,
    )
    selected = [
        aggregate for aggregate in _run_source(runs, service, catalog)
        if _run_matches(aggregate, filters)
    ]
    records = tuple(
        sorted(
            (
                _aggregate_item(
                    aggregate,
                    include_prompt_text=options.include_prompt_text,
                    include_raw_model_output=options.include_raw_model_output,
                    include_attachment_metadata=options.include_attachment_metadata,
                )
                for aggregate in selected
            ),
            key=lambda item: (item.model_name.casefold(), item.created_at, item.run_id or 0),
        )
    )
    summary = _score_statistics(
        [item.review.overall_score if item.review else None for item in records],
        count=len(records),
    )
    grouped: dict[str, list[BenchmarkRunReportItem]] = {}
    for record in records:
        grouped.setdefault(record.model_name, []).append(record)
    sections = tuple(
        BenchmarkModelSection(
            model_name=model_name,
            records=tuple(model_records),
            summary=_score_statistics(
                [item.review.overall_score if item.review else None for item in model_records],
                count=len(model_records),
            ),
        )
        for model_name, model_records in sorted(grouped.items(), key=lambda item: (item[0].casefold(), item[0]))
    )
    metadata = ReportMetadata(
        title=title,
        report_type=ReportType.BENCHMARK_RUNS.value,
        source_record_type="BenchmarkRun",
        selection_summary=_selection_summary(filters),
        record_count=len(records),
        generated_at=generated_at or now(),
        filters=_filter_mapping(filters),
        template_id=options.template_id,
    )
    return BenchmarkRunReport(metadata, summary, records, sections)


def _resolve_session(
    session: BenchmarkSession | int | None,
    catalog: CatalogService | None,
) -> tuple[BenchmarkSession | None, int | None]:
    if session is None:
        return None, None
    if isinstance(session, BenchmarkSession):
        return deepcopy(session), session.id
    return (catalog.sessions.get(session) if catalog else None), session


def _distinct_sorted(values: Sequence[str]) -> tuple[str, ...]:
    distinct = {value for value in values if value}
    return tuple(sorted(distinct, key=lambda value: (value.casefold(), value)))


def build_session_report(
    session: BenchmarkSession | int | None = None,
    runs: Sequence[BenchmarkSource] | None = None,
    *,
    service: BenchmarkService | None = None,
    catalog: CatalogService | None = None,
    filters: BenchmarkReportFilters = BenchmarkReportFilters(),
    include_prompt_text: bool = False,
    include_raw_model_output: bool = False,
    include_attachment_metadata: bool = False,
    title: str = "Session Report",
    generated_at: str | None = None,
    template_options: ReportTemplateOptions | None = None,
    session_id: int | None = None,
) -> SessionReport:
    """Build one immutable session report from eligible historical runs."""

    options = template_options or ReportTemplateOptions(
        template_id="standard",
        include_prompt_text=include_prompt_text,
        include_raw_model_output=include_raw_model_output,
        include_attachment_metadata=include_attachment_metadata,
    )
    selected_session: BenchmarkSession | int | None = session if session is not None else session_id
    session_record, resolved_session_id = _resolve_session(selected_session, catalog)
    effective_filters = replace(filters, session_id=resolved_session_id) if resolved_session_id is not None else filters
    eligible_session = (
        session_record is not None
        and (not session_record.is_deleted or effective_filters.include_deleted)
    ) or (session_record is None and resolved_session_id is not None and catalog is None)
    selected = [
        aggregate
        for aggregate in _run_source(runs, service, catalog)
        if eligible_session and _run_matches(aggregate, effective_filters)
    ]
    records = tuple(
        sorted(
            (
                _aggregate_item(
                    aggregate,
                    include_prompt_text=options.include_prompt_text,
                    include_raw_model_output=options.include_raw_model_output,
                    include_attachment_metadata=options.include_attachment_metadata,
                )
                for aggregate in selected
            ),
            key=lambda item: (item.created_at, item.run_id or 0, item.model_name.casefold(), item.benchmark_name.casefold()),
        )
    )
    session_summary = _session_summary(session_record, resolved_session_id) or SessionReportSummary(id=resolved_session_id)
    metadata = ReportMetadata(
        title=title,
        report_type=ReportType.SESSION.value,
        source_record_type="BenchmarkRun",
        selection_summary=_selection_summary(effective_filters),
        record_count=len(records),
        generated_at=generated_at or now(),
        filters=_filter_mapping(effective_filters),
        template_id=options.template_id,
    )
    speeds = _record_speeds(records)
    return SessionReport(
        metadata=metadata,
        session=session_summary,
        summary=_score_statistics(_record_scores(records), count=len(records)),
        records=records,
        represented_models=_distinct_sorted([item.model_name for item in records]),
        represented_benchmarks=_distinct_sorted([item.benchmark_name for item in records]),
        represented_hardware=_distinct_sorted([_hardware_label(item.hardware) for item in records]),
        average_tokens_per_second=_average_values(speeds),
        hallucination_distribution=_record_hallucination_distribution(records),
        reliability_distribution=_record_reliability_distribution(records),
    )


def build_hardware_report(
    runs: Sequence[BenchmarkSource] | None = None,
    *,
    service: BenchmarkService | None = None,
    catalog: CatalogService | None = None,
    filters: BenchmarkReportFilters = BenchmarkReportFilters(),
    include_prompt_text: bool = False,
    include_raw_model_output: bool = False,
    include_attachment_metadata: bool = False,
    include_hardware_details: bool = False,
    title: str = "Hardware Report",
    generated_at: str | None = None,
    template_options: ReportTemplateOptions | None = None,
) -> HardwareReport:
    """Build deterministic hardware groups from authoritative run snapshots."""

    options = template_options or ReportTemplateOptions(
        template_id="standard",
        include_prompt_text=include_prompt_text,
        include_raw_model_output=include_raw_model_output,
        include_attachment_metadata=include_attachment_metadata,
        include_hardware_details=include_hardware_details,
    )
    selected = [
        aggregate for aggregate in _run_source(runs, service, catalog)
        if _run_matches(aggregate, filters)
    ]
    records_with_keys = [
        (
            _hardware_group_key(aggregate.run.hardware_snapshot),
            _aggregate_item(
                aggregate,
                include_prompt_text=options.include_prompt_text,
                include_raw_model_output=options.include_raw_model_output,
                include_attachment_metadata=options.include_attachment_metadata,
            ),
        )
        for aggregate in selected
    ]
    records_with_keys.sort(
        key=lambda item: (item[1].created_at, item[1].run_id or 0, item[1].model_name.casefold(), item[1].benchmark_name.casefold())
    )
    grouped: dict[str, list[BenchmarkRunReportItem]] = {}
    for key, record in records_with_keys:
        grouped.setdefault(key, []).append(record)
    groups: list[HardwareReportGroup] = []
    for key, group_records in grouped.items():
        hardware = group_records[0].hardware
        groups.append(
            HardwareReportGroup(
                key=key,
                hardware=hardware,
                records=tuple(group_records),
                summary=_score_statistics(_record_scores(group_records), count=len(group_records)),
                represented_models=_distinct_sorted([item.model_name for item in group_records]),
                represented_benchmarks=_distinct_sorted([item.benchmark_name for item in group_records]),
                average_tokens_per_second=_average_values(_record_speeds(group_records)),
                hallucination_distribution=_record_hallucination_distribution(group_records),
                reliability_distribution=_record_reliability_distribution(group_records),
            )
        )
    ordered_groups = tuple(sorted(groups, key=lambda group: (group.label.casefold(), group.label, group.key)))
    all_records = tuple(record for group in ordered_groups for record in group.records)
    metadata = ReportMetadata(
        title=title,
        report_type=ReportType.HARDWARE.value,
        source_record_type="BenchmarkRun",
        selection_summary=_selection_summary(filters),
        record_count=len(all_records),
        generated_at=generated_at or now(),
        filters=_filter_mapping(filters),
        template_id=options.template_id,
    )
    return HardwareReport(
        metadata=metadata,
        summary=_score_statistics(_record_scores(all_records), count=len(all_records)),
        groups=ordered_groups,
        include_hardware_details=options.include_hardware_details,
    )


def build_scoreboard_report(
    entries: Sequence[ScoreboardSource] | None = None,
    *,
    batches: Sequence[ScoreboardImportBatch] | None = None,
    catalog: CatalogService | None = None,
    filters: ScoreboardReportFilters = ScoreboardReportFilters(),
    title: str = "Historical Scoreboard Report",
    generated_at: str | None = None,
    template_options: ReportTemplateOptions | None = None,
) -> ScoreboardReport:
    options = template_options or ReportTemplateOptions()
    selected = [
        aggregate for aggregate in _scoreboard_source(entries, batches, catalog)
        if _scoreboard_matches(aggregate, filters)
    ]
    selected.sort(key=lambda aggregate: (
        aggregate.batch.name.casefold() if aggregate.batch else "~unbatched",
        aggregate.entry.model_name.casefold(),
        aggregate.entry.id or 0,
    ))
    summary = _score_statistics([aggregate.entry.score for aggregate in selected], count=len(selected))
    grouped: dict[int | None, list[ScoreboardEntryAggregate]] = {}
    for aggregate in selected:
        grouped.setdefault(aggregate.entry.import_batch_id, []).append(aggregate)
    batch_lookup = {aggregate.entry.import_batch_id: aggregate.batch for aggregate in selected}

    def batch_sort_key(value: int | None) -> tuple[str, int]:
        batch = batch_lookup.get(value)
        return (batch.name.casefold() if batch is not None else "~unbatched", value or 0)

    sections = tuple(
        ScoreboardBatchSection(
            batch=batch_lookup.get(batch_id),
            entries=tuple(grouped[batch_id]),
            summary=_score_statistics([entry.entry.score for entry in grouped[batch_id]], count=len(grouped[batch_id])),
        )
        for batch_id in sorted(
            grouped,
            key=batch_sort_key,
        )
    )
    metadata = ReportMetadata(
        title=title,
        report_type=ReportType.SCOREBOARD.value,
        source_record_type="ScoreboardEntry",
        selection_summary=_selection_summary(filters),
        record_count=len(selected),
        generated_at=generated_at or now(),
        filters=_filter_mapping(filters),
        template_id=options.template_id,
    )
    return ScoreboardReport(metadata, summary, tuple(selected), sections)


def build_model_leaderboard(
    runs: Sequence[BenchmarkSource] | None = None,
    *,
    service: BenchmarkService | None = None,
    catalog: CatalogService | None = None,
    filters: BenchmarkReportFilters = BenchmarkReportFilters(),
    title: str = "Model Leaderboard",
    generated_at: str | None = None,
    template_options: ReportTemplateOptions | None = None,
) -> ModelLeaderboardReport:
    options = template_options or ReportTemplateOptions()
    selected = [
        aggregate for aggregate in _run_source(runs, service, catalog)
        if _run_matches(aggregate, filters)
    ]
    per_model: dict[str, list[BenchmarkRunAggregate]] = {}
    for aggregate in selected:
        snapshot = aggregate.run.model_snapshot
        model_name = _text(snapshot.get("model_name") or snapshot.get("name"))
        if model_name:
            per_model.setdefault(model_name, []).append(aggregate)

    entries: list[ModelLeaderboardEntry] = []
    for model_name, model_runs in per_model.items():
        scored = [
            aggregate for aggregate in model_runs
            if aggregate.score is not None and aggregate.score.overall_score is not None
        ]
        scores = [aggregate.score.overall_score for aggregate in scored if aggregate.score is not None]
        speeds = [
            speed for aggregate in scored
            if (speed := _float(aggregate.run.model_snapshot.get("tokens_per_second"))) is not None
        ]
        hallucination = Counter(
            aggregate.score.hallucination_level
            for aggregate in scored
            if aggregate.score is not None and aggregate.score.hallucination_level
        )
        reliability = Counter(
            aggregate.score.reliability_level
            for aggregate in scored
            if aggregate.score is not None and aggregate.score.reliability_level
        )
        stats = _score_statistics(scores, count=len(model_runs))
        entries.append(
            ModelLeaderboardEntry(
                model_name=model_name,
                run_count=len(model_runs),
                scored_run_count=len(scores),
                average_overall_score=stats.average,
                median_overall_score=stats.median,
                minimum_overall_score=stats.minimum,
                maximum_overall_score=stats.maximum,
                score_distribution=stats.score_distribution,
                average_tokens_per_second=sum(speeds) / len(speeds) if speeds else None,
                hallucination_distribution=hallucination,
                reliability_distribution=reliability,
            )
        )

    def ranking_key(entry: ModelLeaderboardEntry) -> tuple[Any, ...]:
        # Ranking is deterministic: average score descending, then evidence
        # count descending, then median descending, then case-insensitive model
        # name ascending and original name ascending as a final stable tie-break.
        return (
            entry.average_overall_score is None,
            -(entry.average_overall_score or 0.0),
            -entry.scored_run_count,
            entry.median_overall_score is None,
            -(entry.median_overall_score or 0.0),
            entry.model_name.casefold(),
            entry.model_name,
        )

    ranked = tuple(replace(entry, rank=rank) for rank, entry in enumerate(sorted(entries, key=ranking_key), start=1))
    metadata = ReportMetadata(
        title=title,
        report_type=ReportType.MODEL_LEADERBOARD.value,
        source_record_type="BenchmarkRun",
        selection_summary=_selection_summary(filters),
        record_count=sum(entry.run_count for entry in ranked),
        generated_at=generated_at or now(),
        filters=_filter_mapping(filters),
        template_id=options.template_id,
    )
    return ModelLeaderboardReport(metadata, ranked)


class ReportingService:
    """Application-facing facade over the UI-independent reporting engine."""

    def __init__(self, service: BenchmarkService, catalog: CatalogService | None = None):
        self.service = service
        self.catalog = catalog or service.catalog

    def report_templates(self) -> tuple[ReportTemplate, ...]:
        return available_report_templates()

    def report_template(self, template: str | ReportTemplateId) -> ReportTemplate | None:
        return get_report_template(template)

    def apply_template(self, template: str | ReportTemplateId) -> ReportTemplateOptions:
        return apply_report_template(template)

    def select_benchmark_runs(
        self,
        runs: Sequence[BenchmarkSource] | None = None,
        *,
        filters: BenchmarkReportFilters | None = None,
    ) -> tuple[BenchmarkRunAggregate, ...]:
        source = _run_source(runs, self.service, self.catalog)
        if filters is None:
            return source
        return tuple(aggregate for aggregate in source if _run_matches(aggregate, filters))

    def select_scoreboard_entries(
        self,
        entries: Sequence[ScoreboardSource] | None = None,
        *,
        batches: Sequence[ScoreboardImportBatch] | None = None,
        filters: ScoreboardReportFilters | None = None,
    ) -> tuple[ScoreboardEntryAggregate, ...]:
        source = _scoreboard_source(entries, batches, self.catalog)
        if filters is None:
            return source
        return tuple(aggregate for aggregate in source if _scoreboard_matches(aggregate, filters))

    def benchmark_run_report(
        self,
        runs: Sequence[BenchmarkSource] | None = None,
        *,
        filters: BenchmarkReportFilters = BenchmarkReportFilters(),
        include_prompt_text: bool = False,
        include_raw_model_output: bool = False,
        include_attachment_metadata: bool = False,
        title: str = "Benchmark Run Report",
        generated_at: str | None = None,
        template_options: ReportTemplateOptions | None = None,
    ) -> BenchmarkRunReport:
        return build_benchmark_run_report(
            runs,
            service=self.service,
            catalog=self.catalog,
            filters=filters,
            include_prompt_text=include_prompt_text,
            include_raw_model_output=include_raw_model_output,
            include_attachment_metadata=include_attachment_metadata,
            title=title,
            generated_at=generated_at,
            template_options=template_options,
        )

    def session_report(
        self,
        session: BenchmarkSession | int | None = None,
        runs: Sequence[BenchmarkSource] | None = None,
        *,
        filters: BenchmarkReportFilters = BenchmarkReportFilters(),
        include_prompt_text: bool = False,
        include_raw_model_output: bool = False,
        include_attachment_metadata: bool = False,
        title: str = "Session Report",
        generated_at: str | None = None,
        template_options: ReportTemplateOptions | None = None,
        session_id: int | None = None,
    ) -> SessionReport:
        return build_session_report(
            session,
            runs,
            service=self.service,
            catalog=self.catalog,
            filters=filters,
            include_prompt_text=include_prompt_text,
            include_raw_model_output=include_raw_model_output,
            include_attachment_metadata=include_attachment_metadata,
            title=title,
            generated_at=generated_at,
            template_options=template_options,
            session_id=session_id,
        )

    def hardware_report(
        self,
        runs: Sequence[BenchmarkSource] | None = None,
        *,
        filters: BenchmarkReportFilters = BenchmarkReportFilters(),
        include_prompt_text: bool = False,
        include_raw_model_output: bool = False,
        include_attachment_metadata: bool = False,
        include_hardware_details: bool = False,
        title: str = "Hardware Report",
        generated_at: str | None = None,
        template_options: ReportTemplateOptions | None = None,
    ) -> HardwareReport:
        return build_hardware_report(
            runs,
            service=self.service,
            catalog=self.catalog,
            filters=filters,
            include_prompt_text=include_prompt_text,
            include_raw_model_output=include_raw_model_output,
            include_attachment_metadata=include_attachment_metadata,
            include_hardware_details=include_hardware_details,
            title=title,
            generated_at=generated_at,
            template_options=template_options,
        )

    def scoreboard_report(
        self,
        entries: Sequence[ScoreboardSource] | None = None,
        *,
        batches: Sequence[ScoreboardImportBatch] | None = None,
        filters: ScoreboardReportFilters = ScoreboardReportFilters(),
        title: str = "Historical Scoreboard Report",
        generated_at: str | None = None,
        template_options: ReportTemplateOptions | None = None,
    ) -> ScoreboardReport:
        return build_scoreboard_report(
            entries,
            batches=batches,
            catalog=self.catalog,
            filters=filters,
            title=title,
            generated_at=generated_at,
            template_options=template_options,
        )

    def model_leaderboard(
        self,
        runs: Sequence[BenchmarkSource] | None = None,
        *,
        filters: BenchmarkReportFilters = BenchmarkReportFilters(),
        title: str = "Model Leaderboard",
        generated_at: str | None = None,
        template_options: ReportTemplateOptions | None = None,
    ) -> ModelLeaderboardReport:
        return build_model_leaderboard(
            runs,
            service=self.service,
            catalog=self.catalog,
            filters=filters,
            title=title,
            generated_at=generated_at,
            template_options=template_options,
        )

    def render_model_comparison_markdown(self, report: ModelComparisonResult) -> str:
        """Render a typed model comparison without recalculating its metrics."""

        return render_model_comparison_markdown(report)

    def render_session_comparison_markdown(self, report: SessionComparisonResult) -> str:
        """Render a typed session comparison without recalculating its metrics."""

        return render_session_comparison_markdown(report)

    def render_trend_markdown(
        self,
        report: TrendReport,
        *,
        include_series_details: bool = False,
    ) -> str:
        """Render a typed trend without recalculating any trend metrics."""

        return render_trend_markdown(report, include_series_details=include_series_details)

    def render_benchmark_run_trend_markdown(
        self,
        report: TrendReport,
        *,
        include_series_details: bool = False,
    ) -> str:
        return self.render_trend_markdown(report, include_series_details=include_series_details)

    def render_scoreboard_trend_markdown(
        self,
        report: TrendReport,
        *,
        include_series_details: bool = False,
    ) -> str:
        return self.render_trend_markdown(report, include_series_details=include_series_details)

    trend_markdown = render_trend_markdown
    benchmark_run_trend_markdown = render_benchmark_run_trend_markdown
    scoreboard_trend_markdown = render_scoreboard_trend_markdown

    # Short aliases keep the facade convenient for callers that already use
    # ``render_*`` methods for other report families.
    model_comparison_markdown = render_model_comparison_markdown
    session_comparison_markdown = render_session_comparison_markdown

    def write_markdown_report(
        self,
        report: ReportDocument,
        destination: str | Path,
        *,
        overwrite: bool = False,
        include_model_details: bool = False,
        include_hardware_details: bool = False,
        include_series_details: bool | None = None,
        template_options: ReportTemplateOptions | None = None,
    ) -> ReportWriteResult:
        """Render and stage a report through the application-facing facade.

        Template-controlled detail sections are rendering options, so the
        facade accepts them here rather than requiring a UI caller to render
        Markdown itself. The underlying writer still owns UTF-8 staging,
        finalization, and overwrite protection.
        """

        options = template_options or ReportTemplateOptions(
            include_model_details=include_model_details,
            include_hardware_details=include_hardware_details,
        )
        if isinstance(report, TrendReport) and include_series_details is not None:
            options = replace(options, include_record_details=include_series_details)
        return write_markdown_report(report, destination, overwrite=overwrite, template_options=options)

    def html_analytics_report(
        self,
        *,
        options: HtmlAnalyticsReportOptions | None = None,
        **kwargs: Any,
    ) -> HtmlAnalyticsReport:
        """Prepare a typed standalone HTML analytics report."""

        from .html_reporting import HtmlAnalyticsReportOptions, build_html_analytics_report

        return build_html_analytics_report(
            self.service,
            self.catalog,
            options=options or HtmlAnalyticsReportOptions(),
            **kwargs,
        )

    def render_html_analytics_report(self, report: HtmlAnalyticsReport) -> str:
        """Render engine-prepared analytics without recalculating any metric."""

        from .html_reporting import render_html_analytics_report

        return render_html_analytics_report(report)

    def write_html_analytics_report(
        self,
        report: HtmlAnalyticsReport,
        destination: str | Path | None = None,
        *,
        overwrite: bool = False,
    ) -> ReportWriteResult:
        """Stage and atomically finalize a standalone analytics file."""

        from .html_reporting import write_html_analytics_report

        return write_html_analytics_report(report, destination, overwrite=overwrite)

    build_html_analytics_report = html_analytics_report
    render_html_analytics = render_html_analytics_report
    write_html_analytics = write_html_analytics_report


def _render_header(metadata: ReportMetadata) -> list[str]:
    return [
        f"# {metadata.title}",
        "",
        f"- Generated at: {metadata.generated_at}",
        f"- Report type: {metadata.report_type}",
        f"- Source record type: {metadata.source_record_type}",
        f"- Selection: {metadata.selection_summary}",
        f"- Record count: {metadata.record_count}",
        f"- Template: {metadata.template_id}",
    ]


def _render_stats(lines: list[str], stats: ScoreStatistics, heading: str = "Score summary") -> None:
    lines.extend(
        [
            "",
            f"## {heading}",
            "",
            f"- Count: {stats.count}",
            f"- Scored count: {stats.scored_count}",
            f"- Average: {_display(stats.average)}",
            f"- Median: {_display(stats.median)}",
            f"- Minimum: {_display(stats.minimum)}",
            f"- Maximum: {_display(stats.maximum)}",
            f"- Score distribution: {_format_distribution(stats.score_distribution)}",
        ]
    )


def _render_indented_block(lines: list[str], heading: str, value: str | None) -> None:
    if value is None:
        return
    lines.extend(["", f"##### {heading}"])
    block_lines = value.splitlines() or [""]
    lines.extend(f"    {line}" for line in block_lines)


def _render_model_settings(model: ModelReportSummary) -> str:
    values = (
        ("backend", model.backend),
        ("temperature", model.temperature),
        ("top_p", model.top_p),
        ("top_k", model.top_k),
        ("min_p", model.min_p),
        ("thinking_enabled", model.thinking_enabled),
        ("flash_attention", model.flash_attention),
        ("moe_experts", model.moe_experts),
        ("context_length", model.context_length),
        ("tokens_per_second", model.tokens_per_second),
    )
    return ", ".join(f"{name}={_display(value)}" for name, value in values if value not in (None, "")) or "—"


def _render_compact_run_table(lines: list[str], records: Sequence[BenchmarkRunReportItem]) -> None:
    lines.extend(
        [
            "",
            "## Benchmark runs",
            "",
            "| Recorded | Model | Benchmark | Score | Hallucination | Reliability | Tokens/s | Verdict |",
            "| --- | --- | --- | ---: | --- | --- | ---: | --- |",
        ]
    )
    if not records:
        lines.append("| — | No benchmark runs selected | — | — | — | — | — | — |")
    for item in records:
        review = item.review
        lines.append(
            "| " + " | ".join(
                _markdown_cell(value)
                for value in (
                    item.created_at,
                    item.model_name,
                    item.benchmark_name,
                    review.overall_score if review else None,
                    review.hallucination_level if review else None,
                    review.reliability_level if review else None,
                    item.model.tokens_per_second,
                    review.verdict if review else None,
                )
            ) + " |"
        )


def _template_has_sensitive_content(options: ReportTemplateOptions) -> bool:
    return options.include_prompt_text or options.include_raw_model_output or options.include_attachment_metadata


def render_benchmark_run_markdown(
    report: BenchmarkRunReport,
    *,
    template_options: ReportTemplateOptions | None = None,
) -> str:
    if (
        template_options is not None
        and not template_options.include_record_details
        and not _template_has_sensitive_content(template_options)
    ):
        lines = _render_header(report.metadata)
        _render_stats(lines, report.summary, "Overall score summary")
        _render_compact_run_table(lines, report.records)
        return "\n".join(lines).rstrip() + "\n"
    lines = _render_header(report.metadata)
    _render_stats(lines, report.summary, "Overall score summary")
    lines.extend(["", "## Benchmark runs", ""])
    if not report.records:
        lines.append("No benchmark runs selected.")
    for section in report.model_sections:
        lines.extend([f"### Model: {section.model_name}", ""])
        lines.extend(
            [
                f"- Run count: {section.summary.count}",
                f"- Scored count: {section.summary.scored_count}",
                f"- Average overall score: {_display(section.summary.average)}",
                "",
            ]
        )
        for item in section.records:
            lines.extend([f"#### {item.benchmark_name}", ""])
            lines.extend(
                [
                    f"- Run ID: {_display(item.run_id)}",
                    f"- Benchmark file: {_display(item.benchmark_file)}",
                    f"- Benchmark type: {_display(item.benchmark.benchmark_type)}",
                    f"- Recorded at: {_display(item.created_at)}",
                    f"- Model: {_display(item.model.display_name)}",
                    f"- Model settings: {_render_model_settings(item.model)}",
                ]
            )
            if item.session:
                session = item.session
                session_value = session.title or f"Session {session.id}" if session.id is not None else session.title
                lines.append(f"- Session: {_display(session_value)}")
                if session.started_at or session.completed_at:
                    lines.append(f"- Session period: {_display(session.started_at)} → {_display(session.completed_at)}")
            if item.hardware:
                hardware = item.hardware
                hardware_value = ", ".join(
                    value for value in (hardware.name, hardware.computer_name, hardware.cpu, hardware.gpu) if value
                )
                lines.append(f"- Hardware: {_display(hardware_value)}")
                if hardware.operating_system:
                    lines.append(f"- Operating system: {hardware.operating_system}")
            prompt = item.prompt
            prompt_value = ", ".join(
                value for value in (
                    prompt.name,
                    f"version {prompt.version}" if prompt.version else "",
                    f"SHA-256 {prompt.prompt_hash}" if prompt.prompt_hash else "",
                ) if value
            )
            if prompt_value:
                lines.append(f"- Prompt template: {prompt_value}")
            if item.review:
                review = item.review
                lines.extend(
                    [
                        "",
                        "##### Review",
                        f"- Accuracy: {_display(review.accuracy_score)}",
                        f"- Hallucination: {_display(review.hallucination_level)}",
                        f"- Reliability: {_display(review.reliability_level)}",
                        f"- Depth: {_display(review.depth_score)}",
                        f"- Signal-to-noise: {_display(review.signal_noise_score)}",
                        f"- Actionability: {_display(review.actionability_score)}",
                        f"- Seniority: {_display(review.seniority_score)}",
                        f"- Overall: {_display(review.overall_score)}",
                        f"- Strengths: {_display(review.strengths)}",
                        f"- Weaknesses: {_display(review.weaknesses)}",
                        f"- Verdict: {_display(review.verdict)}",
                        f"- Notes: {_display(review.notes)}",
                        f"- Review recorded at: {_display(review.created_at)}",
                    ]
                )
            else:
                lines.extend(["", "- Review: Unavailable"])
            if item.attachments:
                lines.extend(["", "##### Attachments", ""])
                lines.extend(
                    f"- {_display(attachment.attachment_type)}: {_display(attachment.original_filename)} "
                    f"(path: {_display(attachment.file_path)}; notes: {_display(attachment.notes)})"
                    for attachment in item.attachments
                )
            _render_indented_block(lines, "Prompt text", item.prompt_text)
            _render_indented_block(lines, "Raw model output", item.raw_model_output)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_run_detail(lines: list[str], item: BenchmarkRunReportItem) -> None:
    lines.extend(
        [
            f"### {item.model_name} — {item.benchmark_name}",
            "",
            f"- Run ID: {_display(item.run_id)}",
            f"- Benchmark file: {_display(item.benchmark_file)}",
            f"- Benchmark type: {_display(item.benchmark.benchmark_type)}",
            f"- Recorded at: {_display(item.created_at)}",
            f"- Model settings: {_render_model_settings(item.model)}",
        ]
    )
    if item.hardware:
        lines.append(f"- Hardware: {_hardware_label(item.hardware)}")
    else:
        lines.append("- Hardware: Unknown hardware")
    prompt = item.prompt
    prompt_value = ", ".join(
        value
        for value in (
            prompt.name,
            f"version {prompt.version}" if prompt.version else "",
            f"SHA-256 {prompt.prompt_hash}" if prompt.prompt_hash else "",
        )
        if value
    )
    if prompt_value:
        lines.append(f"- Prompt template: {prompt_value}")
    if item.review:
        review = item.review
        lines.extend(
            [
                "",
                "#### Review",
                f"- Overall: {_display(review.overall_score)}",
                f"- Hallucination: {_display(review.hallucination_level)}",
                f"- Reliability: {_display(review.reliability_level)}",
                f"- Strengths: {_display(review.strengths)}",
                f"- Weaknesses: {_display(review.weaknesses)}",
                f"- Verdict: {_display(review.verdict)}",
            ]
        )
    else:
        lines.extend(["", "- Review: Unavailable"])
    if item.attachments:
        lines.extend(["", "#### Attachment metadata", ""])
        lines.extend(
            f"- {_display(attachment.attachment_type)}: {_display(attachment.original_filename)} "
            f"(path: {_display(attachment.file_path)}; notes: {_display(attachment.notes)})"
            for attachment in item.attachments
        )
    _render_indented_block(lines, "Prompt text", item.prompt_text)
    _render_indented_block(lines, "Raw model output", item.raw_model_output)
    lines.append("")


def render_session_markdown(
    report: SessionReport,
    *,
    template_options: ReportTemplateOptions | None = None,
) -> str:
    options = template_options or ReportTemplateOptions()
    lines = _render_header(report.metadata)
    lines.extend(
        [
            "",
            "## Session",
            "",
            f"- Session ID: {_display(report.session.id)}",
            f"- Title: {_display(report.session.title)}",
            f"- Description: {_display(report.session.description)}",
            f"- Started at: {_display(report.session.started_at)}",
            f"- Completed at: {_display(report.session.completed_at)}",
            f"- Notes: {_display(report.session.notes)}",
        ]
    )
    _render_stats(lines, report.summary, "Session score summary")
    lines.extend(
        [
            "",
            f"- Average tokens/s: {_display(report.average_tokens_per_second)}",
            f"- Represented models: {_display(', '.join(report.represented_models) or None)}",
            f"- Represented benchmarks: {_display(', '.join(report.represented_benchmarks) or None)}",
            f"- Represented hardware: {_display(', '.join(report.represented_hardware) or None)}",
            f"- Hallucination distribution: {_format_distribution(report.hallucination_distribution)}",
            f"- Reliability distribution: {_format_distribution(report.reliability_distribution)}",
        ]
    )
    if options.include_record_details or _template_has_sensitive_content(options):
        lines.extend(["", "## Session runs", ""])
        if not report.records:
            lines.append("No benchmark runs selected.")
        for item in report.records:
            _render_run_detail(lines, item)
    else:
        _render_compact_run_table(lines, report.records)
    return "\n".join(lines).rstrip() + "\n"


def render_hardware_report_markdown(
    report: HardwareReport,
    *,
    template_options: ReportTemplateOptions | None = None,
) -> str:
    options = template_options or ReportTemplateOptions(
        include_hardware_details=report.include_hardware_details,
    )
    lines = _render_header(report.metadata)
    _render_stats(lines, report.summary, "Overall hardware score summary")
    lines.extend(["", "## Hardware groups", ""])
    if not report.groups:
        lines.append("No benchmark runs selected.")
    show_details = report.include_hardware_details or options.include_hardware_details or _template_has_sensitive_content(options)
    for group in report.groups:
        lines.extend(
            [
                f"### {group.label}",
                "",
                f"- Snapshot key: `{group.key}`",
                f"- Run count: {group.summary.count}",
                f"- Scored count: {group.summary.scored_count}",
                f"- Average overall score: {_display(group.summary.average)}",
                f"- Median overall score: {_display(group.summary.median)}",
                f"- Minimum overall score: {_display(group.summary.minimum)}",
                f"- Maximum overall score: {_display(group.summary.maximum)}",
                f"- Score distribution: {_format_distribution(group.summary.score_distribution)}",
                f"- Average tokens/s: {_display(group.average_tokens_per_second)}",
                f"- Represented models: {_display(', '.join(group.represented_models) or None)}",
                f"- Represented benchmarks: {_display(', '.join(group.represented_benchmarks) or None)}",
                f"- Hallucination distribution: {_format_distribution(group.hallucination_distribution)}",
                f"- Reliability distribution: {_format_distribution(group.reliability_distribution)}",
            ]
        )
        if show_details:
            lines.extend(["", "#### Contributing runs", ""])
            if options.include_record_details or _template_has_sensitive_content(options):
                for item in group.records:
                    _render_run_detail(lines, item)
            else:
                _render_compact_run_table(lines, group.records)
    return "\n".join(lines).rstrip() + "\n"


def render_scoreboard_markdown(report: ScoreboardReport) -> str:
    lines = _render_header(report.metadata)
    _render_stats(lines, report.summary, "Overall score summary")
    lines.extend(["", "## Scoreboard entries", ""])
    if not report.entries:
        lines.append("No scoreboard entries selected.")
    for section in report.batch_sections:
        lines.extend([f"### {section.label}", ""])
        if section.batch:
            lines.extend(
                [
                    f"- Source file: {_display(section.batch.source_file)}",
                    f"- Imported at: {_display(section.batch.imported_at)}",
                ]
            )
        lines.extend(
            [
                "",
                "| Model | Score | Temperature | MoE experts | Context | Tokens/s | Review quality | Hallucination | Consistency | Reliability | Verdict | Notes | Source | Imported |",
                "| --- | ---: | ---: | --- | ---: | ---: | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for aggregate in section.entries:
            entry = aggregate.entry
            lines.append(
                "| " + " | ".join(
                    _markdown_cell(value)
                    for value in (
                        entry.model_name,
                        entry.score,
                        entry.temperature,
                        entry.moe_experts,
                        entry.context_length,
                        entry.tokens_per_second,
                        entry.review_quality,
                        entry.hallucination_level,
                        entry.consistency,
                        entry.reliability_score,
                        entry.verdict,
                        entry.notes,
                        entry.source_file,
                        entry.imported_at,
                    )
                ) + " |"
            )
        lines.extend(["", f"Batch score distribution: {_format_distribution(section.summary.score_distribution)}", ""])
    return "\n".join(lines).rstrip() + "\n"


def render_model_leaderboard_markdown(
    report: ModelLeaderboardReport,
    *,
    include_model_details: bool = False,
    template_options: ReportTemplateOptions | None = None,
) -> str:
    if template_options is not None:
        include_model_details = template_options.include_model_details
    lines = _render_header(report.metadata)
    lines.extend(
        [
            "",
            "## Ranked models",
            "",
            "| Rank | Model | Runs | Scored | Average | Median | Minimum | Maximum | Score distribution | Average tokens/s | Hallucination distribution | Reliability distribution |",
            "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |",
        ]
    )
    if not report.models:
        lines.append("| — | No eligible models | 0 | 0 | — | — | — | — | — | — | — | — |")
    for entry in report.models:
        lines.append(
            "| " + " | ".join(
                _markdown_cell(value)
                for value in (
                    entry.rank,
                    entry.model_name,
                    entry.run_count,
                    entry.scored_run_count,
                    entry.average_overall_score,
                    entry.median_overall_score,
                    entry.minimum_overall_score,
                    entry.maximum_overall_score,
                    _format_distribution(entry.score_distribution),
                    entry.average_tokens_per_second,
                    _format_distribution(entry.hallucination_distribution),
                    _format_distribution(entry.reliability_distribution),
                )
            ) + " |"
        )
    if include_model_details:
        for entry in report.models:
            lines.extend(
                [
                    "",
                    f"### {entry.model_name}",
                    "",
                    f"- Score distribution: {_format_distribution(entry.score_distribution)}",
                    f"- Hallucination distribution: {_format_distribution(entry.hallucination_distribution)}",
                    f"- Reliability distribution: {_format_distribution(entry.reliability_distribution)}",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _comparison_value(value: Any) -> str:
    return "Not available" if value is None else _display(value)


def _comparison_percentage(value: float | None) -> str:
    return "Not available" if value is None else f"{value:.2f}%"


def _comparison_labels(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "None represented"


def _render_comparison_header(report: ModelComparisonResult | SessionComparisonResult) -> list[str]:
    metadata = report.metadata
    comparison_type = metadata.comparison_type.value if hasattr(metadata.comparison_type, "value") else metadata.comparison_type
    selected = ", ".join(metadata.selected_entities) if metadata.selected_entities else "None"
    filters = "; ".join(
        f"{key}={value}" for key, value in metadata.active_filters.items()
    ) or "None"
    return [
        f"# {_display(metadata.title)}",
        "",
        f"- Comparison type: {_display(comparison_type)}",
        f"- Generated at: {_display(metadata.generated_at)}",
        f"- Contributing BenchmarkRun records: {metadata.contributing_record_count}",
        f"- Selected entities: {_display(selected)}",
        f"- Active filters: {_display(filters)}",
    ]


def _render_comparison_summary_table(
    lines: list[str],
    report: ModelComparisonResult | SessionComparisonResult,
) -> None:
    lines.extend(
        [
            "",
            "## Broad comparison",
            "",
            "| Entity | Runs | Scored | Mean score | Median score | Mean tokens/s | Models | Benchmarks | Sessions | Hardware |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not report.entities:
        lines.append("| No eligible entities | 0 | 0 | Not available | Not available | Not available | 0 | 0 | 0 | 0 |")
        return
    for entity in report.entities:
        summary = entity.summary
        lines.append(
            "| " + " | ".join(
                _markdown_cell(value)
                for value in (
                    entity.label,
                    entity.record_count,
                    entity.scored_count,
                    _comparison_value(entity.overall_score.mean),
                    _comparison_value(entity.overall_score.median),
                    _comparison_value(entity.tokens_per_second.mean),
                    summary.unique_model_count,
                    summary.unique_benchmark_count,
                    summary.unique_session_count,
                    summary.unique_hardware_environment_count,
                )
            ) + " |"
        )
    lines.extend(["", "### Represented snapshot identities", ""])
    if isinstance(report, ModelComparisonResult):
        lines.extend(
            [
                "| Model | Benchmarks | Sessions | Hardware |",
                "| --- | --- | --- | --- |",
            ]
        )
        for entity in report.entities:
            lines.append(
                "| " + " | ".join(
                    _markdown_cell(value)
                    for value in (
                        entity.label,
                        _comparison_labels(entity.represented_benchmarks),
                        _comparison_labels(entity.represented_sessions),
                        _comparison_labels(entity.represented_hardware),
                    )
                ) + " |"
            )
    else:
        lines.extend(
            [
                "| Session | Models | Benchmarks | Hardware |",
                "| --- | --- | --- | --- |",
            ]
        )
        for entity in report.entities:
            lines.append(
                "| " + " | ".join(
                    _markdown_cell(value)
                    for value in (
                        entity.label,
                        _comparison_labels(entity.represented_models),
                        _comparison_labels(entity.represented_benchmarks),
                        _comparison_labels(entity.represented_hardware),
                    )
                ) + " |"
            )


def _render_comparison_session_metadata(
    lines: list[str],
    report: SessionComparisonResult,
) -> None:
    lines.extend(
        [
            "",
            "## Selected session metadata",
            "",
            "| ID | Session | Description | Started | Completed | Notes |",
            "| ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for session in report.selected_sessions:
        lines.append(
            "| " + " | ".join(
                _markdown_cell(value)
                for value in (
                    session.id,
                    session.label,
                    session.description,
                    session.started_at,
                    session.completed_at,
                    session.notes,
                )
            ) + " |"
        )


def _render_categorical_comparisons(
    lines: list[str],
    report: ModelComparisonResult | SessionComparisonResult,
) -> None:
    rows_by_prefix: dict[str, list[Any]] = {}
    for row in report.categorical_comparisons:
        prefix, _, _ = row.category.partition(":")
        rows_by_prefix.setdefault(prefix, []).append(row)
    if not rows_by_prefix:
        return
    lines.extend(["", "## Categorical distributions", ""])
    entities = tuple(report.entities)
    for prefix in sorted(rows_by_prefix, key=lambda value: (value.casefold(), value)):
        lines.extend(
            [
                f"### {prefix.replace('_', ' ').title()}",
                "",
                "| Category | " + " | ".join(
                    f"{entity.label} count / % / missing" for entity in entities
                ) + " |",
                "| --- | " + " | ".join("---" for _ in entities) + " |",
            ]
        )
        for row in rows_by_prefix[prefix]:
            category = row.category.partition(":")[2]
            cells = []
            for entity in entities:
                cells.append(
                    f"{row.counts.get(entity.identity, 0)} / "
                    f"{_comparison_percentage(row.percentages.get(entity.identity))} / "
                    f"{row.missing_counts.get(entity.identity, 0)}"
                )
            lines.append("| " + " | ".join(_markdown_cell(value) for value in (category, *cells)) + " |")


def _render_aligned_statistics(statistics: Any) -> tuple[Any, ...]:
    return (
        statistics.record_count,
        statistics.scored_count,
        _comparison_value(statistics.overall_score.mean),
        _comparison_value(statistics.overall_score.median),
        _comparison_value(statistics.tokens_per_second.mean),
    )


def _render_aligned_groups(
    lines: list[str],
    heading: str,
    groups: Sequence[Any],
    entities: Sequence[Any],
) -> None:
    lines.extend(
        [
            "",
            f"### {heading}",
            "",
            "| Aligned key | Entity | Runs | Scored | Mean score | Median score | Mean tokens/s |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not groups:
        lines.append("| No shared keys | Not applicable | 0 | 0 | Not available | Not available | Not available |")
        return
    for group in groups:
        for entity in entities:
            statistics = group.entities.get(entity.identity)
            values = _render_aligned_statistics(statistics) if statistics is not None else (0, 0, None, None, None)
            lines.append(
                "| " + " | ".join(
                    _markdown_cell(value) for value in (group.label, entity.label, *values)
                ) + " |"
            )


def _render_model_alignment(lines: list[str], report: ModelComparisonResult) -> None:
    alignment = report.alignment
    lines.extend(
        [
            "",
            "## Aligned benchmark comparison",
            "",
            f"- Shared benchmarks: {alignment.shared_benchmark_count}",
            f"- Excluded non-overlapping benchmark identities: {alignment.excluded_benchmark_count}",
        ]
    )
    if alignment.non_overlapping_benchmarks:
        lines.extend(
            [
                "",
                "### Non-overlapping benchmarks",
                "",
                "| Model | Benchmarks present only for this model |",
                "| --- | --- |",
            ]
        )
        for entity in report.entities:
            lines.append(
                f"| {_markdown_cell(entity.label)} | "
                f"{_markdown_cell(_comparison_labels(alignment.non_overlapping_benchmarks.get(entity.identity, ())))} |"
            )
    _render_aligned_groups(lines, "Shared benchmark summaries", alignment.aligned_benchmarks, report.entities)


def _render_session_alignment(lines: list[str], report: SessionComparisonResult) -> None:
    alignment = report.alignment
    lines.extend(
        [
            "",
            "## Aligned session comparison",
            "",
            f"- Shared models: {alignment.shared_model_count}",
            f"- Shared benchmarks: {alignment.shared_benchmark_count}",
            f"- Shared model/benchmark pairs: {alignment.shared_model_benchmark_pair_count}",
        ]
    )
    _render_aligned_groups(lines, "Shared model summaries", alignment.aligned_models, report.entities)
    _render_aligned_groups(lines, "Shared benchmark summaries", alignment.aligned_benchmarks, report.entities)
    lines.extend(
        [
            "",
            "### Shared model/benchmark pair summaries",
            "",
            "| Model | Benchmark | Session | Runs | Scored | Mean score | Median score | Mean tokens/s |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    if not alignment.aligned_model_benchmarks:
        lines.append("| No shared pairs | Not applicable | Not applicable | 0 | 0 | Not available | Not available | Not available |")
    else:
        for pair in alignment.aligned_model_benchmarks:
            for entity in report.entities:
                statistics = pair.entities.get(entity.identity)
                values = _render_aligned_statistics(statistics) if statistics is not None else (0, 0, None, None, None)
                lines.append(
                    "| " + " | ".join(
                        _markdown_cell(value)
                        for value in (pair.model_label, pair.benchmark_label, entity.label, *values)
                    ) + " |"
                )
    lines.extend(["", "### Non-overlapping session identities", "", "| Session | Models | Benchmarks | Model/benchmark pairs |", "| --- | --- | --- | --- |"])
    for entity in report.entities:
        lines.append(
            "| " + " | ".join(
                _markdown_cell(value)
                for value in (
                    entity.label,
                    _comparison_labels(alignment.non_overlapping_models.get(entity.identity, ())),
                    _comparison_labels(alignment.non_overlapping_benchmarks.get(entity.identity, ())),
                    _comparison_labels(
                        tuple(
                            f"{model} / {benchmark}"
                            for model, benchmark in alignment.non_overlapping_model_benchmark_pairs.get(entity.identity, ())
                        )
                    ),
                )
            ) + " |"
        )


def _render_comparison_pairwise(
    lines: list[str],
    report: ModelComparisonResult | SessionComparisonResult,
) -> None:
    if report.pairwise is None:
        return
    pairwise = report.pairwise
    first = next((entity.label for entity in report.entities if entity.identity == pairwise.baseline_entity), pairwise.baseline_entity)
    second = next((entity.label for entity in report.entities if entity.identity == pairwise.comparison_entity), pairwise.comparison_entity)
    lines.extend(
        [
            "",
            "## Pairwise deltas",
            "",
            f"- Baseline: {first}",
            f"- Comparison: {second}",
            "- Delta direction: second selected entity minus first selected entity.",
            "",
            "| Metric | First value | Second value | Absolute delta | Percentage delta | Direction | Availability |",
            "| --- | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for metric in pairwise.metrics:
        availability = "Available" if metric.is_available else (metric.unavailable_reason or "Not available")
        lines.append(
            "| " + " | ".join(
                _markdown_cell(value)
                for value in (
                    metric.metric_name,
                    _comparison_value(metric.values.get(pairwise.baseline_entity)),
                    _comparison_value(metric.values.get(pairwise.comparison_entity)),
                    _comparison_value(metric.absolute_delta),
                    _comparison_percentage(metric.percentage_delta),
                    metric.direction.value,
                    availability,
                )
            ) + " |"
        )


def _render_model_rankings(lines: list[str], report: ModelComparisonResult) -> None:
    for heading, ranking, value_name in (
        ("Model ranking", report.ranking, "Mean score"),
        ("Speed ranking", report.speed_ranking, "Mean tokens/s"),
    ):
        lines.extend(
            [
                "",
                f"## {heading}",
                "",
                f"| Rank | Model | Scored | Mean score | Median score | {value_name} |",
                "| ---: | --- | ---: | ---: | ---: | ---: |",
            ]
        )
        if not ranking:
            lines.append("| Not ranked | No selected models | 0 | Not available | Not available | Not available |")
        for entry in ranking:
            primary = entry.mean_overall_score if value_name == "Mean score" else entry.mean_tokens_per_second
            lines.append(
                "| " + " | ".join(
                    _markdown_cell(value)
                    for value in (
                        entry.rank if entry.rank is not None else "Unranked",
                        entry.label,
                        entry.scored_count,
                        _comparison_value(entry.mean_overall_score),
                        _comparison_value(entry.median_overall_score),
                        _comparison_value(primary),
                    )
                ) + " |"
            )


def _render_comparison_methodology(lines: list[str]) -> None:
    lines.extend(
        [
            "",
            "## Methodology and unavailable values",
            "",
            "- BenchmarkRun snapshots are authoritative; scoreboard entries are not included in comparisons.",
            "- Non-deleted runs are selected by default. Numeric missing values are omitted from calculations and never zero-filled.",
            "- Categorical percentages use observed values as the denominator; missing values remain a separate count.",
            "- Broad summaries include each selected entity's eligible records. Aligned summaries use exact shared snapshot identities and expose non-overlap separately; runs contribute equally within each summary.",
            "- Pairwise percentages are unavailable when a value is missing or the first selected value is zero. Pairwise deltas are always second minus first.",
            "- This report contains descriptive comparisons only; it does not claim statistical significance, trends, forecasts, or causal effects.",
        ]
    )


def render_model_comparison_markdown(report: ModelComparisonResult) -> str:
    lines = _render_comparison_header(report)
    _render_comparison_summary_table(lines, report)
    _render_categorical_comparisons(lines, report)
    _render_model_alignment(lines, report)
    _render_comparison_pairwise(lines, report)
    _render_model_rankings(lines, report)
    _render_comparison_methodology(lines)
    return "\n".join(lines).rstrip() + "\n"


def render_session_comparison_markdown(report: SessionComparisonResult) -> str:
    lines = _render_comparison_header(report)
    _render_comparison_session_metadata(lines, report)
    _render_comparison_summary_table(lines, report)
    _render_categorical_comparisons(lines, report)
    _render_session_alignment(lines, report)
    _render_comparison_pairwise(lines, report)
    _render_comparison_methodology(lines)
    return "\n".join(lines).rstrip() + "\n"


def _trend_summary_value(summary: Any, field: str = "mean") -> Any:
    return getattr(summary, field, None) if summary is not None else None


def _trend_distribution(distribution: Any) -> str:
    return _format_distribution(distribution.counts if distribution is not None else {})


def _render_trend_point_table(lines: list[str], points: Sequence[Any]) -> None:
    lines.extend(
        [
            "| Bucket | Records | Scored | Score mean | Score median | Speed mean | Hallucination | Reliability | Consistency |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
        ]
    )
    if not points:
        lines.append("| — | 0 | 0 | — | — | — | — | — | — |")
        return
    for point in points:
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    point.label or point.bucket_start,
                    point.record_count,
                    point.scored_count,
                    _trend_summary_value(point.overall_score),
                    _trend_summary_value(point.overall_score, "median"),
                    _trend_summary_value(point.tokens_per_second),
                    _trend_distribution(point.hallucination),
                    _trend_distribution(point.reliability),
                    _trend_distribution(point.consistency),
                )
            )
            + " |"
        )


def _render_trend_series_summary(lines: list[str], report: TrendReport) -> None:
    lines.extend(
        [
            "",
            "## Series summary",
            "",
            "| Series | Records | Scored | Populated buckets | First score mean | Last score mean | Score delta | Score % delta | First speed mean | Last speed mean | Speed delta |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    series = (report.aggregate_series, *report.series)
    for item in series:
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    item.label,
                    item.total_contributing_records,
                    item.total_scored_records,
                    item.populated_bucket_count,
                    _trend_summary_value(item.first_available_score_summary),
                    _trend_summary_value(item.last_available_score_summary),
                    item.score_absolute_delta,
                    item.score_percentage_delta,
                    _trend_summary_value(item.first_available_speed_summary),
                    _trend_summary_value(item.last_available_speed_summary),
                    item.speed_absolute_delta,
                )
            )
            + " |"
        )


def _render_trend_series_details(lines: list[str], series: Sequence[TrendSeries]) -> None:
    for item in series:
        lines.extend(
            [
                "",
                f"### {item.label}",
                "",
                f"- Series identity: `{_markdown_cell(item.identity)}`",
                f"- Contributing records: {item.total_contributing_records}",
                f"- Scored records: {item.total_scored_records}",
                f"- Bucket count: {item.bucket_count}",
                f"- Populated bucket count: {item.populated_bucket_count}",
                f"- Empty bucket count: {item.missing_bucket_count}",
                f"- First-to-last score delta (last minus first): {_display(item.score_absolute_delta)}",
                f"- First-to-last speed delta (last minus first): {_display(item.speed_absolute_delta)}",
                "",
            ]
        )
        _render_trend_point_table(lines, item.points)


def render_trend_markdown(
    report: TrendReport,
    *,
    include_series_details: bool = False,
) -> str:
    """Render a trend report using only its structured typed values."""

    metadata = report.metadata
    date_range = " to ".join(value or "Not available" for value in metadata.date_range)
    interval = metadata.bucket_interval.value if isinstance(metadata.bucket_interval, Enum) else metadata.bucket_interval
    grouping = report.grouping.value if isinstance(report.grouping, Enum) else report.grouping
    trend_type = metadata.trend_type.value if isinstance(metadata.trend_type, Enum) else metadata.trend_type
    filters = "; ".join(f"{key}={value}" for key, value in metadata.active_filters.items()) or "None"
    lines = [
        f"# {_display(metadata.title)}",
        "",
        f"- Generated at: {_display(metadata.generated_at)}",
        f"- Trend type: {_display(trend_type)}",
        f"- Source record family: {_display(metadata.source_record_family)}",
        f"- Bucket interval: {_display(interval)}",
        f"- Date range: {_display(date_range)}",
        f"- Grouping: {_display(grouping)}",
        f"- Contributing records: {metadata.contributing_record_count}",
        f"- Excluded missing/invalid timestamps: {metadata.excluded_timestamp_count}",
        f"- Active filters: {_display(filters)}",
        f"- Empty buckets: {'Included between observed buckets' if report.include_empty_buckets else 'Excluded by default'}",
    ]
    lines.extend(["", "## Methodology", "", f"- {report.methodology_note}"])
    lines.extend(
        [
            "- Deltas are descriptive last-minus-first differences; a positive or negative delta is not labeled as improvement or regression.",
            "- Composition changes across buckets may affect the observed values. No significance or causal interpretation is performed.",
        ]
    )
    lines.extend(["", "## Coverage and excluded data", ""])
    if report.coverage_warnings:
        lines.append("Coverage warnings:")
        lines.extend(f"- {warning}" for warning in report.coverage_warnings)
    else:
        lines.append("- No coverage warnings.")
    for note in report.excluded_data_notes:
        lines.append(f"- {note}")
    lines.extend(
        [
            f"- Same series time range: {'Yes' if report.same_time_range else 'No'}",
            f"- Same represented benchmarks where calculable: {_display(report.same_represented_benchmarks)}",
            f"- Same represented models where calculable: {_display(report.same_represented_models)}",
        ]
    )
    _render_trend_series_summary(lines, report)
    lines.extend(["", "## Overall aggregate buckets", ""])
    _render_trend_point_table(lines, report.aggregate_series.points)
    if report.series and include_series_details:
        lines.extend(["", "## Grouped series buckets", ""])
        _render_trend_series_details(lines, report.series)
    elif report.series:
        lines.extend(
            [
                "",
                "Grouped-series bucket details are omitted. Enable detailed series sections to include them.",
            ]
        )
    elif include_series_details:
        lines.extend(["", "No grouped series are represented by the current selection."])
    return "\n".join(lines).rstrip() + "\n"


def render_benchmark_run_trend_markdown(
    report: TrendReport,
    *,
    include_series_details: bool = False,
) -> str:
    return render_trend_markdown(report, include_series_details=include_series_details)


def render_scoreboard_trend_markdown(
    report: TrendReport,
    *,
    include_series_details: bool = False,
) -> str:
    return render_trend_markdown(report, include_series_details=include_series_details)


def render_markdown(
    report: ReportDocument,
    *,
    template_options: ReportTemplateOptions | None = None,
) -> str:
    if isinstance(report, TrendReport):
        include_details = template_options.include_record_details if template_options is not None else False
        return render_trend_markdown(report, include_series_details=include_details)
    if isinstance(report, ModelComparisonResult):
        return render_model_comparison_markdown(report)
    if isinstance(report, SessionComparisonResult):
        return render_session_comparison_markdown(report)
    if isinstance(report, BenchmarkRunReport):
        return render_benchmark_run_markdown(report, template_options=template_options)
    if isinstance(report, SessionReport):
        return render_session_markdown(report, template_options=template_options)
    if isinstance(report, HardwareReport):
        return render_hardware_report_markdown(report, template_options=template_options)
    if isinstance(report, ScoreboardReport):
        return render_scoreboard_markdown(report)
    return render_model_leaderboard_markdown(report, template_options=template_options)


def render_combined_markdown(benchmark_report: BenchmarkRunReport, scoreboard_report: ScoreboardReport) -> str:
    """Render the legacy combined report shape from structured reports."""

    lines = ["# BenchPup report", "", "## Benchmark runs", ""]
    for record in benchmark_report.records:
        lines.append(f"- {record.model_name} — {record.benchmark_file or 'custom'}")
    lines.extend(["", "## Scoreboard", ""])
    for section in scoreboard_report.batch_sections:
        lines.extend([f"### {section.label}", ""])
        for aggregate in section.entries:
            entry = aggregate.entry
            lines.append(f"- {entry.model_name} | score: {_display(entry.score)}")
    return "\n".join(lines) + "\n"


def write_markdown_report(
    report: ReportDocument | str,
    destination: str | Path,
    *,
    overwrite: bool = False,
    template_options: ReportTemplateOptions | None = None,
    include_series_details: bool | None = None,
) -> ReportWriteResult:
    """Write UTF-8 Markdown through a same-directory staged replacement.

    The engine never prompts.  Callers must explicitly set ``overwrite=True``
    to replace an existing file.
    """

    path = Path(destination)
    if path.exists() and path.is_dir():
        return ReportWriteResult(
            ReportWriteStatus.TEMP_WRITE_FAILED,
            path,
            "Report destination is a directory",
        )
    if path.exists() and not overwrite:
        return ReportWriteResult(
            ReportWriteStatus.OVERWRITE_REQUIRED,
            path,
            "Existing report requires explicit overwrite confirmation",
        )
    temporary: Path | None = None
    finalizing = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.parent.is_dir():
            return ReportWriteResult(ReportWriteStatus.TEMP_WRITE_FAILED, path, "Report parent is not a directory")
        if isinstance(report, TrendReport) and include_series_details is not None:
            template_options = replace(
                template_options or ReportTemplateOptions(),
                include_record_details=include_series_details,
            )
        content = report if isinstance(report, str) else render_markdown(report, template_options=template_options)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        finalizing = True
        os.replace(temporary, path)
        temporary = None
        return ReportWriteResult(ReportWriteStatus.SUCCESS, path, "Markdown report written successfully")
    except OSError as error:
        return ReportWriteResult(
            ReportWriteStatus.FINALIZE_FAILED if finalizing else ReportWriteStatus.TEMP_WRITE_FAILED,
            path,
            "Could not write Markdown report",
            details=f"{type(error).__name__}: {error}",
        )
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


# Friendly aliases for callers that prefer generate/render terminology.
DetailedBenchmarkReport = BenchmarkRunReport
HistoricalScoreboardReport = ScoreboardReport
SessionBenchmarkReport = SessionReport
ModelComparisonReport = ModelComparisonResult
SessionComparisonReport = SessionComparisonResult
ReportSummary = ScoreStatistics
generate_benchmark_run_report = build_benchmark_run_report
generate_scoreboard_report = build_scoreboard_report
generate_model_leaderboard = build_model_leaderboard
generate_session_report = build_session_report
generate_hardware_report = build_hardware_report
render_benchmark_report_markdown = render_benchmark_run_markdown
render_leaderboard_markdown = render_model_leaderboard_markdown
render_session_report_markdown = render_session_markdown
render_hardware_markdown = render_hardware_report_markdown
render_model_comparison = render_model_comparison_markdown
render_session_comparison = render_session_comparison_markdown
render_benchmark_trend_markdown = render_benchmark_run_trend_markdown
render_scoreboard_trend = render_scoreboard_trend_markdown
write_report_markdown = write_markdown_report


def build_html_analytics_report(*args: Any, **kwargs: Any) -> Any:
    """Lazy compatibility import for the dedicated HTML analytics module."""

    from .html_reporting import build_html_analytics_report as builder

    return builder(*args, **kwargs)


def render_html_analytics_report(*args: Any, **kwargs: Any) -> str:
    """Lazy compatibility import for standalone HTML rendering."""

    from .html_reporting import render_html_analytics_report as renderer

    return renderer(*args, **kwargs)


def write_html_analytics_report(*args: Any, **kwargs: Any) -> ReportWriteResult:
    """Lazy compatibility import for staged HTML analytics writing."""

    from .html_reporting import write_html_analytics_report as writer

    return writer(*args, **kwargs)


build_html_analytics = build_html_analytics_report
render_html_analytics = render_html_analytics_report
write_html_analytics = write_html_analytics_report


__all__ = (
    "AttachmentReportMetadata",
    "BenchmarkModelSection",
    "BenchmarkReportFilters",
    "BenchmarkReportSummary",
    "BenchmarkRunAggregate",
    "BenchmarkRunReport",
    "BenchmarkRunReportItem",
    "HardwareReport",
    "HardwareReportGroup",
    "HardwareReportSummary",
    "ModelComparisonReport",
    "ModelComparisonResult",
    "DetailedBenchmarkReport",
    "HistoricalScoreboardReport",
    "SessionBenchmarkReport",
    "SessionReport",
    "ModelLeaderboardEntry",
    "ModelLeaderboardReport",
    "ModelReportSummary",
    "PromptReportSummary",
    "ReportDocument",
    "ReportMetadata",
    "ReportTemplate",
    "ReportTemplateId",
    "ReportTemplateOptions",
    "ReportSummary",
    "ReportType",
    "ReportWriteResult",
    "ReportWriteStatus",
    "ReportingService",
    "ReviewScoreSummary",
    "ScoreStatistics",
    "ScoreboardBatchSection",
    "ScoreboardEntryAggregate",
    "ScoreboardReport",
    "ScoreboardReportFilters",
    "SessionComparisonReport",
    "SessionComparisonResult",
    "SessionReportSummary",
    "TrendReport",
    "build_html_analytics",
    "build_html_analytics_report",
    "build_benchmark_run_report",
    "build_hardware_report",
    "build_model_leaderboard",
    "build_scoreboard_report",
    "build_session_report",
    "available_report_templates",
    "apply_report_template",
    "get_report_template",
    "generate_benchmark_run_report",
    "generate_hardware_report",
    "generate_model_leaderboard",
    "generate_scoreboard_report",
    "generate_session_report",
    "hardware_snapshot_label",
    "normalize_hardware_snapshot",
    "render_benchmark_report_markdown",
    "render_benchmark_run_markdown",
    "render_benchmark_run_trend_markdown",
    "render_benchmark_trend_markdown",
    "render_combined_markdown",
    "render_leaderboard_markdown",
    "render_markdown",
    "render_model_comparison",
    "render_model_comparison_markdown",
    "render_model_leaderboard_markdown",
    "render_hardware_report_markdown",
    "render_hardware_markdown",
    "render_html_analytics",
    "render_html_analytics_report",
    "render_scoreboard_markdown",
    "render_scoreboard_trend",
    "render_scoreboard_trend_markdown",
    "render_session_markdown",
    "render_session_comparison",
    "render_session_comparison_markdown",
    "render_session_report_markdown",
    "render_trend_markdown",
    "write_html_analytics",
    "write_html_analytics_report",
    "write_markdown_report",
    "write_report_markdown",
)
