from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

LEVELS = ("Low", "Low-Medium", "Medium", "Medium-High", "High")
BENCHMARK_FILES = ("speech_server.py", "money.py", "main.js", "make_icon.py", "custom")
BENCHMARK_TYPES = ("code_review", "code_generation", "revision", "review_the_review")


@dataclass
class BenchmarkRun:
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
    benchmark_file: str = "custom"
    benchmark_type: str = "code_review"
    prompt_name: str = ""
    prompt_text: str = ""
    raw_model_output: str = ""
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
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    id: int | None = None

    def validate(self) -> None:
        if not self.model_name.strip():
            raise ValueError("model_name is required")
        if self.benchmark_type not in BENCHMARK_TYPES:
            raise ValueError(f"benchmark_type must be one of: {', '.join(BENCHMARK_TYPES)}")
        for name in ("hallucination_level", "reliability_level"):
            if getattr(self, name) not in LEVELS:
                raise ValueError(f"{name} must be one of: {', '.join(LEVELS)}")
        for name in ("accuracy_score", "depth_score", "signal_noise_score", "actionability_score", "seniority_score", "overall_score"):
            value = getattr(self, name)
            if value is not None and not 0 <= value <= 5:
                raise ValueError(f"{name} must be between 0 and 5")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
