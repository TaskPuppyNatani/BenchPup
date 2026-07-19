from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

LEVELS = ("Low", "Low-Medium", "Medium", "Medium-High", "High")
BENCHMARK_TYPES = ("code_review", "code_generation", "revision", "review_the_review")
ATTACHMENT_TYPES = ("screenshot", "raw_text", "log", "other")


def serialize_utc_timestamp(value: datetime) -> str:
    """Serialize a timezone-aware instant as canonical UTC ISO-8601 text."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def prompt_hash_for(prompt_text: str) -> str:
    """Return the authoritative SHA-256 hash for exact UTF-8 prompt text."""

    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()


def now() -> str:
    return serialize_utc_timestamp(datetime.now(timezone.utc))


def require(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} is required")


def score(value: float | None, name: str) -> None:
    if value is not None and not 0 <= value <= 5:
        raise ValueError(f"{name} must be between 0 and 5")


def _optional_timestamp(value: str | None, name: str) -> datetime | None:
    """Validate an optional ISO timestamp without changing its stored text."""

    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError(f"{name} must be a valid ISO 8601 timestamp") from None


@dataclass
class BenchmarkSession:
    title: str
    description: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    notes: str = ""
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    is_deleted: bool = False
    id: int | None = None

    def validate(self) -> None:
        require(self.title, "title")
        started = _optional_timestamp(self.started_at, "started_at")
        completed = _optional_timestamp(self.completed_at, "completed_at")
        if started is not None and completed is not None:
            if (started.tzinfo is None) != (completed.tzinfo is None):
                raise ValueError("started_at and completed_at must use matching timezone awareness")
            if completed < started:
                raise ValueError("completed_at must be at or after started_at")


@dataclass
class ModelProfile:
    name: str
    model_name: str
    model_family: str = ""
    model_size: str = ""
    quantization: str = ""
    backend: str = "Other"
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    thinking_enabled: bool = False
    flash_attention: bool = False
    moe_experts: str = ""
    context_length: int | None = None
    tokens_per_second: float | None = None
    is_default: bool = False
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    id: int | None = None

    def validate(self) -> None:
        require(self.name, "name"); require(self.model_name, "model_name")
        for value, name in ((self.temperature, "temperature"), (self.top_p, "top_p"), (self.min_p, "min_p")):
            if value is not None and not 0 <= value <= 1: raise ValueError(f"{name} must be between 0 and 1")
        if self.top_k is not None and self.top_k < 0: raise ValueError("top_k must be non-negative")
        if self.context_length is not None and self.context_length <= 0: raise ValueError("context_length must be positive")
        if self.tokens_per_second is not None and self.tokens_per_second < 0: raise ValueError("tokens_per_second must be non-negative")


@dataclass
class HardwareProfile:
    name: str
    computer_name: str = ""
    cpu: str = ""
    gpu: str = ""
    vram_gb: float | None = None
    ram_gb: float | None = None
    operating_system: str = ""
    backend_versions: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    import_source: str = ""
    imported_at: str | None = None
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    id: int | None = None

    def validate(self) -> None:
        require(self.name, "name")
        if self.vram_gb is not None and self.vram_gb < 0: raise ValueError("vram_gb must be non-negative")
        if self.ram_gb is not None and self.ram_gb < 0: raise ValueError("ram_gb must be non-negative")


@dataclass
class BenchmarkDefinition:
    name: str
    file_path: str
    benchmark_type: str
    default_prompt: str = ""
    tags: str = ""
    is_active: bool = True
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    id: int | None = None

    def validate(self) -> None:
        require(self.name, "name"); require(self.file_path, "file_path")
        if self.benchmark_type not in BENCHMARK_TYPES: raise ValueError("invalid benchmark_type")


@dataclass
class PromptTemplate:
    name: str
    version: str
    prompt_text: str
    prompt_hash: str
    benchmark_type: str
    notes: str = ""
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    is_active: bool = True
    id: int | None = None

    def validate(self) -> None:
        require(self.name, "name"); require(self.version, "version")
        require(self.prompt_text, "prompt_text"); require(self.prompt_hash, "prompt_hash")
        if self.benchmark_type not in BENCHMARK_TYPES: raise ValueError("invalid benchmark_type")
        expected = prompt_hash_for(self.prompt_text)
        if self.prompt_hash != expected: raise ValueError("prompt_hash does not match prompt_text")


@dataclass
class BenchmarkRun:
    raw_model_output: str
    prompt_name: str = ""
    prompt_text: str = ""
    session_id: int | None = None
    model_profile_id: int | None = None
    benchmark_definition_id: int | None = None
    prompt_template_id: int | None = None
    hardware_profile_id: int | None = None
    model_snapshot: dict[str, Any] = field(default_factory=dict)
    benchmark_snapshot: dict[str, Any] = field(default_factory=dict)
    prompt_snapshot: dict[str, Any] = field(default_factory=dict)
    hardware_snapshot: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    is_deleted: bool = False
    id: int | None = None

    def validate(self) -> None:
        if not isinstance(self.raw_model_output, str): raise ValueError("raw_model_output must be text")
        for value, name in ((self.model_snapshot, "model_snapshot"), (self.benchmark_snapshot, "benchmark_snapshot"), (self.prompt_snapshot, "prompt_snapshot"), (self.hardware_snapshot, "hardware_snapshot")):
            if not isinstance(value, dict): raise ValueError(f"{name} must be an object")


@dataclass
class ReviewScore:
    run_id: int
    accuracy_score: float | None = None
    hallucination_level: str = "Medium"
    reliability_level: str = "Medium"
    depth_score: float | None = None
    signal_noise_score: float | None = None
    actionability_score: float | None = None
    seniority_score: float | None = None
    overall_score: float | None = None
    strengths: str = ""
    weaknesses: str = ""
    verdict: str = ""
    notes: str = ""
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    id: int | None = None

    def validate(self) -> None:
        if self.run_id <= 0: raise ValueError("run_id must be positive")
        for name in ("accuracy_score", "depth_score", "signal_noise_score", "actionability_score", "seniority_score", "overall_score"):
            score(getattr(self, name), name)
        if self.hallucination_level not in LEVELS: raise ValueError("invalid hallucination_level")
        if self.reliability_level not in LEVELS: raise ValueError("invalid reliability_level")


@dataclass
class ScoreboardImportBatch:
    name: str
    source_file: str
    imported_at: str = field(default_factory=now)
    notes: str = ""
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    is_deleted: bool = False
    id: int | None = None

    def validate(self) -> None:
        require(self.name, "name"); require(self.source_file, "source_file")


@dataclass
class ScoreboardEntry:
    model_name: str
    temperature: float | None = None
    moe_experts: str = ""
    context_length: int | None = None
    tokens_per_second: float | None = None
    review_quality: str = ""
    score: float | None = None
    hallucination_level: str = ""
    consistency: str = ""
    reliability_score: str = ""
    verdict: str = ""
    notes: str = ""
    notes_extra: str = ""
    source_file: str = ""
    imported_at: str = field(default_factory=now)
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    is_deleted: bool = False
    import_batch_id: int | None = None
    id: int | None = None

    def validate(self) -> None:
        require(self.model_name, "model_name")
        for value, name in ((self.temperature, "temperature"), (self.tokens_per_second, "tokens_per_second"),
                            (self.score, "score")):
            if value is not None and value < 0: raise ValueError(f"{name} must be non-negative")
        if self.context_length is not None and self.context_length <= 0: raise ValueError("context_length must be positive")


@dataclass
class RunAttachment:
    run_id: int
    attachment_type: str
    file_path: str
    original_filename: str
    notes: str = ""
    created_at: str = field(default_factory=now)
    id: int | None = None

    def validate(self) -> None:
        if self.run_id <= 0: raise ValueError("run_id must be positive")
        if self.attachment_type not in ATTACHMENT_TYPES: raise ValueError("invalid attachment_type")
        require(self.file_path, "file_path"); require(self.original_filename, "original_filename")


@dataclass
class ExportProfile:
    name: str
    format: str
    field_selection: dict[str, Any] = field(default_factory=dict)
    filter_json: dict[str, Any] = field(default_factory=dict)
    destination: str = ""
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    id: int | None = None

    def validate(self) -> None: require(self.name, "name"); require(self.format, "format")
