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
from statistics import median
from typing import Any, Mapping, Sequence, TypeAlias, cast
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
from .services import BenchmarkService, CatalogService


class ReportType(str, Enum):
    """Stable identifiers for the report families produced by this module."""

    BENCHMARK_RUNS = "benchmark_runs"
    SCOREBOARD = "scoreboard"
    MODEL_LEADERBOARD = "model_leaderboard"


class ReportWriteStatus(str, Enum):
    """Result states for safe Markdown output."""

    SUCCESS = "success"
    OVERWRITE_REQUIRED = "overwrite_required"
    TEMP_WRITE_FAILED = "temp_write_failed"
    FINALIZE_FAILED = "finalize_failed"


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
    numeric = [float(value) for value in scores if value is not None]
    distribution = Counter(numeric)
    if not numeric:
        return ScoreStatistics(count=len(scores) if count is None else count, score_distribution=distribution)
    return ScoreStatistics(
        count=len(scores) if count is None else count,
        scored_count=len(numeric),
        average=sum(numeric) / len(numeric),
        median=float(median(numeric)),
        minimum=min(numeric),
        maximum=max(numeric),
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


ReportDocument: TypeAlias = BenchmarkRunReport | ScoreboardReport | ModelLeaderboardReport
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
        if not _contains(hardware_text, filters.hardware):
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
) -> BenchmarkRunReport:
    selected = [
        aggregate for aggregate in _run_source(runs, service, catalog)
        if _run_matches(aggregate, filters)
    ]
    records = tuple(
        sorted(
            (
                _aggregate_item(
                    aggregate,
                    include_prompt_text=include_prompt_text,
                    include_raw_model_output=include_raw_model_output,
                    include_attachment_metadata=include_attachment_metadata,
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
    )
    return BenchmarkRunReport(metadata, summary, records, sections)


def build_scoreboard_report(
    entries: Sequence[ScoreboardSource] | None = None,
    *,
    batches: Sequence[ScoreboardImportBatch] | None = None,
    catalog: CatalogService | None = None,
    filters: ScoreboardReportFilters = ScoreboardReportFilters(),
    title: str = "Historical Scoreboard Report",
    generated_at: str | None = None,
) -> ScoreboardReport:
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
) -> ModelLeaderboardReport:
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
    )
    return ModelLeaderboardReport(metadata, ranked)


class ReportingService:
    """Application-facing facade over the UI-independent reporting engine."""

    def __init__(self, service: BenchmarkService, catalog: CatalogService | None = None):
        self.service = service
        self.catalog = catalog or service.catalog

    def select_benchmark_runs(self, runs: Sequence[BenchmarkSource] | None = None) -> tuple[BenchmarkRunAggregate, ...]:
        return _run_source(runs, self.service, self.catalog)

    def select_scoreboard_entries(
        self,
        entries: Sequence[ScoreboardSource] | None = None,
        *,
        batches: Sequence[ScoreboardImportBatch] | None = None,
    ) -> tuple[ScoreboardEntryAggregate, ...]:
        return _scoreboard_source(entries, batches, self.catalog)

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
        )

    def scoreboard_report(
        self,
        entries: Sequence[ScoreboardSource] | None = None,
        *,
        batches: Sequence[ScoreboardImportBatch] | None = None,
        filters: ScoreboardReportFilters = ScoreboardReportFilters(),
        title: str = "Historical Scoreboard Report",
        generated_at: str | None = None,
    ) -> ScoreboardReport:
        return build_scoreboard_report(
            entries,
            batches=batches,
            catalog=self.catalog,
            filters=filters,
            title=title,
            generated_at=generated_at,
        )

    def model_leaderboard(
        self,
        runs: Sequence[BenchmarkSource] | None = None,
        *,
        filters: BenchmarkReportFilters = BenchmarkReportFilters(),
        title: str = "Model Leaderboard",
        generated_at: str | None = None,
    ) -> ModelLeaderboardReport:
        return build_model_leaderboard(
            runs,
            service=self.service,
            catalog=self.catalog,
            filters=filters,
            title=title,
            generated_at=generated_at,
        )


def _render_header(metadata: ReportMetadata) -> list[str]:
    return [
        f"# {metadata.title}",
        "",
        f"- Generated at: {metadata.generated_at}",
        f"- Report type: {metadata.report_type}",
        f"- Source record type: {metadata.source_record_type}",
        f"- Selection: {metadata.selection_summary}",
        f"- Record count: {metadata.record_count}",
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


def render_benchmark_run_markdown(report: BenchmarkRunReport) -> str:
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


def render_model_leaderboard_markdown(report: ModelLeaderboardReport, *, include_model_details: bool = False) -> str:
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


def render_markdown(report: ReportDocument) -> str:
    if isinstance(report, BenchmarkRunReport):
        return render_benchmark_run_markdown(report)
    if isinstance(report, ScoreboardReport):
        return render_scoreboard_markdown(report)
    return render_model_leaderboard_markdown(report)


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
        content = report if isinstance(report, str) else render_markdown(report)
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
ReportSummary = ScoreStatistics
generate_benchmark_run_report = build_benchmark_run_report
generate_scoreboard_report = build_scoreboard_report
generate_model_leaderboard = build_model_leaderboard
render_benchmark_report_markdown = render_benchmark_run_markdown
render_leaderboard_markdown = render_model_leaderboard_markdown
write_report_markdown = write_markdown_report


__all__ = (
    "AttachmentReportMetadata",
    "BenchmarkModelSection",
    "BenchmarkReportFilters",
    "BenchmarkReportSummary",
    "BenchmarkRunAggregate",
    "BenchmarkRunReport",
    "BenchmarkRunReportItem",
    "HardwareReportSummary",
    "DetailedBenchmarkReport",
    "HistoricalScoreboardReport",
    "ModelLeaderboardEntry",
    "ModelLeaderboardReport",
    "ModelReportSummary",
    "PromptReportSummary",
    "ReportDocument",
    "ReportMetadata",
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
    "SessionReportSummary",
    "build_benchmark_run_report",
    "build_model_leaderboard",
    "build_scoreboard_report",
    "generate_benchmark_run_report",
    "generate_model_leaderboard",
    "generate_scoreboard_report",
    "render_benchmark_report_markdown",
    "render_benchmark_run_markdown",
    "render_combined_markdown",
    "render_leaderboard_markdown",
    "render_markdown",
    "render_model_leaderboard_markdown",
    "render_scoreboard_markdown",
    "write_markdown_report",
    "write_report_markdown",
)
