from __future__ import annotations

import hashlib
import json
import logging
import csv
import os
import webbrowser
from dataclasses import replace
from pathlib import Path
from typing import Callable

from engine.database import EngineDatabase
from engine.domain import ATTACHMENT_TYPES, BENCHMARK_TYPES, LEVELS, BenchmarkDefinition, BenchmarkRun, BenchmarkSession, HardwareProfile, ModelProfile, PromptTemplate, ReviewScore, RunAttachment
from engine.services import BenchmarkService, CatalogService
from engine.importers import CsvImportService, MAPPING_FIELDS, SUMMARY_MAPPING_FIELDS
from engine.exporters import export_benchmark_runs_csv, export_combined_markdown, export_jsonl_training_data, export_scoreboard_csv, export_scoreboard_html
from engine.path_completion import install_path_completion, normalize_path, resolve_export_destination

BACK, CANCEL, MAIN = object(), object(), object()
BACK_WORDS = {"b", "back"}
CANCEL_WORDS = {"c", "cancel"}
QUIT_WORDS = {"q", "quit", "exit"}
APP_VERSION = "0.2 Alpha"
MENU_WIDTH = 56


class TerminalApp:
    """A forgiving terminal interface over the Phase 1 service layer."""
    def __init__(self, database_path: str | Path, input_fn: Callable[[str], str] = input, output_fn: Callable[[str], None] = print):
        database = EngineDatabase(database_path)
        database.migrate()
        self.catalog = CatalogService(database)
        self.benchmarks = BenchmarkService(database, self.catalog)
        self.importer = CsvImportService(self.benchmarks)
        self.input, self.output = input_fn, output_fn
        self.interactive_input = input_fn is input
        self.last_used: dict[str, int | None] = {"session": None, "model": None, "benchmark": None, "prompt": None, "hardware": None}
        log_path = Path(database_path).parent.parent / "logs" / "error.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(f"llm_benchmarker.{id(self)}")
        self.logger.setLevel(logging.ERROR)
        self.logger.addHandler(logging.FileHandler(log_path, encoding="utf-8"))

    @staticmethod
    def normalized(value: str) -> str:
        return value.strip().lower()

    def ask(self, label: str, *, navigation: bool = False, default: str | None = None) -> str | object:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            raw = self.input(f"{label}{suffix}: ")
        except KeyboardInterrupt:
            self.output("\nReturning to the main menu.")
            return MAIN
        value = raw.strip()
        command = value.lower()
        if navigation:
            if command in BACK_WORDS: return BACK
            if command in CANCEL_WORDS: return CANCEL
            if command in QUIT_WORDS: return MAIN
        return default if value == "" and default is not None else value

    def pause(self) -> None:
        try: self.input("Press Enter to continue...")
        except KeyboardInterrupt: self.output("")

    def prompt_path(self, label: str, *, must_exist: bool = False, extensions: tuple[str, ...] = (), default: str | None = None, preserve_trailing_separator: bool = False) -> str | object:
        """Prompt for a filesystem path while preserving non-interactive input behavior."""
        self.output("Tip: press Tab to autocomplete paths.")
        restore_completion = install_path_completion(extensions, debug=self.output) if self.interactive_input else lambda: None
        try:
            value = self.ask(label, navigation=True, default=default)
        finally:
            restore_completion()
        if value in (BACK, CANCEL, MAIN):
            return value
        raw_value = str(value)
        normalized = normalize_path(raw_value)
        if preserve_trailing_separator and raw_value.rstrip().endswith(("/", "\\")):
            normalized += os.sep
        suffixes = {extension.lower() for extension in extensions}
        if must_exist and not Path(normalized).is_file():
            self.output(f'Path not found or not a file: "{normalized}".')
            return None
        if must_exist and suffixes and Path(normalized).suffix.lower() not in suffixes:
            self.output(f'Expected a {"/".join(extensions)} file: "{normalized}".')
            return None
        return normalized

    def prepare_export_destination(self, destination: str, export_name: str) -> Path | None | object:
        defaults = {
            "Scoreboard HTML": ("scoreboard.html", ".html"),
            "Scoreboard CSV": ("scoreboard.csv", None),
            "Benchmark Runs CSV": ("benchmark_runs.csv", None),
            "JSONL training data": ("training_data.jsonl", None),
            "Markdown report": ("report.md", None),
        }
        default_filename, extension = defaults.get(export_name, ("report.md", None))
        output_path = resolve_export_destination(destination, default_filename=default_filename, extension=extension)
        if not output_path.parent.exists():
            create = self.yes_no(f'Parent directory "{output_path.parent}" does not exist. Create it?', navigation=True)
            if create in (BACK, CANCEL, MAIN):
                return create
            if create is not True:
                self.output("Export cancelled; parent directory was not created.")
                return None
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                self.output(f'Could not create parent directory "{output_path.parent}": {error}')
                return None
        self.output(f"Writing export to {output_path}")
        return output_path

    def ask_float(self, label: str, *, default: float | None = None, navigation: bool = False) -> float | None | object:
        while True:
            value = self.ask(label, navigation=navigation, default=str(default) if default is not None else None)
            if value in (BACK, CANCEL, MAIN): return value
            if value == "": return None
            try: return float(value)
            except (TypeError, ValueError): self.output(f'"{value}" is not a valid number. Please enter a number or leave it blank.')

    def ask_id(self, label: str = "Run ID", *, navigation: bool = True) -> int | object:
        while True:
            value = self.ask(label, navigation=navigation)
            if value in (BACK, CANCEL, MAIN): return value
            if isinstance(value, str) and value.isdigit() and int(value) > 0: return int(value)
            self.output(f'"{value}" is not a valid {label}. Please enter a numeric ID or B to go back.')

    def pick(self, label: str, choices: tuple[str, ...], default: str, *, navigation: bool = False) -> str | object:
        lookup = {item.lower(): item for item in choices}
        while True:
            value = self.ask(f"{label} [{'/'.join(choices)}]", navigation=navigation, default=default)
            if value in (BACK, CANCEL, MAIN): return value
            selected = lookup.get(str(value).lower())
            if selected: return selected
            self.output("Choose one of the listed values.")

    def yes_no(self, label: str, *, default: bool = False, navigation: bool = False) -> bool | object:
        hint = "Y/n" if default else "y/N"
        while True:
            value = self.ask(f"{label} [{hint}]", navigation=navigation)
            if value in (BACK, CANCEL, MAIN): return value
            command = str(value).lower()
            if not command: return default
            if command in {"y", "yes"}: return True
            if command in {"n", "no"}: return False
            self.output("Please answer yes or no.")

    def _form(self, fields: list[tuple[str, str, str | float | None, str]]) -> dict | object:
        """Collect simple text/number fields; navigation works from every prompt."""
        values = {}
        for name, label, default, kind in fields:
            value = self.ask_float(label, default=default, navigation=True) if kind == "float" else self.ask(label, navigation=True, default=default if isinstance(default, str) else None)
            if value in (BACK, CANCEL, MAIN): return value
            values[name] = value
        return values

    def choose_catalog(self, title: str, repository, create, key: str, *, optional: bool, current_id: int | None = None) -> int | None | object:
        while True:
            items = repository.list()
            self.output(f"\n{title}")
            for item in items: self.output(f"{item.id}) {item.name if hasattr(item, 'name') else item.title}")
            suggested = current_id or self.last_used[key]
            options = "N) Create new  B) Back  C) Cancel  Q) Main menu"
            if suggested: options += f"  Enter) Use #{suggested}"
            elif optional: options += "  Enter) None"
            value = self.ask(options, navigation=True)
            if value is BACK: return BACK
            if value is CANCEL: return CANCEL
            if value is MAIN: return MAIN
            if value == "" and suggested and repository.get(suggested): return suggested
            if value == "" and optional: return None
            if isinstance(value, str) and value.lower() == "n":
                created = create()
                if created in (BACK, CANCEL, MAIN): return created
                if created: self.last_used[key] = created.id; return created.id
                continue
            if isinstance(value, str) and value.isdigit() and repository.get(int(value)):
                self.last_used[key] = int(value); return int(value)
            self.output("Choose a listed numeric ID, N to create, or B to go back.")

    def create_session(self) -> BenchmarkSession | object | None:
        values = self._form([("title", "Session title", None, "text"), ("description", "Description", "", "text"), ("started_at", "Started at (ISO, optional)", "", "text"), ("completed_at", "Completed at (ISO, optional)", "", "text"), ("notes", "Notes", "", "text")])
        if values in (BACK, CANCEL, MAIN): return values
        if not values["title"]: self.output("A session title is required."); return None
        return self.catalog.sessions.create(BenchmarkSession(**values, started_at=values["started_at"] or None, completed_at=values["completed_at"] or None))

    def create_model_profile(self) -> ModelProfile | object | None:
        values = self._form([("name", "Profile name", None, "text"), ("model_name", "Model name", None, "text"), ("backend", "Backend", "Other", "text"), ("model_family", "Model family", "", "text"), ("model_size", "Model size", "", "text"), ("quantization", "Quantization", "", "text"), ("temperature", "Temperature", None, "float"), ("tokens_per_second", "Tokens per second", None, "float")])
        if values in (BACK, CANCEL, MAIN): return values
        if not values["name"] or not values["model_name"]: self.output("Profile name and model name are required."); return None
        return self.catalog.model_profiles.create(ModelProfile(**values))

    def create_definition(self) -> BenchmarkDefinition | object | None:
        name = self.ask("Benchmark name", navigation=True)
        if name in (BACK, CANCEL, MAIN): return name
        file_path = self.prompt_path("Benchmark file path")
        if file_path in (BACK, CANCEL, MAIN): return file_path
        if not name or not file_path: self.output("Benchmark name and file path are required."); return None
        benchmark_type = self.pick("Benchmark type", BENCHMARK_TYPES, "code_review", navigation=True)
        if benchmark_type in (BACK, CANCEL, MAIN): return benchmark_type
        rest = self._form([("default_prompt", "Default prompt (optional)", "", "text"), ("tags", "Tags (optional)", "", "text")])
        if rest in (BACK, CANCEL, MAIN): return rest
        return self.catalog.benchmark_definitions.create(BenchmarkDefinition(name=str(name), file_path=str(file_path), benchmark_type=benchmark_type, **rest))

    def create_prompt_template(self) -> PromptTemplate | object | None:
        values = self._form([("name", "Prompt template name", None, "text"), ("version", "Prompt version", None, "text"), ("prompt_text", "Prompt text", None, "text")])
        if values in (BACK, CANCEL, MAIN): return values
        if not all(values.values()): self.output("Template name, version, and prompt text are required."); return None
        benchmark_type = self.pick("Benchmark type", BENCHMARK_TYPES, "code_review", navigation=True)
        if benchmark_type in (BACK, CANCEL, MAIN): return benchmark_type
        notes = self.ask("Notes", navigation=True, default="")
        if notes in (BACK, CANCEL, MAIN): return notes
        return self.catalog.prompt_templates.create(PromptTemplate(**values, prompt_hash=hashlib.sha256(values["prompt_text"].encode("utf-8")).hexdigest(), benchmark_type=benchmark_type, notes=notes))

    def create_hardware_profile(self) -> HardwareProfile | object | None:
        values = self._form([("name", "Hardware profile name", None, "text"), ("cpu", "CPU", "", "text"), ("gpu", "GPU", "", "text"), ("vram_gb", "VRAM GB", None, "float"), ("ram_gb", "RAM GB", None, "float"), ("operating_system", "Operating system", "", "text"), ("versions", "Backend versions (LM Studio=0.3, optional)", "", "text"), ("notes", "Notes", "", "text")])
        if values in (BACK, CANCEL, MAIN): return values
        if not values["name"]: self.output("A hardware profile name is required."); return None
        versions = {part.split("=", 1)[0].strip(): part.split("=", 1)[1].strip() for part in values.pop("versions").split(",") if "=" in part}
        return self.catalog.hardware_profiles.create(HardwareProfile(**values, backend_versions=versions))

    def collect_score(self, current: ReviewScore | None = None) -> ReviewScore | object:
        self.output("\nReview score (B=back, C=cancel, Q=main menu)")
        accuracy = self.ask_float("Accuracy score (0-5)", default=current.accuracy_score if current else None, navigation=True)
        if accuracy in (BACK, CANCEL, MAIN): return accuracy
        hallucination = self.pick("Hallucination level", LEVELS, current.hallucination_level if current else "Medium", navigation=True)
        reliability = self.pick("Reliability level", LEVELS, current.reliability_level if current else "Medium", navigation=True)
        if hallucination in (BACK, CANCEL, MAIN) or reliability in (BACK, CANCEL, MAIN): return hallucination if hallucination in (BACK, CANCEL, MAIN) else reliability
        values = self._form([("depth_score", "Depth score (0-5)", current.depth_score if current else None, "float"), ("signal_noise_score", "Signal/noise score (0-5)", current.signal_noise_score if current else None, "float"), ("actionability_score", "Actionability score (0-5)", current.actionability_score if current else None, "float"), ("seniority_score", "Seniority score (0-5)", current.seniority_score if current else None, "float"), ("overall_score", "Overall score (0-5)", current.overall_score if current else None, "float"), ("strengths", "Strengths", current.strengths if current else "", "text"), ("weaknesses", "Weaknesses", current.weaknesses if current else "", "text"), ("verdict", "Verdict", current.verdict if current else "", "text"), ("notes", "Notes", current.notes if current else "", "text")])
        if values in (BACK, CANCEL, MAIN): return values
        return ReviewScore(run_id=current.run_id if current else 0, id=current.id if current else None, accuracy_score=accuracy, hallucination_level=hallucination, reliability_level=reliability, **values)

    def collect_attachments(self, draft: list[dict]) -> object | None:
        while True:
            answer = self.yes_no("Add attachment metadata", navigation=True)
            if answer in (BACK, CANCEL, MAIN): return answer
            if not answer: return None
            attachment_type = self.pick("Attachment type", ATTACHMENT_TYPES, "other", navigation=True)
            if attachment_type in (BACK, CANCEL, MAIN): return attachment_type
            file_path = self.prompt_path("File path", must_exist=True)
            if file_path in (BACK, CANCEL, MAIN): return file_path
            original_filename = self.ask("Original filename", navigation=True)
            notes = self.ask("Attachment notes", navigation=True, default="")
            if original_filename in (BACK, CANCEL, MAIN) or notes in (BACK, CANCEL, MAIN): return original_filename if original_filename in (BACK, CANCEL, MAIN) else notes
            if not file_path or not original_filename: self.output("File path and filename are required."); continue
            draft.append({"attachment_type": attachment_type, "file_path": file_path, "original_filename": original_filename, "notes": notes})

    def add_run_wizard(self) -> None:
        state: dict = {"attachments": []}
        steps = [
            ("session_id", lambda: self.choose_catalog("Session", self.catalog.sessions, self.create_session, "session", optional=True, current_id=state.get("session_id"))),
            ("model_profile_id", lambda: self.choose_catalog("Model profile", self.catalog.model_profiles, self.create_model_profile, "model", optional=False, current_id=state.get("model_profile_id"))),
            ("benchmark_definition_id", lambda: self.choose_catalog("Benchmark definition", self.catalog.benchmark_definitions, self.create_definition, "benchmark", optional=False, current_id=state.get("benchmark_definition_id"))),
            ("prompt_template_id", lambda: self.choose_catalog("Prompt template", self.catalog.prompt_templates, self.create_prompt_template, "prompt", optional=False, current_id=state.get("prompt_template_id"))),
            ("hardware_profile_id", lambda: self.choose_catalog("Hardware profile", self.catalog.hardware_profiles, self.create_hardware_profile, "hardware", optional=True, current_id=state.get("hardware_profile_id"))),
            ("raw_model_output", lambda: self.ask("Raw model output", navigation=True, default=state.get("raw_model_output", ""))),
            ("score", lambda: self.collect_score(state.get("score"))),
            ("attachments", lambda: self.attachment_step(state["attachments"])),
        ]
        index = 0
        while index < len(steps):
            key, action = steps[index]; value = action()
            if value is MAIN or value is CANCEL: self.output("Wizard cancelled."); return
            if value is BACK: index = max(0, index - 1); continue
            state[key] = value; index += 1
        self.review_and_save(state, steps)

    def attachment_step(self, draft: list[dict]) -> list[dict] | object:
        result = self.collect_attachments(draft)
        return draft if result is None else result

    def review_and_save(self, state: dict, steps: list) -> None:
        while True:
            self.show_draft(state)
            selected = self.ask("S) Save  E) Edit  C) Cancel  Q) Main menu", navigation=True)
            if selected in (CANCEL, MAIN): self.output("Wizard cancelled."); return
            if selected is BACK: continue
            choice = self.normalized(str(selected))
            if choice in {"s", "save"}:
                try:
                    template = self.catalog.prompt_templates.get(state["prompt_template_id"])
                    run = BenchmarkRun(raw_model_output=state["raw_model_output"], prompt_name=template.name, session_id=state["session_id"], model_profile_id=state["model_profile_id"], benchmark_definition_id=state["benchmark_definition_id"], prompt_template_id=state["prompt_template_id"], hardware_profile_id=state["hardware_profile_id"])
                    saved, _ = self.benchmarks.save_run(run, state["score"])
                    for attachment in state["attachments"]: self.benchmarks.add_attachment(RunAttachment(run_id=saved.id, **attachment))
                    self.output("✓ Benchmark saved."); return
                except ValueError: self.output("The benchmark could not be saved. Check the entered values and try again.")
            elif choice in {"e", "edit"}:
                section = self.ask_id("Section number", navigation=True)
                if section in (CANCEL, MAIN): return
                if section is BACK: continue
                if not 1 <= section <= len(steps): self.output("Choose a section from 1 to 8."); continue
                index = section - 1
                while index < len(steps):
                    key, action = steps[index]; value = action()
                    if value in (CANCEL, MAIN): return
                    if value is BACK: index = max(0, index - 1); continue
                    state[key] = value; index += 1
            elif choice in {"", "c", "cancel"}: self.output("Wizard cancelled."); return
            else: self.output("Choose Save, Edit, Cancel, or Main menu.")

    def show_draft(self, state: dict) -> None:
        model = self.catalog.model_profiles.get(state["model_profile_id"])
        definition = self.catalog.benchmark_definitions.get(state["benchmark_definition_id"])
        template = self.catalog.prompt_templates.get(state["prompt_template_id"])
        self.output("\nReview benchmark run")
        self.output(f"Model: {model.name} | Benchmark: {definition.name} | Prompt: {template.name} v{template.version}")
        self.output(f"Raw output: {state['raw_model_output'][:120]}")
        self.output(f"Attachments: {len(state['attachments'])} | Overall score: {state['score'].overall_score}")

    def list_runs(self) -> None:
        runs = self.benchmarks.runs.list()
        if not runs: self.output("No benchmark runs found."); self.pause(); return
        for run in runs:
            score = self.benchmarks.get_run(run.id)[1]
            self.output(f"#{run.id} | {run.model_snapshot.get('model_name', 'Unknown')} | {run.benchmark_snapshot.get('name') or run.benchmark_snapshot.get('file_path', 'Unknown')} | overall={score.overall_score if score else '-'}")
        self.pause()

    def view_run(self, run_id: int) -> None:
        run, score, attachments = self.benchmarks.get_run(run_id)
        if not run: self.output("Run not found."); self.pause(); return
        self.output(f"\nRun #{run.id}\nRaw model output:\n{run.raw_model_output}")
        self.output("\nSnapshots:\n" + json.dumps({"model": run.model_snapshot, "benchmark": run.benchmark_snapshot, "prompt": run.prompt_snapshot, "hardware": run.hardware_snapshot}, indent=2))
        self.output("\nReview:\n" + (json.dumps(score.__dict__, indent=2) if score else "No review score."))
        if attachments: self.output("Attachments:\n" + "\n".join(f"- {a.attachment_type}: {a.file_path}" for a in attachments))
        self.pause()

    def edit_run(self, run_id: int) -> None:
        run, score, _ = self.benchmarks.get_run(run_id)
        if not run: self.output("Run not found."); self.pause(); return
        while True:
            selected = self.ask("Edit: 1) Output  2) Prompt  3) Score  4) Attachment  B) Back", navigation=True)
            if selected in (CANCEL, MAIN): return
            if selected is BACK: return
            choice = self.normalized(str(selected))
            if choice in {"b", "back", "q", "quit", "exit"}: return
            if choice in {"1", "output"}:
                value = self.ask("Raw model output", navigation=True, default=run.raw_model_output)
                if value in (CANCEL, MAIN, BACK): continue
                run = self.benchmarks.update_run(replace(run, raw_model_output=value)); self.output("✓ Run updated.")
            elif choice in {"2", "prompt"}:
                value = self.ask("Prompt text", navigation=True, default=run.prompt_text)
                if value in (CANCEL, MAIN, BACK): continue
                run = self.benchmarks.update_run(replace(run, prompt_text=value)); self.output("✓ Run updated.")
            elif choice in {"3", "score"}:
                new_score = self.collect_score(score)
                if new_score not in (BACK, CANCEL, MAIN): score = self.benchmarks.update_score(new_score) if score else self.benchmarks.scores.create(replace(new_score, run_id=run.id)); self.output("✓ Review score updated.")
            elif choice in {"4", "attachment"}: self.collect_attachments_after_save(run.id)
            else: self.output("Choose output, prompt, score, attachment, or back.")

    def collect_attachments_after_save(self, run_id: int) -> None:
        draft: list[dict] = []
        result = self.collect_attachments(draft)
        if result is None:
            for attachment in draft: self.benchmarks.add_attachment(RunAttachment(run_id=run_id, **attachment))
            if draft: self.output("✓ Attachment metadata saved.")

    def delete_run(self, run_id: int) -> None:
        run, _, _ = self.benchmarks.get_run(run_id)
        if not run: self.output("Run not found."); self.pause(); return
        self.output(f"Delete benchmark #{run.id}?\nModel: {run.model_snapshot.get('model_name', 'Unknown')}\nBenchmark: {run.benchmark_snapshot.get('file_path', 'Unknown')}")
        confirmation = self.ask("Type DELETE to confirm", navigation=True)
        if confirmation in (BACK, CANCEL, MAIN) or str(confirmation).upper() != "DELETE": self.output("Delete cancelled."); return
        self.benchmarks.delete_run(run_id); self.output("✓ Run deleted.")

    def _scoreboard_batches(self) -> dict[int, object]:
        return {batch.id: batch for batch in self.catalog.scoreboard_import_batches.list() if batch.id is not None}

    def list_scoreboard_entries(self, batch_id: int | None = None) -> None:
        batches = self._scoreboard_batches()
        entries = self.catalog.scoreboard_entries.list()
        if batch_id is not None:
            entries = [entry for entry in entries if entry.import_batch_id == batch_id]
        if not entries:
            self.output("No scoreboard entries found.")
            return
        for entry in entries:
            batch = batches.get(entry.import_batch_id)
            self.output(
                f"#{entry.id} | {entry.model_name} | score={entry.score if entry.score is not None else '-'} "
                f"| batch={batch.name if batch else '-'} | imported={entry.imported_at}"
            )

    def view_scoreboard_entry(self, entry_id: int) -> None:
        entry = self.catalog.scoreboard_entries.get(entry_id)
        if not entry:
            self.output("Scoreboard entry not found.")
            return
        batch = self._scoreboard_batches().get(entry.import_batch_id)
        self.output(f"\nScoreboard entry #{entry.id}")
        self.output(f"Batch: {batch.name if batch else '-'}")
        self.output(f"Imported at: {entry.imported_at}")
        self.output(json.dumps(entry.__dict__, indent=2))

    def list_scoreboard_batches(self) -> None:
        batches = self.catalog.scoreboard_import_batches.list()
        if not batches:
            self.output("No scoreboard import batches found.")
            return
        entry_counts: dict[int, int] = {}
        for entry in self.catalog.scoreboard_entries.list():
            if entry.import_batch_id is not None:
                entry_counts[entry.import_batch_id] = entry_counts.get(entry.import_batch_id, 0) + 1
        for batch in batches:
            self.output(
                f"#{batch.id} | {batch.name} | entries={entry_counts.get(batch.id, 0)} "
                f"| imported={batch.imported_at} | source={batch.source_file}"
            )

    def scoreboard_screen(self) -> None:
        while True:
            self.output("\nScoreboard (historical summary imports)\n---------------------------------------")
            choice = self.ask("1) List entries  2) View entry  3) List import batches  4) View entries by batch  B) Back", navigation=True)
            if choice in (BACK, CANCEL, MAIN):
                return
            command = self.normalized(str(choice))
            if command in {"1", "list"}:
                self.list_scoreboard_entries()
            elif command in {"2", "view"}:
                entry_id = self.ask_id("Scoreboard entry ID")
                if entry_id not in (BACK, CANCEL, MAIN):
                    self.view_scoreboard_entry(entry_id)
            elif command in {"3", "batches"}:
                self.list_scoreboard_batches()
            elif command in {"4", "batch"}:
                batch_id = self.ask_id("Import batch ID")
                if batch_id not in (BACK, CANCEL, MAIN):
                    if batch_id not in self._scoreboard_batches():
                        self.output("Import batch not found.")
                    else:
                        self.list_scoreboard_entries(batch_id)
            else:
                self.output("Choose 1, 2, 3, 4, or B to return.")

    def catalog_screen(self, title: str, repository, create) -> None:
        """A small, focused catalog screen for one reusable record type."""
        while True:
            self.output(f"\n{title}\n{'-' * len(title)}")
            items = repository.list()
            if items:
                for item in items: self.output(f"{item.id}) {item.name if hasattr(item, 'name') else item.title}")
            else:
                self.output("No records found.")
            choice = self.ask("N) New  B) Back", navigation=True)
            if choice in (BACK, CANCEL, MAIN): return
            if self.normalized(str(choice)) not in {"n", "new"}:
                self.output("Choose N to create a record or B to return.")
                continue
            try:
                item = create()
                if item not in (BACK, CANCEL, MAIN, None): self.output(f"✓ {type(item).__name__} created.")
            except ValueError:
                self.output("The record could not be created. Check the entered values and try again.")

    def not_available(self, name: str, phase: str) -> None:
        self.output(f"{name} will be available in {phase}.")
        self.pause()

    def show_import_mapping(self, preview) -> None:
        self.output("\nDetected columns:")
        for heading in preview.headings:
            target = preview.mapping[heading]
            marker = "✓" if target else "–"
            self.output(f"{marker} {heading:<20} -> {target or 'ignored'}")

    def edit_import_mapping(self, preview, mapping_fields=MAPPING_FIELDS) -> dict[str, str | None] | object:
        mapping = dict(preview.mapping)
        while True:
            self.output("\nEdit mapping")
            for number, heading in enumerate(preview.headings, start=1):
                self.output(f"{number}) {heading} -> {mapping[heading] or 'ignored'}")
            choice = self.ask("Column number, A) Skip all unknown, D) Done", navigation=True)
            if choice in (BACK, CANCEL, MAIN): return choice
            command = self.normalized(str(choice))
            if command == "d": return mapping
            if command == "a":
                for heading in preview.unknown_headings: mapping[heading] = None
                self.output("All unknown columns will be ignored.")
                continue
            if not command.isdigit() or not 1 <= int(command) <= len(preview.headings):
                self.output("Choose a listed column number, A, or D.")
                continue
            heading = preview.headings[int(command) - 1]
            self.output(f"\nColumn: {heading}")
            for number, field in enumerate(mapping_fields, start=1): self.output(f"{number}) {field}")
            self.output("0) Ignore this column")
            while True:
                field_choice = self.ask("Choose field", navigation=True)
                if field_choice in (BACK, CANCEL, MAIN): return field_choice
                selected = self.normalized(str(field_choice))
                if selected.isdigit() and 0 <= int(selected) <= len(mapping_fields):
                    mapping[heading] = None if selected == "0" else mapping_fields[int(selected) - 1]
                    break
                self.output(f"Enter a number from 0 to {len(mapping_fields)}.")

    def choose_mapping_profile(self) -> dict[str, str | None] | object | None:
        profiles = self.importer.mapping_profiles()
        if not profiles: return None
        use_profile = self.yes_no("Use saved mapping profile", navigation=True)
        if use_profile in (BACK, CANCEL, MAIN): return use_profile
        if not use_profile: return None
        for number, (_, name, _) in enumerate(profiles, start=1): self.output(f"{number}) {name}")
        while True:
            choice = self.ask("Choose mapping profile", navigation=True)
            if choice in (BACK, CANCEL, MAIN): return choice
            selected = self.normalized(str(choice))
            if selected.isdigit() and 1 <= int(selected) <= len(profiles): return profiles[int(selected) - 1][2]
            self.output("Choose a listed mapping profile number.")

    def save_mapping_profile(self, mapping: dict[str, str | None]) -> object | None:
        save_profile = self.yes_no("Save this mapping as a profile", navigation=True)
        if save_profile in (BACK, CANCEL, MAIN): return save_profile
        if not save_profile: return None
        names = ("My Spreadsheet", "Reddit Format", "Simple CSV", "Custom")
        for number, name in enumerate(names, start=1): self.output(f"{number}) {name}")
        while True:
            choice = self.ask("Profile name", navigation=True)
            if choice in (BACK, CANCEL, MAIN): return choice
            selected = self.normalized(str(choice))
            if selected.isdigit() and 1 <= int(selected) <= len(names):
                name = names[int(selected) - 1]
                if name == "Custom":
                    name = self.ask("Custom profile name", navigation=True)
                    if name in (BACK, CANCEL, MAIN): return name
                    if not str(name).strip(): self.output("Enter a profile name."); continue
                self.importer.save_mapping_profile(str(name), mapping)
                self.output(f'Saved mapping profile "{name}".')
                return None
            self.output("Choose a number from 1 to 4.")

    @staticmethod
    def apply_default_benchmark(rows: list[dict[str, str]], benchmark: str) -> None:
        if benchmark:
            for row in rows:
                if not row.get("benchmark_file", ""): row["benchmark_file"] = benchmark

    def import_screen(self) -> None:
        choice = self.ask("Import: 1) Benchmark Runs CSV  2) Scoreboard CSV  3) Auto-detect CSV type", navigation=True)
        if choice in (BACK, CANCEL, MAIN): return
        import_type = {"1": "runs", "2": "scoreboard", "3": "auto"}.get(self.normalized(str(choice)))
        if not import_type: self.output("Choose 1, 2, or 3."); return
        self.import_csv(import_type)

    def import_csv(self, import_type: str = "auto") -> None:
        path = self.prompt_path("CSV file path", must_exist=True, extensions=(".csv",))
        if path is None: return
        if path in (BACK, CANCEL, MAIN): return
        try:
            preview = self.importer.preview(str(path))
        except (OSError, csv.Error) as error:
            self.output(f"Could not read CSV: {error}")
            return
        run_fields = {"benchmark_file", "prompt_text", "raw_model_output"}
        summary_import = import_type == "scoreboard" or (import_type == "auto" and not run_fields <= set(preview.mapping.values()))
        if summary_import: preview = self.importer.preview(str(path), summary=True)
        profile_mapping = self.choose_mapping_profile()
        if profile_mapping in (BACK, CANCEL, MAIN): return
        if profile_mapping is not None: preview = self.importer.preview(str(path), profile_mapping, summary=summary_import)
        while True:
            self.show_import_mapping(preview)
            prompt = ("This CSV appears to be a model-summary/leaderboard file, not per-benchmark runs.\nImport as scoreboard entries? Y) Import  E) Edit mapping  C) Cancel"
                      if summary_import else "Continue with this mapping? Y) Import  E) Edit mapping  C) Cancel")
            choice = self.ask(prompt, navigation=True, default="y")
            if choice in (CANCEL, MAIN, BACK): return
            command = self.normalized(str(choice))
            if command in {"y", "import"}: break
            if command in {"e", "edit"}:
                mapping = self.edit_import_mapping(preview, SUMMARY_MAPPING_FIELDS if summary_import else MAPPING_FIELDS)
                if mapping in (BACK, CANCEL, MAIN): return
                preview = self.importer.preview(str(path), mapping, summary=summary_import)
                saved = self.save_mapping_profile(mapping)
                if saved in (BACK, CANCEL, MAIN): return
                continue
            self.output("Choose Y to import, E to edit the mapping, or C to cancel.")
        if not summary_import:
            blank_benchmarks = sum(not row.get("benchmark_file", "") for row in preview.rows)
            if blank_benchmarks:
                default_benchmark = self.ask(f"Benchmark is blank for {blank_benchmarks} row(s). Default benchmark (optional)", navigation=True)
                if default_benchmark in (BACK, CANCEL, MAIN): return
                self.apply_default_benchmark(preview.rows, str(default_benchmark))
        if summary_import:
            self.output(f"\nPreview: {len(preview.rows)} importable row(s), {len(preview.skipped_rows)} skipped non-data row(s)")
        else:
            self.output(f"\nPreview: {len(preview.rows)} row(s)")
        for number, row in enumerate(preview.rows[:5], start=1):
            score = row.get("score", "-") if summary_import else row.get("overall_score", "-")
            self.output(f"{number}) model={row.get('model_name', '')!r}, benchmark={row.get('benchmark_file', '') if not summary_import else 'summary'!r}, score={score!r}, hallucination={row.get('hallucination_level', '-')!r}, reliability={row.get('reliability_level', '-')!r}, notes={row.get('notes', '')[:50]!r}")
        if len(preview.rows) > 5: self.output(f"... plus {len(preview.rows) - 5} more row(s)")
        if summary_import:
            proceed = self.yes_no("Import these scoreboard entries", navigation=True)
            if proceed is not True: return
            batch_name = self.ask("Optional scoreboard import batch name (blank uses filename and import date)", navigation=True)
            if batch_name in (BACK, CANCEL, MAIN): return
            try:
                result = self.importer.import_scoreboard_entries(preview.rows, str(path), str(batch_name) or None, row_numbers=preview.row_numbers)
                self.output(f"Imported {result.imported} scoreboard entry row(s).")
            except ValueError as error:
                self.output(f"Import cancelled: {error}. No rows were written.")
            return
        incomplete = sum(not row.get("prompt_text", "") or not row.get("raw_model_output", "") for row in preview.rows)
        if incomplete: self.output(f"Warning: {incomplete} row(s) have blank prompt or output; they can be imported as historical data but may be incomplete for JSONL training export.")
        proceed = self.yes_no("Import these previewed rows", navigation=True)
        if proceed is not True: return
        policy = self.ask("Duplicates: S) Skip  R) Replace  K) Keep", navigation=True, default="s")
        if policy in (BACK, CANCEL, MAIN): return
        duplicate_policy = {"s": "skip", "skip": "skip", "r": "replace", "replace": "replace", "k": "keep", "keep": "keep"}.get(self.normalized(str(policy)))
        if not duplicate_policy:
            self.output("Choose Skip, Replace, or Keep."); return
        try:
            result = self.importer.import_rows(preview.rows, duplicate_policy)
            self.output(f"Imported {result.imported}; skipped {result.skipped}; replaced {result.replaced}; duplicates detected {result.duplicates}.")
        except ValueError as error:
            self.output(f"Import cancelled: {error}. No rows were written.")

    def export_screen(self) -> None:
        choice = self.ask("Export: 1) Benchmark Runs CSV  2) Scoreboard CSV  3) JSONL training data  4) Markdown report  5) Scoreboard HTML", navigation=True)
        if choice in (BACK, CANCEL, MAIN): return
        exporters = {"1": ("Benchmark Runs CSV", export_benchmark_runs_csv), "2": ("Scoreboard CSV", export_scoreboard_csv), "3": ("JSONL training data", export_jsonl_training_data), "4": ("Markdown report", export_combined_markdown), "5": ("Scoreboard HTML", export_scoreboard_html)}
        selected = exporters.get(self.normalized(str(choice)))
        if not selected: self.output("Choose 1, 2, 3, 4, or 5."); return
        path = self.prompt_path(f"Destination for {selected[0]}", preserve_trailing_separator=True)
        if path is None: return
        if path in (BACK, CANCEL, MAIN): return
        output_path = self.prepare_export_destination(str(path), selected[0])
        if output_path is None or output_path in (BACK, CANCEL, MAIN): return
        exporter = selected[1]
        if selected[0] == "Markdown report":
            saved = exporter(self.benchmarks, self.catalog, str(output_path))
        elif selected[0] in {"Scoreboard CSV", "Scoreboard HTML"}:
            saved = exporter(self.catalog, str(output_path))
        else:
            saved = exporter(self.benchmarks, str(output_path))
        self.output(f"Exported {selected[0]} to {saved}.")
        if selected[0] == "Scoreboard HTML" and self.yes_no("Open HTML report in browser?", navigation=True) is True:
            try:
                webbrowser.open(Path(saved).resolve().as_uri())
            except OSError as error:
                self.output(f"Could not open HTML report: {error}")

    def help(self) -> None:
        self.output("Commands: add, list, view, edit, delete, reference data, quit.\nUse B/back to return, C/cancel to abandon a wizard, and Q/quit/exit for the main menu.")
        self.pause()

    def show_main_menu(self) -> None:
        runs = len(self.benchmarks.runs.list())
        scoreboard_entries = len(self.catalog.scoreboard_entries.list())
        models = len(self.catalog.model_profiles.list())
        sessions = len(self.catalog.sessions.list())
        database_name = self.benchmarks.database.path.name
        border = "=" * MENU_WIDTH
        self.output(f"\n{border}")
        self.output("Local LLM Benchmark Recorder".center(MENU_WIDTH))
        self.output(f"Version {APP_VERSION}".center(MENU_WIDTH))
        self.output(f"{border}\n")
        self.output(f"Database : {database_name}\nRuns     : {runs}\nScoreboard entries : {scoreboard_entries}\nModels   : {models}\nSessions : {sessions}\nVersion  : {APP_VERSION}\n")
        self.output(" Runs\n ----\n 1) Add Run\n 2) List Runs\n 3) View Run\n 4) Edit Run\n 5) Delete Run\n")
        self.output(" Reference Data\n --------------\n 6) Sessions\n 7) Models\n 8) Benchmarks\n 9) Prompt Templates\n10) Hardware Profiles\n")
        self.output(" Data\n ----\n11) Import\n12) Export\n13) Scoreboard\n")
        self.output(" Help\n ----\nH) Help\nS) Settings\nQ) Quit\n")
        self.output(border)

    def run(self) -> None:
        commands = {
            "1": "add", "add": "add", "2": "list", "list": "list", "3": "view", "view": "view",
            "4": "edit", "edit": "edit", "5": "delete", "delete": "delete",
            "6": "sessions", "session": "sessions", "sessions": "sessions",
            "7": "models", "model": "models", "models": "models", "profile": "models",
            "8": "benchmarks", "benchmark": "benchmarks", "benchmarks": "benchmarks", "definition": "benchmarks",
            "9": "prompts", "prompt": "prompts", "prompts": "prompts", "template": "prompts",
            "10": "hardware", "hardware": "hardware",
            "11": "import", "import": "import", "12": "export", "export": "export",
            "13": "scoreboard", "scoreboard": "scoreboard",
            "reference": "sessions", "reference-data": "sessions", "s": "settings", "settings": "settings", "h": "help", "help": "help",
        }
        while True:
            self.show_main_menu()
            raw = self.ask("Choose an option")
            command = self.normalized(str(raw))
            if command in QUIT_WORDS: return
            command = commands.get(command)
            try:
                if command == "add": self.add_run_wizard()
                elif command == "list": self.list_runs()
                elif command in {"view", "edit", "delete"}:
                    run_id = self.ask_id("Run ID")
                    if run_id is MAIN: continue
                    if run_id in (BACK, CANCEL): continue
                    {"view": self.view_run, "edit": self.edit_run, "delete": self.delete_run}[command](run_id)
                elif command == "sessions": self.catalog_screen("Sessions", self.catalog.sessions, self.create_session)
                elif command == "models": self.catalog_screen("Model Profiles", self.catalog.model_profiles, self.create_model_profile)
                elif command == "benchmarks": self.catalog_screen("Benchmark Definitions", self.catalog.benchmark_definitions, self.create_definition)
                elif command == "prompts": self.catalog_screen("Prompt Templates", self.catalog.prompt_templates, self.create_prompt_template)
                elif command == "hardware": self.catalog_screen("Hardware Profiles", self.catalog.hardware_profiles, self.create_hardware_profile)
                elif command == "import": self.import_screen()
                elif command == "export": self.export_screen()
                elif command == "scoreboard": self.scoreboard_screen()
                elif command == "settings": self.not_available("Settings", "a future phase")
                elif command == "help": self.help()
                else: self.output("Choose a menu number or command. Type H for help.")
            except KeyboardInterrupt:
                self.output("\nReturning to the main menu.")
            except Exception:
                self.logger.exception("Unexpected CLI error")
                self.output("An unexpected error occurred.\nSee logs/error.log for details.")
