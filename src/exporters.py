from __future__ import annotations

import csv
import json
from pathlib import Path

from models import BenchmarkRun


def export_csv(runs: list[BenchmarkRun], path: str | Path) -> Path:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(BenchmarkRun.__dataclass_fields__)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(run.to_dict() for run in runs)
    return path


def training_record(run: BenchmarkRun) -> dict:
    return {
        "instruction": "Review this model output and evaluate its quality.",
        "input": {
            "model": run.model_name,
            "benchmark_file": run.benchmark_file,
            "benchmark_type": run.benchmark_type,
            "prompt_name": run.prompt_name,
            "prompt": run.prompt_text,
            "raw_model_output": run.raw_model_output,
        },
        "response": {
            "accuracy": run.accuracy_score,
            "hallucination": run.hallucination_level,
            "reliability": run.reliability_level,
            "depth": run.depth_score,
            "signal_noise": run.signal_noise_score,
            "actionability": run.actionability_score,
            "seniority": run.seniority_score,
            "overall": run.overall_score,
            "strengths": run.strengths,
            "weaknesses": run.weaknesses,
            "verdict": run.verdict,
            "notes": run.notes,
        },
        "metadata": {
            "model_family": run.model_family,
            "model_size": run.model_size,
            "quantization": run.quantization,
            "backend": run.backend,
            "temperature": run.temperature,
            "top_p": run.top_p,
            "top_k": run.top_k,
            "min_p": run.min_p,
            "thinking_enabled": run.thinking_enabled,
            "flash_attention": run.flash_attention,
            "moe_experts": run.moe_experts,
            "context_length": run.context_length,
            "tokens_per_second": run.tokens_per_second,
            "recorded_at": run.created_at,
        },
    }


def export_jsonl(runs: list[BenchmarkRun], path: str | Path) -> Path:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        for run in runs:
            output.write(json.dumps(training_record(run)) + "\n")
    return path
