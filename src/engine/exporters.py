from __future__ import annotations

import csv
import json
from pathlib import Path

from .services import BenchmarkService, CatalogService


def export_benchmark_runs_csv(service: BenchmarkService, path: str | Path) -> Path:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for run in service.runs.list():
        _, score, _ = service.get_run(run.id)
        rows.append({**run.model_snapshot, **run.benchmark_snapshot, "prompt_name": run.prompt_name,
                     "prompt_text": run.prompt_text, "raw_model_output": run.raw_model_output,
                     **({key: value for key, value in score.__dict__.items() if key not in {"id", "run_id"}} if score else {})})
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    return path


def export_scoreboard_csv(catalog: CatalogService, path: str | Path) -> Path:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    batches = {batch.id: batch for batch in catalog.scoreboard_import_batches.list()}
    rows = []
    for entry in catalog.scoreboard_entries.list():
        batch = batches.get(entry.import_batch_id)
        row = {key: value for key, value in entry.__dict__.items() if key not in {"id", "is_deleted"}}
        row["batch_name"] = batch.name if batch else ""
        row["batch_imported_at"] = batch.imported_at if batch else entry.imported_at
        rows.append(row)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    return path


def export_jsonl_training_data(service: BenchmarkService, path: str | Path) -> Path:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        for run in service.runs.list():
            _, score, _ = service.get_run(run.id)
            output.write(json.dumps({"instruction": "Review this model output and evaluate its quality.", "input": {"model": run.model_snapshot.get("model_name", ""), "benchmark_file": run.benchmark_snapshot.get("benchmark_file", ""), "prompt": run.prompt_text, "raw_model_output": run.raw_model_output}, "response": {"overall": score.overall_score if score else None, "verdict": score.verdict if score else ""}}) + "\n")
    return path


def export_combined_markdown(service: BenchmarkService, catalog: CatalogService, path: str | Path) -> Path:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# BenchPup report", "", "## Benchmark runs", ""]
    for run in service.runs.list(): lines.append(f"- {run.model_snapshot.get('model_name', 'Unknown')} — {run.benchmark_snapshot.get('benchmark_file', 'custom')}")
    lines += ["", "## Scoreboard", ""]
    batches = {batch.id: batch for batch in catalog.scoreboard_import_batches.list()}
    grouped: dict[int | None, list] = {}
    for entry in catalog.scoreboard_entries.list(): grouped.setdefault(entry.import_batch_id, []).append(entry)
    for batch_id, entries in grouped.items():
        batch = batches.get(batch_id)
        lines += [f"### {batch.name if batch else 'Unbatched scoreboard entries'}", ""]
        for entry in entries: lines.append(f"- {entry.model_name} | score: {entry.score if entry.score is not None else '-'}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
    for entry in catalog.scoreboard_entries.list(): lines.append(f"- {entry.model_name} — score: {entry.score if entry.score is not None else '-'}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
