from __future__ import annotations

import csv
from html import escape
import json
from pathlib import Path

from .domain import now
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


def export_scoreboard_html(catalog: CatalogService, path: str | Path) -> Path:
    """Export historical scoreboard entries as a standalone, offline HTML report."""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    batches = {batch.id: batch for batch in catalog.scoreboard_import_batches.list()}
    grouped: dict[int | None, list] = {}
    for entry in catalog.scoreboard_entries.list():
        grouped.setdefault(entry.import_batch_id, []).append(entry)

    columns = (
        ("Model", "model_name"), ("Score", "score"), ("Hallucination", "hallucination_level"),
        ("Reliability", "reliability_score"), ("Temperature", "temperature"),
        ("Context", "context_length"), ("tok/s", "tokens_per_second"), ("Verdict", "verdict"),
        ("Notes", "notes"), ("Batch", None), ("Imported At", "imported_at"),
    )

    def text(value: object) -> str:
        return escape("-" if value is None or value == "" else str(value), quote=True)

    header = "".join(f"<th>{escape(label)}</th>" for label, _ in columns)
    sections = []
    for batch_id, entries in grouped.items():
        batch = batches.get(batch_id)
        batch_name = batch.name if batch else "Unbatched scoreboard entries"
        rows = []
        for entry in entries:
            cells = []
            for _, field in columns:
                value = batch_name if field is None else getattr(entry, field)
                css_class = " class=\"notes\"" if field == "notes" else ""
                cells.append(f"<td{css_class}>{text(value)}</td>")
            rows.append("<tr>" + "".join(cells) + "</tr>")
        sections.append(
            f"<section><h2>{text(batch_name)}</h2><table><thead><tr>{header}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></section>"
        )

    body = "".join(sections) or "<p class=\"empty\">No historical scoreboard entries found.</p>"
    path.write_text(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BenchPup Scoreboard Report</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin: 0; padding: 2rem; background: #111827; color: #e5e7eb; font: 15px/1.45 system-ui, sans-serif; }}
main {{ max-width: 1600px; margin: auto; }}
h1 {{ margin: 0 0 .25rem; }} .generated {{ color: #9ca3af; margin-top: 0; }}
section {{ margin-top: 2rem; }} h2 {{ color: #93c5fd; font-size: 1.15rem; }}
table {{ width: 100%; border-collapse: collapse; background: #1f2937; }}
th, td {{ padding: .65rem .75rem; border: 1px solid #374151; text-align: left; vertical-align: top; }}
th {{ position: sticky; top: 0; background: #1d4ed8; color: white; white-space: nowrap; }}
tr:nth-child(even) {{ background: #243044; }} .notes {{ white-space: pre-wrap; overflow-wrap: anywhere; min-width: 18rem; }}
.empty {{ color: #9ca3af; }}
@media (max-width: 800px) {{ body {{ padding: 1rem; }} table {{ display: block; overflow-x: auto; }} }}
</style>
</head>
<body><main>
<h1>BenchPup Scoreboard Report</h1>
<p class="generated">Generated at {text(now())}. Historical scoreboard imports only; benchmark runs are not included.</p>
{body}
</main></body></html>
""", encoding="utf-8")
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
