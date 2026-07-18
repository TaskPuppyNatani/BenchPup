from __future__ import annotations

import csv
from html import escape
import json
from pathlib import Path

from .domain import now
from .reporting import (
    BenchmarkReportFilters,
    ScoreboardReportFilters,
    build_benchmark_run_report,
    build_model_leaderboard,
    build_scoreboard_report,
    render_combined_markdown,
    render_model_leaderboard_markdown,
    render_scoreboard_markdown,
    write_markdown_report,
)
from .services import BenchmarkService, CatalogService


def export_benchmark_runs_csv(service: BenchmarkService, path: str | Path) -> Path:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for run in service.runs.list():
        run_id = run.id
        assert run_id is not None, "Persisted benchmark run is missing its ID"
        _, score, _ = service.get_run(run_id)
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
    """Export historical scoreboard entries as an interactive, offline HTML viewer."""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    batches = {batch.id: batch for batch in catalog.scoreboard_import_batches.list()}
    entries = catalog.scoreboard_entries.list()
    templates = catalog.prompt_templates.list()

    def text(value: object) -> str:
        return escape("-" if value is None or value == "" else str(value), quote=True)

    def numeric(value: object) -> float | None:
        return float(value) if isinstance(value, (int, float)) else None

    records = []
    for entry in entries:
        batch = batches.get(entry.import_batch_id)
        batch_name = batch.name if batch else "Unbatched scoreboard entries"
        records.append({
            "model_name": entry.model_name, "score": entry.score,
            "hallucination_level": entry.hallucination_level, "reliability_score": entry.reliability_score,
            "temperature": entry.temperature, "context_length": entry.context_length,
            "tokens_per_second": entry.tokens_per_second, "verdict": entry.verdict,
            "notes": entry.notes, "batch": batch_name, "imported_at": entry.imported_at,
            "metadata": {
                "Review quality": entry.review_quality, "Consistency": entry.consistency,
                "Experts": entry.moe_experts, "Additional notes": entry.notes_extra,
                "Source file": entry.source_file, "Entry ID": entry.id,
            },
        })

    def options(field: str) -> str:
        values = sorted({str(record[field]) for record in records if record[field] not in (None, "")})
        return "".join(f'<option value="{text(value)}">{text(value)}</option>' for value in values)

    scores: list[tuple[float, str]] = []
    speeds: list[tuple[float, str]] = []
    for record in records:
        model_name = str(record["model_name"])
        score = numeric(record["score"])
        speed = numeric(record["tokens_per_second"])
        if score is not None:
            scores.append((score, model_name))
        if speed is not None:
            speeds.append((speed, model_name))
    highest = max(scores, default=(None, "-"))
    fastest = max(speeds, default=(None, "-"))
    average_score = sum(score for score, _ in scores) / len(scores) if scores else None
    batch_count = len({record["batch"] for record in records if record["batch"] != "Unbatched scoreboard entries"})

    rows = []
    for record in records:
        search_text = " ".join(str(record[field] or "") for field in ("model_name", "verdict", "notes", "batch"))
        metadata = "".join(f"<dt>{text(label)}</dt><dd>{text(value)}</dd>" for label, value in record["metadata"].items())
        rows.append(
            f'<tr class="data-row" data-search="{text(search_text).lower()}" '
            f'data-hallucination="{text(record["hallucination_level"])}" '
            f'data-reliability="{text(record["reliability_score"])}" '
            f'data-verdict="{text(record["verdict"])}" data-batch="{text(record["batch"])}" '
            f'data-model="{text(record["model_name"])}" data-score="{text(record["score"])}" '
            f'data-temperature="{text(record["temperature"])}" data-context="{text(record["context_length"])}" '
            f'data-tokens="{text(record["tokens_per_second"])}" data-imported="{text(record["imported_at"])}">'
            f'<td><button class="row-toggle" type="button" aria-expanded="false">{text(record["model_name"])}</button></td>'
            f'<td>{text(record["score"])}</td><td>{text(record["hallucination_level"])}</td>'
            f'<td>{text(record["reliability_score"])}</td><td>{text(record["temperature"])}</td>'
            f'<td>{text(record["context_length"])}</td><td>{text(record["tokens_per_second"])}</td>'
            f'<td>{text(record["verdict"])}</td><td class="notes">{text(record["notes"])}</td>'
            f'<td>{text(record["batch"])}</td><td>{text(record["imported_at"])}</td></tr>'
            f'<tr class="details-row" hidden><td colspan="11"><div class="details">'
            f'<strong>Full notes</strong><div class="full-notes">{text(record["notes"])}</div><dl>{metadata}</dl>'
            f'</div></td></tr>'
        )

    table_body = "".join(rows) or '<tr><td colspan="11" class="empty">No historical scoreboard entries found.</td></tr>'
    template_types = sorted({template.benchmark_type for template in templates})
    template_rows = []
    for template in templates:
        active = "Active" if template.is_active else "Inactive"
        search_text = " ".join((template.name, template.version, template.benchmark_type, template.notes, template.prompt_text))
        template_rows.append(
            f'<tr class="template-data-row" data-template-search="{text(search_text).lower()}" '
            f'data-template-type="{text(template.benchmark_type)}" data-template-active="{text(active)}">'
            f'<td><button class="template-row-toggle" type="button" aria-expanded="false">{text(template.name)}</button></td>'
            f'<td>{text(template.version)}</td><td>{text(template.benchmark_type)}</td><td>{text(active)}</td>'
            f'<td>{text(len(template.prompt_text))}</td><td>{text(len(template.prompt_text.splitlines()))}</td>'
            f'<td class="notes">{text(template.notes)}</td></tr>'
            f'<tr class="template-details-row" hidden><td colspan="7"><div class="details">'
            f'<strong>Prompt text</strong><pre class="prompt-text">{text(template.prompt_text)}</pre><dl>'
            f'<dt>SHA-256</dt><dd>{text(template.prompt_hash)}</dd><dt>Created</dt><dd>{text(template.created_at)}</dd>'
            f'<dt>Updated</dt><dd>{text(template.updated_at)}</dd><dt>Benchmark type</dt><dd>{text(template.benchmark_type)}</dd>'
            f'<dt>Active</dt><dd>{text(active)}</dd></dl></div></td></tr>'
        )
    template_table_body = "".join(template_rows) or '<tr><td colspan="7" class="empty">No prompt templates found.</td></tr>'
    template_type_options = "".join(f'<option value="{text(value)}">{text(value)}</option>' for value in template_types)
    active_template_count = sum(template.is_active for template in templates)
    unique_prompt_hashes = len({template.prompt_hash for template in templates})
    generated = text(now())
    path.write_text("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BenchPup Scoreboard Report</title>
<style>
 :root { color-scheme: dark; } body { margin: 0; padding: 2rem; background: #111827; color: #e5e7eb; font: 15px/1.45 system-ui, sans-serif; }
main { max-width: 1800px; margin: auto; } h1 { margin: 0 0 .25rem; } .generated, .empty { color: #9ca3af; } .tabs { display: flex; gap: .5rem; margin: 1rem 0; } .tabs button { background: #1d4ed8; color: white; border: 0; border-radius: .25rem; padding: .5rem .8rem; cursor: pointer; } .tabs button[aria-selected="false"] { background: #374151; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(165px, 1fr)); gap: .8rem; margin: 1.5rem 0; }
.card, .controls { background: #1f2937; border: 1px solid #374151; border-radius: .5rem; padding: 1rem; }
.card .label { color: #9ca3af; font-size: .85rem; } .card .value { font-size: 1.25rem; font-weight: 650; overflow-wrap: anywhere; }
.controls { display: flex; flex-wrap: wrap; gap: .8rem; align-items: end; } label { display: grid; gap: .25rem; color: #cbd5e1; }
input, select { background: #111827; color: #e5e7eb; border: 1px solid #4b5563; border-radius: .25rem; padding: .5rem; }
table { width: 100%; border-collapse: collapse; background: #1f2937; margin-top: 1rem; }
th, td { padding: .65rem .75rem; border: 1px solid #374151; text-align: left; vertical-align: top; }
th { position: sticky; top: 0; background: #1d4ed8; color: white; white-space: nowrap; }
th button, .row-toggle { all: unset; cursor: pointer; color: inherit; font-weight: inherit; } th button::after { content: " ↕"; font-size: .8em; }
tr.data-row:nth-of-type(4n + 1), tr.template-data-row:nth-of-type(4n + 1) { background: #243044; } .notes, .full-notes { white-space: pre-wrap; overflow-wrap: anywhere; min-width: 16rem; }
.prompt-text { white-space: pre-wrap; overflow-wrap: anywhere; background: #111827; border: 1px solid #374151; border-radius: .25rem; padding: .75rem; max-height: 34rem; overflow: auto; }
.details { padding: .5rem; } dl { display: grid; grid-template-columns: max-content 1fr; gap: .3rem .8rem; } dt { color: #93c5fd; } dd { margin: 0; overflow-wrap: anywhere; }
@media (max-width: 800px) { body { padding: 1rem; } table { display: block; overflow-x: auto; } }
</style>
</head>
<body><main>
<h1>BenchPup Scoreboard Report</h1>
<p class="generated">Generated at """ + generated + """. Historical scoreboard imports and stored prompt templates; benchmark runs are not included.</p>
<nav class="tabs" aria-label="Report sections"><button type="button" data-tab="scoreboard-section" aria-selected="true">Scoreboard</button><button type="button" data-tab="prompt-templates-section" aria-selected="false">Prompt Templates</button></nav>
<section id="scoreboard-section" class="report-section">
<section class="cards">
<div class="card"><div class="label">Total entries</div><div class="value">""" + text(len(records)) + """</div></div>
<div class="card"><div class="label">Import batches</div><div class="value">""" + text(batch_count) + """</div></div>
<div class="card"><div class="label">Highest score</div><div class="value">""" + text(f"{highest[0]} — {highest[1]}" if highest[0] is not None else "-") + """</div></div>
<div class="card"><div class="label">Fastest model</div><div class="value">""" + text(f"{fastest[0]} tok/s — {fastest[1]}" if fastest[0] is not None else "-") + """</div></div>
<div class="card"><div class="label">Average score</div><div class="value">""" + text(f"{average_score:.2f}" if average_score is not None else "-") + """</div></div>
</section>
<section class="controls" aria-label="Scoreboard filters">
<label>Search<input id="search" type="search" placeholder="Model, verdict, notes, or batch"></label>
<label>Hallucination<select id="hallucination"><option value="">All</option>""" + options("hallucination_level") + """</select></label>
<label>Reliability<select id="reliability"><option value="">All</option>""" + options("reliability_score") + """</select></label>
<label>Verdict<select id="verdict"><option value="">All</option>""" + options("verdict") + """</select></label>
<label>Import batch<select id="batch"><option value="">All</option>""" + options("batch") + """</select></label>
</section>
<table><thead><tr>
<th><button data-sort="model">Model</button></th><th><button data-sort="score">Score</button></th><th>Hallucination</th><th>Reliability</th>
<th><button data-sort="temperature">Temperature</button></th><th><button data-sort="context">Context</button></th>
<th><button data-sort="tokens">tok/s</button></th><th><button data-sort="verdict">Verdict</button></th><th>Notes</th><th>Batch</th>
<th><button data-sort="imported">Imported At</button></th></tr></thead><tbody id="scoreboard-rows">""" + table_body + """</tbody></table>
</section>
<section id="prompt-templates-section" class="report-section" hidden>
<section class="cards">
<div class="card"><div class="label">Total prompt templates</div><div class="value">""" + text(len(templates)) + """</div></div>
<div class="card"><div class="label">Active templates</div><div class="value">""" + text(active_template_count) + """</div></div>
<div class="card"><div class="label">Benchmark types represented</div><div class="value">""" + text(len(template_types)) + """</div></div>
<div class="card"><div class="label">Unique prompt hashes</div><div class="value">""" + text(unique_prompt_hashes) + """</div></div>
</section>
<section class="controls" aria-label="Prompt template filters">
<label>Search prompts<input id="template-search" type="search" placeholder="Name, notes, type, or prompt text"></label>
<label>Benchmark type<select id="template-type"><option value="">All</option>""" + template_type_options + """</select></label>
<label>Active state<select id="template-active"><option value="">All</option><option value="Active">Active</option><option value="Inactive">Inactive</option></select></label>
</section>
<table><thead><tr><th>Name</th><th>Version</th><th>Benchmark type</th><th>Active</th><th>Character count</th><th>Line count</th><th>Notes</th></tr></thead><tbody id="prompt-template-rows">""" + template_table_body + """</tbody></table>
</section>
</main><script>
const tbody = document.getElementById('scoreboard-rows');
const controls = ['hallucination', 'reliability', 'verdict', 'batch'].map(id => document.getElementById(id));
function applyFilters() {
  const search = document.getElementById('search').value.trim().toLowerCase();
  for (const row of tbody.querySelectorAll('.data-row')) {
    const visible = (!search || row.dataset.search.includes(search)) &&
      controls.every(control => !control.value || row.dataset[control.id] === control.value);
    row.hidden = !visible; row.nextElementSibling.hidden = true;
    row.querySelector('.row-toggle')?.setAttribute('aria-expanded', 'false');
  }
}
document.getElementById('search').addEventListener('input', applyFilters);
controls.forEach(control => control.addEventListener('change', applyFilters));
tbody.addEventListener('click', event => {
  const toggle = event.target.closest('.row-toggle'); if (!toggle) return;
  const details = toggle.closest('.data-row').nextElementSibling;
  details.hidden = !details.hidden; toggle.setAttribute('aria-expanded', String(!details.hidden));
});
let sortDirection = 1;
document.querySelectorAll('[data-sort]').forEach(button => button.addEventListener('click', () => {
  const field = button.dataset.sort;
  const rows = [...tbody.querySelectorAll('.data-row')];
  rows.sort((a, b) => {
    const left = a.dataset[field] || '', right = b.dataset[field] || '';
    const leftNumber = Number(left), rightNumber = Number(right);
    const compare = Number.isFinite(leftNumber) && Number.isFinite(rightNumber) && left !== '' && right !== ''
      ? leftNumber - rightNumber : left.localeCompare(right, undefined, { numeric: true });
    return compare * sortDirection;
  });
  sortDirection *= -1;
  rows.forEach(row => { const details = row.nextElementSibling; tbody.append(row, details); });
}));
const templateBody = document.getElementById('prompt-template-rows');
function applyTemplateFilters() {
  const search = document.getElementById('template-search').value.trim().toLowerCase();
  const type = document.getElementById('template-type').value;
  const active = document.getElementById('template-active').value;
  for (const row of templateBody.querySelectorAll('.template-data-row')) {
    const visible = (!search || row.dataset.templateSearch.includes(search)) &&
      (!type || row.dataset.templateType === type) && (!active || row.dataset.templateActive === active);
    row.hidden = !visible; row.nextElementSibling.hidden = true;
    row.querySelector('.template-row-toggle')?.setAttribute('aria-expanded', 'false');
  }
}
['template-search', 'template-type', 'template-active'].forEach(id => document.getElementById(id).addEventListener(id === 'template-search' ? 'input' : 'change', applyTemplateFilters));
templateBody.addEventListener('click', event => {
  const toggle = event.target.closest('.template-row-toggle'); if (!toggle) return;
  const details = toggle.closest('.template-data-row').nextElementSibling;
  details.hidden = !details.hidden; toggle.setAttribute('aria-expanded', String(!details.hidden));
});
document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.report-section').forEach(section => { section.hidden = section.id !== button.dataset.tab; });
  document.querySelectorAll('[data-tab]').forEach(tab => tab.setAttribute('aria-selected', String(tab === button)));
}));
</script></body></html>""", encoding="utf-8")
    return path


def export_jsonl_training_data(service: BenchmarkService, path: str | Path) -> Path:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        for run in service.runs.list():
            run_id = run.id
            assert run_id is not None, "Persisted benchmark run is missing its ID"
            _, score, _ = service.get_run(run_id)
            output.write(json.dumps({"instruction": "Review this model output and evaluate its quality.", "input": {"model": run.model_snapshot.get("model_name", ""), "benchmark_file": run.benchmark_snapshot.get("benchmark_file", ""), "prompt": run.prompt_text, "raw_model_output": run.raw_model_output}, "response": {"overall": score.overall_score if score else None, "verdict": score.verdict if score else ""}}) + "\n")
    return path


def export_combined_markdown(service: BenchmarkService, catalog: CatalogService, path: str | Path) -> Path:
    benchmark_report = build_benchmark_run_report(service=service, catalog=catalog)
    scoreboard_report = build_scoreboard_report(catalog=catalog)
    result = write_markdown_report(render_combined_markdown(benchmark_report, scoreboard_report), path, overwrite=True)
    if not result.succeeded:
        raise OSError(result.details or result.message)
    return result.path


def export_benchmark_runs_markdown(
    service: BenchmarkService,
    path: str | Path,
    *,
    filters: BenchmarkReportFilters = BenchmarkReportFilters(),
    include_prompt_text: bool = False,
    include_raw_model_output: bool = False,
    include_attachment_metadata: bool = False,
) -> Path:
    """Export the structured detailed BenchmarkRun report as Markdown."""

    report = build_benchmark_run_report(
        service=service,
        filters=filters,
        include_prompt_text=include_prompt_text,
        include_raw_model_output=include_raw_model_output,
        include_attachment_metadata=include_attachment_metadata,
    )
    result = write_markdown_report(report, path, overwrite=True)
    if not result.succeeded:
        raise OSError(result.details or result.message)
    return result.path


def export_scoreboard_markdown(
    catalog: CatalogService,
    path: str | Path,
    *,
    filters: ScoreboardReportFilters = ScoreboardReportFilters(),
) -> Path:
    """Export the structured historical ScoreboardEntry report as Markdown."""

    report = build_scoreboard_report(catalog=catalog, filters=filters)
    result = write_markdown_report(render_scoreboard_markdown(report), path, overwrite=True)
    if not result.succeeded:
        raise OSError(result.details or result.message)
    return result.path


def export_model_leaderboard_markdown(
    service: BenchmarkService,
    path: str | Path,
    *,
    filters: BenchmarkReportFilters = BenchmarkReportFilters(),
    include_model_details: bool = False,
) -> Path:
    """Export the model leaderboard foundation as Markdown."""

    report = build_model_leaderboard(service=service, filters=filters)
    result = write_markdown_report(
        render_model_leaderboard_markdown(report, include_model_details=include_model_details),
        path,
        overwrite=True,
    )
    if not result.succeeded:
        raise OSError(result.details or result.message)
    return result.path


def _legacy_combined_markdown(service: BenchmarkService, catalog: CatalogService, path: str | Path) -> Path:
    path = Path(path)
    lines: list[str] = []
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
