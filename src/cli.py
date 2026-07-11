from __future__ import annotations

import hashlib
import json
import logging
import csv
import os
import re
import sys
import webbrowser
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, TypeAlias, TypedDict, cast
from prompt_toolkit import prompt as toolkit_prompt
from prompt_toolkit.completion import PathCompleter

from engine.database import EngineDatabase, MIGRATIONS
from engine.domain import ATTACHMENT_TYPES, BENCHMARK_TYPES, LEVELS, BenchmarkDefinition, BenchmarkRun, BenchmarkSession, HardwareProfile, ModelProfile, PromptTemplate, ReviewScore, RunAttachment, ScoreboardImportBatch, now
from engine.services import BenchmarkService, CatalogService
from engine.importers import CsvImportService, ImportPreview, MAPPING_FIELDS, SUMMARY_MAPPING_FIELDS
from engine.exporters import export_benchmark_runs_csv, export_combined_markdown, export_jsonl_training_data, export_scoreboard_csv, export_scoreboard_html
from engine.hardware_importers import HardwareImporterRegistry, HardwareProfileDraft, decode_hardware_text, parse_key_value_pairs
from engine.path_completion import normalize_path, resolve_export_destination
from engine.archive import ArchiveError, ArchiveService, TABLES
from engine.prompt_file_importer import PromptFileError, decode_prompt_file, prompt_preview
from engine.settings import DefaultWorkingDirectorySettings
from engine.datasets import DatasetBuilder, DatasetFilters, RedactionConfig, DatasetOutputError

class NavigationSignal:
    """Typed sentinel for returning from a prompt without accepting input."""


class QuitApplication(BaseException):
    """Propagates a global Quit All request to the top-level application loop."""


BACK, CANCEL, MAIN = NavigationSignal(), NavigationSignal(), NavigationSignal()
PromptResult: TypeAlias = str | NavigationSignal
NumericPromptResult: TypeAlias = float | None | NavigationSignal
FormValue: TypeAlias = str | float | None
FormValues: TypeAlias = dict[str, FormValue]


class AttachmentDraft(TypedDict):
    attachment_type: str
    file_path: str
    original_filename: str
    notes: str
BACK_WORDS = {"b", "back"}
CANCEL_WORDS = {"c", "cancel"}
QUIT_WORDS = {"q", "quit", "exit"}
QUIT_ALL_WORDS = {"qa", "quit all", "quit a"}
APP_VERSION = "0.4.1-Alpha"
MENU_WIDTH = 56
ARCHIVE_LABELS = {
    "benchmark_sessions": "Sessions", "model_profiles": "Models", "hardware_profiles": "Hardware Profiles",
    "benchmark_definitions": "Benchmark Definitions", "prompt_templates": "Prompt Templates",
    "benchmark_runs": "Benchmark Runs", "review_scores": "Review Scores", "run_attachments": "Attachments",
    "scoreboard_import_batches": "Scoreboard Batches", "scoreboard_entries": "Scoreboard Entries",
    "export_profiles": "Export Profiles",
}


class TerminalApp:
    """A forgiving terminal interface over the Phase 1 service layer."""
    def __init__(self, database_path: str | Path, input_fn: Callable[..., str] = input, output_fn: Callable[[str], None] = print):
        database = EngineDatabase(database_path)
        database.migrate()
        self.catalog = CatalogService(database)
        self.benchmarks = BenchmarkService(database, self.catalog)
        self.importer = CsvImportService(self.benchmarks)
        self.hardware_importers = HardwareImporterRegistry()
        self.archives = ArchiveService(database)
        self.datasets = DatasetBuilder(self.benchmarks, benchpup_version=APP_VERSION, schema_version=max(version for version, _ in MIGRATIONS))
        self.settings = DefaultWorkingDirectorySettings(database_path)
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
        return " ".join(value.strip().lower().split())

    def _check_quit_all(self, raw_choice: str) -> None:
        if self.normalized(raw_choice) in QUIT_ALL_WORDS:
            raise QuitApplication()

    def ask(self, label: str, *, navigation: bool = False, default: str | None = None) -> PromptResult:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            # Ensure all status text has reached the terminal before input() owns the cursor.
            sys.stdout.flush()
            raw = self.input(f"{label}{suffix}: ")
        except KeyboardInterrupt:
            self.output("\nOperation cancelled.")
            return MAIN
        self._check_quit_all(raw)
        value = raw.strip()
        command = self.normalized(value)
        if navigation:
            if command in BACK_WORDS: return BACK
            if command in CANCEL_WORDS: return CANCEL
            if command in QUIT_WORDS: return MAIN
        return default if value == "" and default is not None else value

    def pause(self) -> None:
        try: self.input("Press Enter to continue...")
        except KeyboardInterrupt: self.output("")

    def render_screen(self, title: str, content: str = "") -> None:
        """Redraw one CLI screen without relying exclusively on ANSI control codes."""
        if self.interactive_input and sys.stdout.isatty():
            os.system("cls" if os.name == "nt" else "clear")
        else:
            self.output("")
        self.output("=" * MENU_WIDTH)
        self.output("BenchPup".center(MENU_WIDTH))
        self.output(f"Version {APP_VERSION}".center(MENU_WIDTH))
        self.output("=" * MENU_WIDTH)
        self.output(f"\n{title}\n{'-' * len(title)}")
        if content:
            self.output(content)
        self.output("")
        self.output("=" * MENU_WIDTH)

    def prompt_path(self, label: str, *, must_exist: bool = False, extensions: tuple[str, ...] = (), default: str | None = None, preserve_trailing_separator: bool = False, blank_cancels: bool = False, directory_only: bool = False, reject_boolean_paths: bool = False) -> str | NavigationSignal | None:
        """Prompt for a filesystem path while preserving non-interactive input behavior."""
        default_directory = self.settings.get_default_working_directory()
        self.output("Tip: press Tab to autocomplete paths.")
        if self.interactive_input:
            try:
                suffix = f" [{default}]" if default not in (None, "") else ""
                completer = (
                    PathCompleter(expanduser=True, get_paths=lambda: [str(default_directory)])
                    if default_directory is not None else PathCompleter(expanduser=True)
                )
                raw = toolkit_prompt(
                    f"{label}{suffix}: ",
                    completer=completer,
                    complete_while_typing=False,
                )
                self._check_quit_all(raw)
                value: PromptResult = default if raw.strip() == "" and default is not None else raw.strip()
                if isinstance(value, str):
                    command = self.normalized(value)
                    if command in BACK_WORDS: value = BACK
                    elif command in CANCEL_WORDS: value = CANCEL
                    elif command in QUIT_WORDS: value = MAIN
            except (KeyboardInterrupt, EOFError):
                self.output("\nOperation cancelled.")
                return MAIN
            finally:
                # prompt_toolkit owns its terminal state; flushing ensures its
                # completed render is settled before normal CLI output resumes.
                sys.stdout.flush()
        else:
            value = self.ask(label, navigation=True, default=default)
        if value in (BACK, CANCEL, MAIN):
            return value
        raw_value = str(value)
        if blank_cancels and not raw_value.strip():
            return None
        if reject_boolean_paths and self.normalized(raw_value) in {"y", "yes", "n", "no"}:
            self.output("Enter a directory path, not a yes/no response.")
            return None
        normalized = normalize_path(raw_value, base_dir=default_directory)
        if preserve_trailing_separator and raw_value.rstrip().endswith(("/", "\\")):
            normalized += os.sep
        suffixes = {extension.lower() for extension in extensions}
        if directory_only and Path(normalized).exists() and not Path(normalized).is_dir():
            self.output(f'Expected a directory, not a file: "{normalized}".')
            return None
        if must_exist and not Path(normalized).is_file():
            self.output(f'Path not found or not a file: "{normalized}".')
            return None
        if must_exist and suffixes and Path(normalized).suffix.lower() not in suffixes:
            self.output(f'Expected a {"/".join(extensions)} file: "{normalized}".')
            return None
        return normalized

    def prepare_export_destination(self, destination: str, export_name: str) -> Path | NavigationSignal | None:
        defaults: dict[str, tuple[str, str | None]] = {
            "Scoreboard HTML": ("scoreboard.html", ".html"),
            "Scoreboard CSV": ("scoreboard.csv", None),
            "Benchmark Runs CSV": ("benchmark_runs.csv", None),
            "JSONL training data": ("training_data.jsonl", None),
            "Markdown report": ("report.md", None),
            "BenchPup Backup": ("benchpup-backup.json", ".json"),
            "JSONL Dataset": ("dataset.jsonl", ".jsonl"),
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

    def ask_float(self, label: str, *, default: float | None = None, navigation: bool = False) -> NumericPromptResult:
        while True:
            value = self.ask(label, navigation=navigation, default=str(default) if default is not None else None)
            if isinstance(value, NavigationSignal): return value
            if value == "": return None
            try: return float(value)
            except ValueError: self.output(f'"{value}" is not a valid number. Please enter a number or leave it blank.')

    def ask_id(self, label: str = "Run ID", *, navigation: bool = True) -> int | NavigationSignal:
        while True:
            value = self.ask(label, navigation=navigation)
            if value in (BACK, CANCEL, MAIN): return value
            if isinstance(value, str) and value.isdigit() and int(value) > 0: return int(value)
            self.output(f'"{value}" is not a valid {label}. Please enter a numeric ID or B to go back.')

    def pick(self, label: str, choices: tuple[str, ...], default: str, *, navigation: bool = False) -> PromptResult:
        lookup: dict[str, str] = {item.lower(): item for item in choices}
        while True:
            value = self.ask(f"{label} [{'/'.join(choices)}]", navigation=navigation, default=default)
            if value in (BACK, CANCEL, MAIN): return value
            selected = lookup.get(str(value).lower())
            if selected: return selected
            self.output("Choose one of the listed values.")

    def yes_no(self, label: str, *, default: bool = False, navigation: bool = False) -> bool | NavigationSignal:
        hint = "Y/n" if default else "y/N"
        while True:
            value = self.ask(f"{label} [{hint}]", navigation=navigation)
            if value in (BACK, CANCEL, MAIN): return value
            command = str(value).lower()
            if not command: return default
            if command in {"y", "yes"}: return True
            if command in {"n", "no"}: return False
            self.output("Please answer yes or no.")

    def _form(self, fields: list[tuple[str, str, FormValue, str]]) -> FormValues | NavigationSignal:
        """Collect simple text/number fields; navigation works from every prompt."""
        values: FormValues = {}
        for name, label, default, kind in fields:
            value = self.ask_float(label, default=default if isinstance(default, float) else None, navigation=True) if kind == "float" else self.ask(label, navigation=True, default=default if isinstance(default, str) else None)
            if isinstance(value, NavigationSignal): return value
            values[name] = value
        return values

    @staticmethod
    def _form_text(values: Mapping[str, FormValue], key: str) -> str:
        value = values.get(key)
        return value if isinstance(value, str) else ""

    @staticmethod
    def _form_float(values: Mapping[str, FormValue], key: str) -> float | None:
        value = values.get(key)
        return value if isinstance(value, float) else None

    def choose_catalog(self, title: str, repository: Any, create: Callable[[], Any], key: str, *, optional: bool, current_id: int | None = None) -> int | None | NavigationSignal:
        while True:
            options = "1) Select Existing\n2) Create New"
            if optional:
                options += f"\n3) Continue Without {title}"
            options += "\n\nB) Back\nC) Cancel Run\nQA) Quit BenchPup completely"
            self.render_screen(title, options)
            value = self.ask("Choose an option", navigation=True)
            if value is BACK: return BACK
            if value is CANCEL: return CANCEL
            if value is MAIN: return MAIN
            command = self.normalized(str(value))
            if command == "1":
                selected = self.select_catalog_record(title, repository)
                if isinstance(selected, int):
                    self.last_used[key] = selected
                    return selected
                if selected is BACK:
                    continue
                if selected in (CANCEL, MAIN):
                    return selected
                continue
            if command == "2":
                created = create()
                if created in (BACK, CANCEL, MAIN): return created
                if created: self.last_used[key] = created.id; return created.id
                continue
            if command == "3" and optional:
                return None
            self.output("Choose a listed option, B to go back, or C to cancel the run.")

    def select_catalog_record(self, title: str, repository: Any) -> int | NavigationSignal:
        items = repository.list()
        lines = []
        for number, item in enumerate(items, start=1):
            label = item.name if hasattr(item, "name") else item.title
            lines.append(f"{number}) {label}")
        if not lines:
            lines.append("No records found.")
        lines.extend(("", "B) Back", "C) Cancel Run", "QA) Quit BenchPup completely"))
        self.render_screen(f"Select {title}", "\n".join(lines))
        while True:
            choice = self.ask("Choose an option", navigation=True)
            if choice in (BACK, CANCEL, MAIN):
                return choice
            selected = self.normalized(str(choice))
            if selected.isdigit() and 1 <= int(selected) <= len(items):
                item_id = items[int(selected) - 1].id
                assert item_id is not None, f"Persisted {title} is missing its ID"
                return item_id
            self.output(f"Choose a number from 1 to {len(items)}, B to go back, or C to cancel the run.")

    def create_session(self) -> BenchmarkSession | NavigationSignal | None:
        values: FormValues | NavigationSignal = self._form([("title", "Session title", None, "text"), ("description", "Description", "", "text"), ("started_at", "Started at (ISO, optional)", "", "text"), ("completed_at", "Completed at (ISO, optional)", "", "text"), ("notes", "Notes", "", "text")])
        if isinstance(values, NavigationSignal): return values
        session = BenchmarkSession(
            title=self._form_text(values, "title"), description=self._form_text(values, "description"),
            started_at=self._form_text(values, "started_at") or None,
            completed_at=self._form_text(values, "completed_at") or None, notes=self._form_text(values, "notes"),
        )
        if not session.title: self.output("A session title is required."); return None
        return self.catalog.sessions.create(session)

    def create_model_profile(self) -> ModelProfile | NavigationSignal | None:
        values: FormValues | NavigationSignal = self._form([("name", "Profile name", None, "text"), ("model_name", "Model name", None, "text"), ("backend", "Backend", "Other", "text"), ("model_family", "Model family", "", "text"), ("model_size", "Model size", "", "text"), ("quantization", "Quantization", "", "text"), ("temperature", "Temperature", None, "float"), ("tokens_per_second", "Tokens per second", None, "float")])
        if isinstance(values, NavigationSignal): return values
        profile = ModelProfile(
            name=self._form_text(values, "name"), model_name=self._form_text(values, "model_name"),
            backend=self._form_text(values, "backend"), model_family=self._form_text(values, "model_family"),
            model_size=self._form_text(values, "model_size"), quantization=self._form_text(values, "quantization"),
            temperature=self._form_float(values, "temperature"), tokens_per_second=self._form_float(values, "tokens_per_second"),
        )
        if not profile.name or not profile.model_name: self.output("Profile name and model name are required."); return None
        return self.catalog.model_profiles.create(profile)

    def create_definition(self) -> BenchmarkDefinition | NavigationSignal | None:
        name: PromptResult = self.ask("Benchmark name", navigation=True)
        if isinstance(name, NavigationSignal): return name
        file_path = self.prompt_path("Benchmark file path")
        if isinstance(file_path, NavigationSignal): return file_path
        if file_path is None: return None
        if not name or not file_path: self.output("Benchmark name and file path are required."); return None
        benchmark_type = self.pick("Benchmark type", BENCHMARK_TYPES, "code_review", navigation=True)
        if isinstance(benchmark_type, NavigationSignal): return benchmark_type
        rest: FormValues | NavigationSignal = self._form([("default_prompt", "Default prompt (optional)", "", "text"), ("tags", "Tags (optional)", "", "text")])
        if isinstance(rest, NavigationSignal): return rest
        return self.catalog.benchmark_definitions.create(BenchmarkDefinition(
            name=name, file_path=file_path, benchmark_type=benchmark_type,
            default_prompt=self._form_text(rest, "default_prompt"), tags=self._form_text(rest, "tags"),
        ))

    def create_prompt_template(self) -> PromptTemplate | NavigationSignal | None:
        values: FormValues | NavigationSignal = self._form([("name", "Prompt template name", None, "text"), ("version", "Prompt version", None, "text"), ("prompt_text", "Prompt text", None, "text")])
        if isinstance(values, NavigationSignal): return values
        name, version, prompt_text = (self._form_text(values, key) for key in ("name", "version", "prompt_text"))
        if not all((name, version, prompt_text)): self.output("Template name, version, and prompt text are required."); return None
        benchmark_type = self.pick("Benchmark type", BENCHMARK_TYPES, "code_review", navigation=True)
        if isinstance(benchmark_type, NavigationSignal): return benchmark_type
        notes = self.ask("Notes", navigation=True, default="")
        if isinstance(notes, NavigationSignal): return notes
        return self.catalog.prompt_templates.create(PromptTemplate(
            name=name, version=version, prompt_text=prompt_text,
            prompt_hash=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(), benchmark_type=benchmark_type, notes=notes,
        ))

    def import_prompt_template_file(self) -> None:
        path = self.prompt_path("Prompt template file", must_exist=True)
        if not isinstance(path, str):
            return
        suffix = Path(path).suffix.lower()
        if suffix not in {".txt", ".md", ".markdown", ".prompt"}:
            self.output(f'Warning: "{suffix or "no extension"}" is unfamiliar; importing because it is readable text.')
        try:
            prompt_text, encoding = decode_prompt_file(Path(path).read_bytes())
        except (OSError, PromptFileError) as error:
            self.output(f"Could not import prompt file: {error}")
            return
        preview, truncated = prompt_preview(prompt_text)
        self.output("\nPrompt File Preview\n-------------------")
        self.output(f"Filename: {Path(path).name}")
        self.output(f"Encoding: {encoding}")
        self.output(f"Characters: {len(prompt_text)}")
        self.output(f"Lines: {len(prompt_text.splitlines())}")
        self.output(preview)
        if truncated:
            self.output("[Preview truncated]")

        name = self.ask("Template name", navigation=True, default=Path(path).stem)
        if not isinstance(name, str):
            return
        if not name:
            self.output("Template name is required.")
            return
        existing: PromptTemplate | None = None
        replace_existing = False
        while True:
            version = self.ask("Version", navigation=True, default="1.0")
            if not isinstance(version, str):
                return
            if not version:
                self.output("Version is required.")
                continue
            existing = next((item for item in self.catalog.prompt_templates.list()
                             if item.name == name and item.version == version), None)
            if not existing:
                replace_existing = False
                break
            duplicate = self.ask("Name and version already exist: C) Cancel  V) Choose different version  R) Replace", navigation=True)
            if not isinstance(duplicate, str) or self.normalized(duplicate) in {"c", "cancel"}:
                return
            if self.normalized(duplicate) in {"v", "version"}:
                continue
            if self.normalized(duplicate) in {"r", "replace"}:
                replace_existing = True
                break
            self.output("Choose C, V, or R.")
        benchmark_type = self.pick("Benchmark type", BENCHMARK_TYPES, "code_review", navigation=True)
        if not isinstance(benchmark_type, str):
            return
        notes = self.ask("Notes (optional)", navigation=True, default="")
        if not isinstance(notes, str):
            return
        active = self.yes_no("Active", default=True, navigation=True)
        if active is not True and active is not False:
            return
        prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
        hash_match = next((item for item in self.catalog.prompt_templates.list()
                           if item.prompt_hash == prompt_hash and (item.name != name or item.version != version)), None)
        if hash_match:
            self.output(f'Warning: identical prompt content already exists as "{hash_match.name}" v{hash_match.version}.')
        confirm = self.yes_no("Import this prompt template", default=True, navigation=True)
        if confirm is not True:
            return
        template = PromptTemplate(
            name=name, version=version, prompt_text=prompt_text, prompt_hash=prompt_hash,
            benchmark_type=benchmark_type, notes=notes, is_active=active,
            id=existing.id if existing is not None and replace_existing else None,
        )
        try:
            saved = self.catalog.prompt_templates.update(template) if template.id else self.catalog.prompt_templates.create(template)
        except ValueError as error:
            self.output(f"Could not save prompt template: {error}")
            return
        self.output(
            "\n✓ Prompt template imported successfully\n"
            f"Name: {saved.name}\nVersion: {saved.version}\nBenchmark type: {saved.benchmark_type}\n"
            f"Source file: {path}\nCharacters: {len(prompt_text)}\nLines: {len(prompt_text.splitlines())}\nSHA-256: {prompt_hash}"
        )

    def view_prompt_template(self, template_id: int) -> None:
        template = self.catalog.prompt_templates.get(template_id)
        if template is None:
            self.output("Prompt template not found.")
            return
        self.output("\nPrompt Template\n---------------")
        self.output(
            f"Name: {template.name}\nVersion: {template.version}\nBenchmark type: {template.benchmark_type}\n"
            f"Active: {'Yes' if template.is_active else 'No'}\nCharacters: {len(template.prompt_text)}\n"
            f"Lines: {len(template.prompt_text.splitlines())}\nSHA-256: {template.prompt_hash}\n"
            f"Notes: {template.notes or '-'}\nCreated: {template.created_at}\nUpdated: {template.updated_at}"
        )
        self.output("\nPrompt Text\n-----------")
        self.output(template.prompt_text)
        self.output("\nB) Back\nQA) Quit BenchPup completely")
        self.ask("Choose an option", navigation=True, default="")

    def edit_prompt_template(self, template_id: int) -> None:
        template = self.catalog.prompt_templates.get(template_id)
        if template is None:
            self.output("Prompt template not found.")
            return
        name = self.ask("Template name", navigation=True, default=template.name)
        version = self.ask("Version", navigation=True, default=template.version)
        if not isinstance(name, str) or not isinstance(version, str):
            return
        if not name or not version:
            self.output("Template name and version are required.")
            return
        benchmark_type = self.pick("Benchmark type", BENCHMARK_TYPES, template.benchmark_type, navigation=True)
        notes = self.ask("Notes", navigation=True, default=template.notes)
        active = self.yes_no("Active", default=template.is_active, navigation=True)
        if not isinstance(benchmark_type, str) or not isinstance(notes, str) or not isinstance(active, bool):
            return
        replacement = self.yes_no("Replace prompt text from a text file", default=False, navigation=True)
        if not isinstance(replacement, bool):
            return
        prompt_text = template.prompt_text
        if replacement:
            path = self.prompt_path("Replacement prompt file", must_exist=True)
            if not isinstance(path, str):
                return
            try:
                prompt_text, _ = decode_prompt_file(Path(path).read_bytes())
            except (OSError, PromptFileError) as error:
                self.output(f"Could not read replacement prompt file: {error}")
                return
        duplicate = next((item for item in self.catalog.prompt_templates.list()
                          if item.id != template.id and item.name == name and item.version == version), None)
        if duplicate is not None:
            self.output(f'Another template already uses "{name}" version {version}.')
            return
        prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
        changed_text = prompt_text != template.prompt_text
        if changed_text:
            self.output(f"Replacement prompt: {len(prompt_text)} characters, {len(prompt_text.splitlines())} lines, SHA-256 {prompt_hash}")
        confirm = self.yes_no("Save template changes", default=True, navigation=True)
        if confirm is not True:
            return
        saved = self.catalog.prompt_templates.update(replace(
            template, name=name, version=version, benchmark_type=benchmark_type, notes=notes,
            is_active=active, prompt_text=prompt_text, prompt_hash=prompt_hash,
        ))
        self.output(f"✓ Prompt template updated: {saved.name} v{saved.version}")
        if changed_text:
            self.output(f"Prompt text updated. SHA-256: {saved.prompt_hash}")

    def export_prompt_template(self, template_id: int) -> None:
        template = self.catalog.prompt_templates.get(template_id)
        if template is None:
            self.output("Prompt template not found.")
            return
        filename = "".join(character if character.isalnum() or character in "._- " else "_" for character in template.name).strip() or "prompt-template"
        path = self.prompt_path("Prompt template export destination", default=f"{filename}.txt", preserve_trailing_separator=True)
        if not isinstance(path, str):
            return
        output_path = resolve_export_destination(path, default_filename=f"{filename}.txt")
        if output_path.suffix.lower() not in {".txt", ".md"}:
            self.output("Prompt template exports must use a .txt or .md extension.")
            return
        if not output_path.parent.exists():
            create = self.yes_no(f'Parent directory "{output_path.parent}" does not exist. Create it?', navigation=True)
            if create is not True:
                return
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                self.output(f"Could not create export directory: {error}")
                return
        try:
            output_path.write_bytes(template.prompt_text.encode("utf-8"))
        except OSError as error:
            self.output(f"Could not export prompt template: {error}")
            return
        self.output(f"Exported prompt template to {output_path}.")

    def delete_prompt_template(self, template_id: int) -> None:
        template = self.catalog.prompt_templates.get(template_id)
        if template is None:
            self.output("Prompt template not found.")
            return
        if self.yes_no(f'Delete prompt template "{template.name}" v{template.version}', navigation=True) is True:
            self.catalog.prompt_templates.delete(template_id)
            self.output("Prompt template deleted.")

    def list_prompt_templates_screen(self) -> None:
        self.render_screen("Prompt Templates")
        templates = self.catalog.prompt_templates.list()
        if templates:
            for number, template in enumerate(templates, start=1):
                self.output(f"{number}) {template.name} v{template.version} ({template.benchmark_type})")
        else:
            self.output("No prompt templates found.")
        self.output("\nB) Back\nQA) Quit BenchPup completely")
        self.ask("Choose an option", navigation=True, default="")

    def select_prompt_template(self) -> int | NavigationSignal:
        self.render_screen("Select Template")
        templates = self.catalog.prompt_templates.list()
        if not templates:
            self.output("No prompt templates found.")
            self.output("\nB) Back\nQA) Quit BenchPup completely")
            self.ask("Choose an option", navigation=True, default="")
            return BACK
        for number, template in enumerate(templates, start=1):
            self.output(f"{number}) {template.name} v{template.version} ({template.benchmark_type})")
        self.output("\nB) Back\nQA) Quit BenchPup completely")
        while True:
            choice = self.ask("Select template number", navigation=True)
            if isinstance(choice, NavigationSignal):
                return BACK
            selected = self.normalized(choice)
            if selected.isdigit() and 1 <= int(selected) <= len(templates):
                template_id = templates[int(selected) - 1].id
                assert template_id is not None, "Persisted prompt template is missing its ID"
                return template_id
            self.output(f"Choose a template number from 1 to {len(templates)}, or B to return.")

    def prompt_templates_screen(self) -> None:
        while True:
            self.render_screen("Prompt Templates", "1) List Templates\n2) View Template\n3) New Template\n4) Import Template From File\n5) Edit Template\n6) Export Template\n7) Delete Template\n\nB) Back\nQA) Quit BenchPup completely")
            choice = self.ask("Choose an option", navigation=True)
            if choice in (BACK, CANCEL, MAIN):
                return
            command = self.normalized(str(choice))
            if command == "1":
                self.list_prompt_templates_screen()
                continue
            if command == "3":
                try:
                    created = self.create_prompt_template()
                    if created not in (BACK, CANCEL, MAIN, None):
                        self.output("✓ PromptTemplate created.")
                except ValueError:
                    self.output("The prompt template could not be created. Check the entered values and try again.")
                continue
            if command == "4":
                self.import_prompt_template_file()
                continue
            actions: dict[str, Callable[[int], None]] = {
                "2": self.view_prompt_template,
                "5": self.edit_prompt_template,
                "6": self.export_prompt_template,
                "7": self.delete_prompt_template,
            }
            action = actions.get(command)
            if action is None:
                self.output("Choose a number from 1 to 7, or B to return.")
                continue
            template_id = self.select_prompt_template()
            if isinstance(template_id, int):
                action(template_id)

    def create_hardware_profile(self) -> HardwareProfile | NavigationSignal | None:
        values: FormValues | NavigationSignal = self._form([("name", "Hardware profile name", None, "text"), ("cpu", "CPU", "", "text"), ("gpu", "GPU", "", "text"), ("vram_gb", "VRAM GB", None, "float"), ("ram_gb", "RAM GB", None, "float"), ("operating_system", "Operating system", "", "text"), ("versions", "Backend versions (LM Studio=0.3, optional)", "", "text"), ("notes", "Notes", "", "text")])
        if isinstance(values, NavigationSignal): return values
        versions_text = self._form_text(values, "versions")
        versions: dict[str, str] = {part.split("=", 1)[0].strip(): part.split("=", 1)[1].strip() for part in versions_text.split(",") if "=" in part}
        profile = HardwareProfile(
            name=self._form_text(values, "name"), cpu=self._form_text(values, "cpu"), gpu=self._form_text(values, "gpu"),
            vram_gb=self._form_float(values, "vram_gb"), ram_gb=self._form_float(values, "ram_gb"),
            operating_system=self._form_text(values, "operating_system"), backend_versions=versions, notes=self._form_text(values, "notes"),
        )
        if not profile.name: self.output("A hardware profile name is required."); return None
        return self.catalog.hardware_profiles.create(profile)

    def show_hardware_preview(self, draft: HardwareProfileDraft) -> None:
        self.output("\nDetected Hardware Profile\n----------------------------------")
        for label, value in (
            ("Profile Name", draft.name), ("Computer Name", draft.computer_name), ("CPU", draft.cpu),
            ("GPU", draft.gpu), ("VRAM", f"{draft.vram_gb} GB" if draft.vram_gb is not None else ""),
            ("RAM", f"{draft.ram_gb} GB" if draft.ram_gb is not None else ""),
            ("Operating System", draft.operating_system), ("Import Source", draft.source_name),
        ):
            self.output(f"{label}: {value or '-'}")
        self.output("----------------------------------")

    def edit_hardware_draft(self, draft: HardwareProfileDraft) -> HardwareProfileDraft | NavigationSignal:
        fields = (
            ("name", "Profile name"), ("computer_name", "Computer name"), ("cpu", "CPU"), ("gpu", "GPU"),
            ("vram_gb", "VRAM GB"), ("ram_gb", "RAM GB"), ("operating_system", "Operating system"), ("notes", "Notes"),
        )
        for name, label in fields:
            current = getattr(draft, name)
            value = self.ask(label, navigation=True, default="" if current is None else str(current))
            if isinstance(value, NavigationSignal):
                return value
            if name in {"vram_gb", "ram_gb"}:
                try:
                    setattr(draft, name, float(value) if value else None)
                except ValueError:
                    self.output(f'"{value}" is not a valid number; keeping the existing value.')
            else:
                setattr(draft, name, str(value))
        return draft

    def imported_hardware_profile(self, draft: HardwareProfileDraft, existing_id: int | None = None) -> HardwareProfile:
        return HardwareProfile(
            name=draft.name or draft.computer_name or f"{draft.source_name} hardware",
            computer_name=draft.computer_name, cpu=draft.cpu, gpu=draft.gpu, vram_gb=draft.vram_gb,
            ram_gb=draft.ram_gb, operating_system=draft.operating_system, backend_versions=draft.backend_versions,
            notes=draft.notes, import_source=draft.source_name, imported_at=now(), id=existing_id,
        )

    def import_hardware_profile(self) -> None:
        choice = self.ask("Hardware profile data source: 1) MSInfo32  2) DXDiag  3) lshw --short  4) Manual", navigation=True)
        if choice in (BACK, CANCEL, MAIN):
            return
        source_name = {"1": "MSInfo32", "2": "DXDiag", "3": "lshw --short"}.get(self.normalized(str(choice)))
        if self.normalized(str(choice)) in {"4", "manual"}:
            self.create_hardware_profile()
            return
        if not source_name:
            self.output("Choose MSInfo32, DXDiag, lshw --short, or Manual.")
            return
        path = self.prompt_path("Hardware profile text file", must_exist=True, extensions=(".txt",))
        if path in (BACK, CANCEL, MAIN, None):
            return
        try:
            raw = Path(str(path)).read_bytes()
            text = decode_hardware_text(raw)
            pairs = parse_key_value_pairs(text)
            if source_name == "MSInfo32" and not pairs:
                self.output("Could not parse MSInfo32 data: no Item/Value fields were found. Export MSInfo32 as a text file and try again.")
                return
            draft = self.hardware_importers.parse(source_name, text)
        except Exception as error:
            self.logger.exception("Hardware profile parsing failed")
            self.output(f"Could not parse the hardware profile file: {error}")
            return
        while True:
            self.show_hardware_preview(draft)
            action = self.ask("Import? Y) Save  E) Edit  C) Cancel", navigation=True, default="y")
            if isinstance(action, NavigationSignal) or self.normalized(str(action)) in {"c", "cancel"}:
                return
            if self.normalized(str(action)) in {"e", "edit"}:
                edited = self.edit_hardware_draft(draft)
                if isinstance(edited, NavigationSignal):
                    return
                draft = edited
                continue
            if self.normalized(str(action)) not in {"y", "yes", "save"}:
                self.output("Choose Y, E, or C.")
                continue
            profile = self.imported_hardware_profile(draft)
            similar = next((item for item in self.catalog.hardware_profiles.list()
                            if item.name.casefold() == profile.name.casefold()
                            or (profile.computer_name and item.computer_name.casefold() == profile.computer_name.casefold())
                            or (profile.cpu and profile.gpu and item.cpu.casefold() == profile.cpu.casefold()
                                and item.gpu.casefold() == profile.gpu.casefold())), None)
            if similar:
                duplicate = self.ask("Existing hardware profile detected. 1) Update existing  2) Create new profile  3) Cancel", navigation=True)
                if isinstance(duplicate, NavigationSignal) or self.normalized(str(duplicate)) in {"3", "cancel"}:
                    return
                if self.normalized(str(duplicate)) == "1":
                    self.catalog.hardware_profiles.update(self.imported_hardware_profile(draft, similar.id))
                    self.output("✓ Hardware profile updated.")
                    return
                if self.normalized(str(duplicate)) != "2":
                    self.output("Choose 1, 2, or 3.")
                    continue
                profile.name = f"{profile.name} (imported)"
            try:
                self.catalog.hardware_profiles.create(profile)
                self.output("✓ Hardware profile imported.")
            except ValueError as error:
                self.output(f"Could not save hardware profile: {error}")
            return

    def collect_score(self, current: ReviewScore | None = None) -> ReviewScore | NavigationSignal:
        self.render_screen("Review Score", "B) Back\nC) Cancel Run\nQA) Quit BenchPup completely")
        accuracy = self.ask_float("Accuracy score (0-5)", default=current.accuracy_score if current else None, navigation=True)
        if isinstance(accuracy, NavigationSignal): return accuracy
        hallucination = self.pick("Hallucination level", LEVELS, current.hallucination_level if current else "Medium", navigation=True)
        reliability = self.pick("Reliability level", LEVELS, current.reliability_level if current else "Medium", navigation=True)
        if isinstance(hallucination, NavigationSignal): return hallucination
        if isinstance(reliability, NavigationSignal): return reliability
        values: FormValues | NavigationSignal = self._form([("depth_score", "Depth score (0-5)", current.depth_score if current else None, "float"), ("signal_noise_score", "Signal/noise score (0-5)", current.signal_noise_score if current else None, "float"), ("actionability_score", "Actionability score (0-5)", current.actionability_score if current else None, "float"), ("seniority_score", "Seniority score (0-5)", current.seniority_score if current else None, "float"), ("overall_score", "Overall score (0-5)", current.overall_score if current else None, "float"), ("strengths", "Strengths", current.strengths if current else "", "text"), ("weaknesses", "Weaknesses", current.weaknesses if current else "", "text"), ("verdict", "Verdict", current.verdict if current else "", "text"), ("notes", "Notes", current.notes if current else "", "text")])
        if isinstance(values, NavigationSignal): return values
        return ReviewScore(
            run_id=current.run_id if current else 0, id=current.id if current else None, accuracy_score=accuracy,
            hallucination_level=hallucination, reliability_level=reliability,
            depth_score=self._form_float(values, "depth_score"), signal_noise_score=self._form_float(values, "signal_noise_score"),
            actionability_score=self._form_float(values, "actionability_score"), seniority_score=self._form_float(values, "seniority_score"),
            overall_score=self._form_float(values, "overall_score"), strengths=self._form_text(values, "strengths"),
            weaknesses=self._form_text(values, "weaknesses"), verdict=self._form_text(values, "verdict"), notes=self._form_text(values, "notes"),
        )

    def collect_attachments(self, draft: list[AttachmentDraft]) -> NavigationSignal | None:
        while True:
            answer = self.yes_no("Add attachment metadata", navigation=True)
            if isinstance(answer, NavigationSignal): return answer
            if not answer: return None
            attachment_type = self.pick("Attachment type", ATTACHMENT_TYPES, "other", navigation=True)
            if isinstance(attachment_type, NavigationSignal): return attachment_type
            file_path = self.prompt_path("File path", must_exist=True)
            if isinstance(file_path, NavigationSignal): return file_path
            if file_path is None: continue
            original_filename = self.ask("Original filename", navigation=True)
            notes = self.ask("Attachment notes", navigation=True, default="")
            if isinstance(original_filename, NavigationSignal): return original_filename
            if isinstance(notes, NavigationSignal): return notes
            if not file_path or not original_filename: self.output("File path and filename are required."); continue
            draft.append({"attachment_type": attachment_type, "file_path": file_path, "original_filename": original_filename, "notes": notes})

    def add_run_wizard(self) -> None:
        state: dict[str, Any] = {"attachments": []}
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

    def attachment_step(self, draft: list[AttachmentDraft]) -> list[AttachmentDraft] | NavigationSignal:
        result = self.collect_attachments(draft)
        return draft if result is None else result

    def review_and_save(self, state: dict[str, Any], steps: list[tuple[str, Callable[[], Any]]]) -> None:
        while True:
            self.render_screen("Review Run")
            self.show_draft(state)
            self.output("\n1) Save Run\n2) Edit a Section\n3) Cancel Run\n\nB) Back\nC) Cancel Run\nQA) Quit BenchPup completely")
            selected = self.ask("Choose an option", navigation=True)
            if selected in (CANCEL, MAIN): self.output("Wizard cancelled."); return
            if selected is BACK: continue
            choice = self.normalized(str(selected))
            if choice == "1":
                try:
                    template = self.catalog.prompt_templates.get(state["prompt_template_id"])
                    assert template is not None
                    run = BenchmarkRun(raw_model_output=state["raw_model_output"], prompt_name=template.name, session_id=state["session_id"], model_profile_id=state["model_profile_id"], benchmark_definition_id=state["benchmark_definition_id"], prompt_template_id=state["prompt_template_id"], hardware_profile_id=state["hardware_profile_id"])
                    saved, _ = self.benchmarks.save_run(run, state["score"])
                    assert saved.id is not None
                    for attachment in state["attachments"]: self.benchmarks.add_attachment(RunAttachment(run_id=saved.id, **attachment))
                    self.output("✓ Benchmark saved."); return
                except ValueError: self.output("The benchmark could not be saved. Check the entered values and try again.")
            elif choice == "2":
                self.render_screen("Edit Run Section", "1) Session\n2) Model\n3) Benchmark\n4) Prompt Template\n5) Hardware Profile\n6) Raw Output\n7) Review Score\n8) Attachments\n\nB) Back\nC) Cancel Run\nQA) Quit BenchPup completely")
                section = self.ask_id("Choose a section", navigation=True)
                if section in (CANCEL, MAIN): return
                if section is BACK: continue
                if not isinstance(section, int): continue
                if not 1 <= section <= len(steps): self.output("Choose a section from 1 to 8."); continue
                index = section - 1
                while index < len(steps):
                    key, action = steps[index]; value = action()
                    if value in (CANCEL, MAIN): return
                    if value is BACK: index = max(0, index - 1); continue
                    state[key] = value; index += 1
            elif choice in {"", "c", "cancel"}: self.output("Wizard cancelled."); return
            elif choice in {"3", "c", "cancel"}:
                self.output("Wizard cancelled."); return
            else: self.output("Choose 1, 2, or 3.")

    def show_draft(self, state: Mapping[str, Any]) -> None:
        model = self.catalog.model_profiles.get(state["model_profile_id"])
        definition = self.catalog.benchmark_definitions.get(state["benchmark_definition_id"])
        template = self.catalog.prompt_templates.get(state["prompt_template_id"])
        assert model is not None and definition is not None and template is not None
        self.output("\nReview benchmark run")
        self.output(f"Model: {model.name} | Benchmark: {definition.name} | Prompt: {template.name} v{template.version}")
        self.output(f"Raw output: {state['raw_model_output'][:120]}")
        self.output(f"Attachments: {len(state['attachments'])} | Overall score: {state['score'].overall_score}")

    def list_runs(self) -> None:
        self.render_screen("Runs")
        runs = self.benchmarks.runs.list()
        if not runs: self.output("No benchmark runs found."); self.pause(); return
        for run in runs:
            assert run.id is not None
            score = self.benchmarks.get_run(run.id)[1]
            self.output(f"#{run.id} | {run.model_snapshot.get('model_name', 'Unknown')} | {run.benchmark_snapshot.get('name') or run.benchmark_snapshot.get('file_path', 'Unknown')} | overall={score.overall_score if score else '-'}")
        self.pause()

    def view_run(self, run_id: int) -> None:
        self.render_screen("Run Details")
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
            self.render_screen("Edit Run", "1) Output\n2) Prompt\n3) Score\n4) Attachment\n\nB) Back\nQA) Quit BenchPup completely")
            selected = self.ask("Choose an option", navigation=True)
            if selected in (CANCEL, MAIN): return
            if selected is BACK: return
            choice = self.normalized(str(selected))
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
                if isinstance(new_score, ReviewScore):
                    assert run.id is not None
                    score = self.benchmarks.update_score(new_score) if score else self.benchmarks.scores.create(replace(new_score, run_id=run.id))
                    self.output("✓ Review score updated.")
            elif choice in {"4", "attachment"}:
                assert run.id is not None
                self.collect_attachments_after_save(run.id)
            else: self.output("Choose output, prompt, score, attachment, or back.")

    def collect_attachments_after_save(self, run_id: int) -> None:
        draft: list[AttachmentDraft] = []
        result = self.collect_attachments(draft)
        if result is None:
            for attachment in draft: self.benchmarks.add_attachment(RunAttachment(run_id=run_id, **attachment))
            if draft: self.output("✓ Attachment metadata saved.")

    def delete_run(self, run_id: int) -> None:
        self.render_screen("Delete Run")
        run, _, _ = self.benchmarks.get_run(run_id)
        if not run: self.output("Run not found."); self.pause(); return
        self.output(f"Delete benchmark #{run.id}?\nModel: {run.model_snapshot.get('model_name', 'Unknown')}\nBenchmark: {run.benchmark_snapshot.get('file_path', 'Unknown')}")
        confirmation = self.ask("Type DELETE to confirm", navigation=True)
        if confirmation in (BACK, CANCEL, MAIN) or str(confirmation).upper() != "DELETE": self.output("Delete cancelled."); return
        self.benchmarks.delete_run(run_id); self.output("✓ Run deleted.")

    def _scoreboard_batches(self) -> dict[int, ScoreboardImportBatch]:
        return {batch.id: batch for batch in self.catalog.scoreboard_import_batches.list() if batch.id is not None}

    def list_scoreboard_entries(self, batch_id: int | None = None) -> None:
        self.render_screen("Scoreboard Entries")
        batches = self._scoreboard_batches()
        entries = self.catalog.scoreboard_entries.list()
        if batch_id is not None:
            entries = [entry for entry in entries if entry.import_batch_id == batch_id]
        if not entries:
            self.output("No scoreboard entries found.")
            return
        for entry in entries:
            batch = batches.get(entry.import_batch_id) if entry.import_batch_id is not None else None
            self.output(
                f"#{entry.id} | {entry.model_name} | score={entry.score if entry.score is not None else '-'} "
                f"| batch={batch.name if batch else '-'} | imported={entry.imported_at}"
            )

    def view_scoreboard_entry(self, entry_id: int) -> None:
        self.render_screen("Scoreboard Entry")
        entry = self.catalog.scoreboard_entries.get(entry_id)
        if not entry:
            self.output("Scoreboard entry not found.")
            return
        batch = self._scoreboard_batches().get(entry.import_batch_id) if entry.import_batch_id is not None else None
        self.output(f"\nScoreboard entry #{entry.id}")
        self.output(f"Batch: {batch.name if batch else '-'}")
        self.output(f"Imported at: {entry.imported_at}")
        self.output(json.dumps(entry.__dict__, indent=2))

    def list_scoreboard_batches(self) -> None:
        self.render_screen("Scoreboard Import Batches")
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
                f"#{batch.id} | {batch.name} | entries={entry_counts.get(batch.id, 0) if batch.id is not None else 0} "
                f"| imported={batch.imported_at} | source={batch.source_file}"
            )

    def scoreboard_screen(self) -> None:
        while True:
            self.render_screen("Scoreboard", "Historical summary imports\n\n1) List Entries\n2) View Entry\n3) List Import Batches\n4) View Entries by Batch\n\nB) Back\nQA) Quit BenchPup completely")
            choice = self.ask("Choose an option", navigation=True)
            if choice in (BACK, CANCEL, MAIN):
                return
            command = self.normalized(str(choice))
            if command in {"1", "list"}:
                self.list_scoreboard_entries()
            elif command in {"2", "view"}:
                entry_id = self.ask_id("Scoreboard entry ID")
                if isinstance(entry_id, int):
                    self.view_scoreboard_entry(entry_id)
            elif command in {"3", "batches"}:
                self.list_scoreboard_batches()
            elif command in {"4", "batch"}:
                batch_id = self.ask_id("Import batch ID")
                if isinstance(batch_id, int):
                    if batch_id not in self._scoreboard_batches():
                        self.output("Import batch not found.")
                    else:
                        self.list_scoreboard_entries(batch_id)
            else:
                self.output("Choose 1, 2, 3, 4, or B to return.")

    def catalog_screen(self, title: str, repository, create, import_action: Callable[[], None] | None = None) -> None:
        """A small, focused catalog screen for one reusable record type."""
        while True:
            self.render_screen(title)
            items = repository.list()
            if items:
                for item in items: self.output(f"{item.id}) {item.name if hasattr(item, 'name') else item.title}")
            else:
                self.output("No records found.")
            action_hint = "\nI) Import raw prompt file" if import_action is not None else ""
            self.output(f"\nN) New{action_hint}\nB) Back\nQA) Quit BenchPup completely")
            choice = self.ask("Choose an option", navigation=True)
            if choice in (BACK, CANCEL, MAIN): return
            if import_action is not None and self.normalized(str(choice)) in {"i", "import"}:
                import_action()
                continue
            if self.normalized(str(choice)) not in {"n", "new"}:
                self.output("Choose N to create, I to import, or B to return.")
                continue
            try:
                item = create()
                if item not in (BACK, CANCEL, MAIN, None): self.output(f"✓ {type(item).__name__} created.")
            except ValueError:
                self.output("The record could not be created. Check the entered values and try again.")

    def set_default_working_directory(self) -> None:
        selected = self.prompt_path(
            "Default Working Directory",
            blank_cancels=True,
            directory_only=True,
            reject_boolean_paths=True,
        )
        if not isinstance(selected, str):
            return
        directory = Path(selected)
        if directory.exists():
            self.settings.set_default_working_directory(directory)
            self.output(f"Default Working Directory saved: {directory}")
            return
        self.output(f"Directory does not exist:\n{directory}")
        create = self.yes_no("Create this directory", default=True, navigation=True)
        if create is not True:
            self.output("Default Working Directory was not changed.")
            return
        if not directory.parent.is_dir():
            self.output(f'Parent directory does not exist: "{directory.parent}". No directory was created.')
            return
        try:
            directory.mkdir()
        except OSError as error:
            self.output(f"Could not create directory: {error}")
            return
        self.settings.set_default_working_directory(directory)
        self.output(f"Default Working Directory saved: {directory}")

    def settings_screen(self) -> None:
        while True:
            current = self.settings.get_default_working_directory()
            self.render_screen("Settings", f"Default Working Directory\nCurrent: {current if current is not None else 'Not configured'}\n\n1) Set Default Working Directory\n2) Clear Default Working Directory\n\nB) Back\nQA) Quit BenchPup completely")
            choice = self.ask("Choose an option", navigation=True)
            if choice in (BACK, CANCEL, MAIN):
                return
            command = self.normalized(str(choice))
            if command in {"1", "set"}:
                self.set_default_working_directory()
            elif command in {"2", "clear"}:
                self.settings.clear_default_working_directory()
                self.output("Default Working Directory cleared.")
            else:
                self.output("Choose 1, 2, or B to return.")

    def not_available(self, name: str, phase: str) -> None:
        self.output(f"{name} will be available in {phase}.")
        self.pause()

    def show_import_mapping(self, preview: ImportPreview) -> None:
        self.output("\nDetected columns:")
        for heading in preview.headings:
            target = preview.mapping[heading]
            marker = "✓" if target else "–"
            self.output(f"{marker} {heading:<20} -> {target or 'ignored'}")

    def edit_import_mapping(self, preview: ImportPreview, mapping_fields: tuple[str, ...] = MAPPING_FIELDS) -> dict[str, str | None] | NavigationSignal:
        mapping: dict[str, str | None] = dict(preview.mapping)
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

    def choose_mapping_profile(self) -> dict[str, str | None] | NavigationSignal | None:
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

    def save_mapping_profile(self, mapping: dict[str, str | None]) -> NavigationSignal | None:
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
        self.render_screen("Import", "1) Benchmark Runs CSV\n2) Scoreboard CSV\n3) Auto-detect CSV Type\n4) Hardware Profile\n5) Prompt Template File\n\nB) Back\nQA) Quit BenchPup completely")
        choice = self.ask("Choose an option", navigation=True)
        if choice in (BACK, CANCEL, MAIN): return
        command = self.normalized(str(choice))
        if command in {"4", "hardware"}:
            self.import_hardware_profile()
            return
        if command in {"5", "prompt", "prompts", "prompt template", "prompt template file"}:
            self.import_prompt_template_file()
            return
        import_type = {"1": "runs", "2": "scoreboard", "3": "auto"}.get(command)
        if not import_type: self.output("Choose 1, 2, 3, 4, or 5."); return
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
        if isinstance(profile_mapping, NavigationSignal): return
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
                if isinstance(mapping, NavigationSignal): return
                preview = self.importer.preview(str(path), mapping, summary=summary_import)
                saved = self.save_mapping_profile(mapping)
                if isinstance(saved, NavigationSignal): return
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
        self.render_screen("Export", "1) Benchmark Runs CSV\n2) Scoreboard CSV\n3) JSONL Training Data\n4) Markdown Report\n5) Scoreboard HTML\n\nB) Back\nQA) Quit BenchPup completely")
        choice = self.ask("Choose an option", navigation=True)
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

    @staticmethod
    def _dataset_filter_value(value: object) -> str:
        if value is None or value == "" or value == frozenset():
            return "Not set"
        if isinstance(value, frozenset):
            return ", ".join(str(item) for item in sorted(value))
        return str(value)

    def _dataset_optional_float(self, label: str, current: float | None) -> float | None | NavigationSignal:
        while True:
            value = self.ask(f"{label} (blank clears; current: {self._dataset_filter_value(current)})", navigation=True)
            if isinstance(value, NavigationSignal):
                return value
            if not value:
                return None
            try:
                return float(value)
            except ValueError:
                self.output(f'"{value}" is not a valid number.')

    def _select_dataset_choice(self, title: str, choices: tuple[str, ...], current: str | None) -> str | None | NavigationSignal:
        lines = [f"Current: {self._dataset_filter_value(current)}", ""]
        lines.extend(f"{number}) {item}" for number, item in enumerate(choices, start=1))
        lines.extend((f"{len(choices) + 1}) Clear filter", "", "B) Back", "QA) Quit BenchPup completely"))
        self.render_screen(title, "\n".join(lines))
        while True:
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return choice
            command = self.normalized(choice)
            if command.isdigit() and 1 <= int(command) <= len(choices):
                return choices[int(command) - 1]
            if command == str(len(choices) + 1):
                return None
            self.output(f"Choose a number from 1 to {len(choices) + 1}, or B to return.")

    def _select_dataset_catalog(self, title: str, items: list[Any], label: Callable[[Any], str], current_id: int | None) -> int | None | NavigationSignal:
        lines = [f"Current: {self._dataset_filter_value(current_id)}", ""]
        lines.extend(f"{number}) {label(item)}" for number, item in enumerate(items, start=1))
        lines.extend((f"{len(items) + 1}) Clear filter", "", "B) Back", "QA) Quit BenchPup completely"))
        self.render_screen(title, "\n".join(lines))
        while True:
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return choice
            command = self.normalized(choice)
            if command.isdigit() and 1 <= int(command) <= len(items):
                item_id = items[int(command) - 1].id
                assert item_id is not None, f"Persisted {title} record is missing its ID"
                return item_id
            if command == str(len(items) + 1):
                return None
            self.output(f"Choose a number from 1 to {len(items) + 1}, or B to return.")

    def _configure_dataset_date_range(self, filters: DatasetFilters) -> DatasetFilters:
        self.render_screen("Date Range", f"Current start: {self._dataset_filter_value(filters.date_from)}\nCurrent end: {self._dataset_filter_value(filters.date_to)}\n\nB) Back\nQA) Quit BenchPup completely")
        start = self.ask("Start date (ISO YYYY-MM-DD; blank clears)", navigation=True)
        if isinstance(start, NavigationSignal):
            return filters
        end = self.ask("End date (ISO YYYY-MM-DD; blank clears)", navigation=True)
        if isinstance(end, NavigationSignal):
            return filters
        try:
            start_date = date.fromisoformat(start) if start else None
            end_date = date.fromisoformat(end) if end else None
        except ValueError:
            self.output("Dates must use ISO format YYYY-MM-DD.")
            return filters
        if start_date is not None and end_date is not None and start_date > end_date:
            self.output("Start date must be on or before end date.")
            return filters
        return replace(filters, date_from=start_date, date_to=end_date)

    def _configure_dataset_run_ids(self, filters: DatasetFilters, *, include: bool) -> DatasetFilters:
        title = "Include Run IDs" if include else "Exclude Run IDs"
        current = filters.include_run_ids if include else filters.exclude_run_ids
        self.render_screen(title, f"Current: {self._dataset_filter_value(current)}\n\nEnter comma-separated positive IDs. Blank clears the filter.\n\nB) Back\nQA) Quit BenchPup completely")
        raw = self.ask("Run IDs", navigation=True)
        if isinstance(raw, NavigationSignal):
            return filters
        if not raw:
            values = frozenset()
        else:
            try:
                values = frozenset(int(part.strip()) for part in raw.split(","))
                if not values or any(value <= 0 for value in values):
                    raise ValueError
            except ValueError:
                self.output("Enter comma-separated positive whole-number run IDs, or leave the field blank to clear it.")
                return filters
        return replace(filters, include_run_ids=values) if include else replace(filters, exclude_run_ids=values)

    def dataset_filters_screen(self, filters: DatasetFilters) -> DatasetFilters:
        while True:
            content = "\n".join((
                f"1) Minimum Overall Score [{self._dataset_filter_value(filters.min_overall)}]",
                f"2) Maximum Hallucination Level [{self._dataset_filter_value(filters.max_hallucination)}]",
                f"3) Minimum Reliability Level [{self._dataset_filter_value(filters.min_reliability)}]",
                f"4) Verdict [{self._dataset_filter_value(filters.verdict)}]",
                f"5) Benchmark Type [{self._dataset_filter_value(filters.benchmark_type)}]",
                f"6) Model [{self._dataset_filter_value(filters.model)}]",
                f"7) Session [{self._dataset_filter_value(filters.session_id)}]",
                f"8) Date Range [{self._dataset_filter_value(filters.date_from)} to {self._dataset_filter_value(filters.date_to)}]",
                f"9) Prompt Template [{self._dataset_filter_value(filters.prompt_template_id)}]",
                f"10) Hardware Profile [{self._dataset_filter_value(filters.hardware_profile_id)}]",
                f"11) Include Run IDs [{self._dataset_filter_value(filters.include_run_ids)}]",
                f"12) Exclude Run IDs [{self._dataset_filter_value(filters.exclude_run_ids)}]",
                f"13) Duplicate Policy [{'Retain exact duplicates' if filters.keep_source_duplicates else 'Skip exact duplicates'}]",
                "14) Reset Filters",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Dataset Filters", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return filters
            command = self.normalized(choice)
            if command == "1":
                value = self._dataset_optional_float("Minimum overall score", filters.min_overall)
                if not isinstance(value, NavigationSignal): filters = replace(filters, min_overall=value)
            elif command in {"2", "3"}:
                title = "Maximum Hallucination Level" if command == "2" else "Minimum Reliability Level"
                current = filters.max_hallucination if command == "2" else filters.min_reliability
                value = self._select_dataset_choice(title, LEVELS, current)
                if not isinstance(value, NavigationSignal):
                    filters = replace(filters, max_hallucination=value) if command == "2" else replace(filters, min_reliability=value)
            elif command == "4":
                value = self.ask("Verdict contains (blank clears)", navigation=True)
                if isinstance(value, str): filters = replace(filters, verdict=value)
            elif command == "5":
                value = self._select_dataset_choice("Benchmark Type", BENCHMARK_TYPES, filters.benchmark_type or None)
                if not isinstance(value, NavigationSignal): filters = replace(filters, benchmark_type=value or "")
            elif command == "6":
                item_id = self._select_dataset_catalog("Select Model", self.catalog.model_profiles.list(), lambda item: f"{item.name} ({item.model_name})", None)
                if isinstance(item_id, int):
                    model = self.catalog.model_profiles.get(item_id)
                    if model is not None: filters = replace(filters, model=model.model_name)
                elif item_id is None: filters = replace(filters, model="")
            elif command == "7":
                value = self._select_dataset_catalog("Select Session", self.catalog.sessions.list(), lambda item: item.title, filters.session_id)
                if not isinstance(value, NavigationSignal): filters = replace(filters, session_id=value)
            elif command == "8": filters = self._configure_dataset_date_range(filters)
            elif command == "9":
                value = self._select_dataset_catalog("Select Prompt Template", self.catalog.prompt_templates.list(), lambda item: f"{item.name} v{item.version}", filters.prompt_template_id)
                if not isinstance(value, NavigationSignal): filters = replace(filters, prompt_template_id=value)
            elif command == "10":
                value = self._select_dataset_catalog("Select Hardware Profile", self.catalog.hardware_profiles.list(), lambda item: item.name, filters.hardware_profile_id)
                if not isinstance(value, NavigationSignal): filters = replace(filters, hardware_profile_id=value)
            elif command == "11": filters = self._configure_dataset_run_ids(filters, include=True)
            elif command == "12": filters = self._configure_dataset_run_ids(filters, include=False)
            elif command == "13":
                value = self._select_dataset_choice("Duplicate Policy", ("Skip exact source duplicates", "Retain exact source duplicates"), "Retain exact source duplicates" if filters.keep_source_duplicates else "Skip exact source duplicates")
                if not isinstance(value, NavigationSignal) and value is not None: filters = replace(filters, keep_source_duplicates=value.startswith("Retain"))
            elif command == "14": filters = DatasetFilters()
            else: self.output("Choose a number from 1 to 14, or B to return.")

    def _configure_redaction_terms(self, redaction: RedactionConfig, *, regex: bool) -> RedactionConfig:
        title = "Custom Regex Patterns" if regex else "Literal Terms"
        values = redaction.regex_patterns if regex else redaction.literals
        while True:
            lines = [f"{number}) {value}" for number, value in enumerate(values, start=1)] or ["No rules configured."]
            lines.extend(("", "1) Add", "2) Remove", "3) Reset", "", "B) Back", "QA) Quit BenchPup completely"))
            self.render_screen(title, "\n".join(lines))
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal): return redaction
            command = self.normalized(choice)
            if command == "1":
                value = self.ask("Regex pattern" if regex else "Literal term", navigation=True)
                if not isinstance(value, str) or not value: continue
                if regex:
                    try: re.compile(value)
                    except re.error as error:
                        self.output(f"Invalid custom regex: {error}"); continue
                values = (*values, value)
            elif command == "2":
                if not values:
                    self.output("There are no rules to remove."); continue
                remove = self.ask("Rule number to remove", navigation=True)
                if not isinstance(remove, str) or not remove.isdigit() or not 1 <= int(remove) <= len(values):
                    self.output("Choose a listed rule number."); continue
                values = tuple(value for number, value in enumerate(values, start=1) if number != int(remove))
            elif command == "3": values = ()
            else:
                self.output("Choose 1, 2, 3, or B to return."); continue
            redaction = replace(redaction, regex_patterns=values) if regex else replace(redaction, literals=values)

    def _configure_redaction_toggle(self, label: str, enabled: bool) -> bool | NavigationSignal:
        self.render_screen(label, f"Current: {'Enabled' if enabled else 'Disabled'}\n\n1) Enable\n2) Disable\n\nB) Back\nQA) Quit BenchPup completely")
        while True:
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal): return choice
            if self.normalized(choice) == "1": return True
            if self.normalized(choice) == "2": return False
            self.output("Choose 1, 2, or B to return.")

    def dataset_redaction_preview(self, filters: DatasetFilters, redaction: RedactionConfig) -> None:
        preview = self.datasets.preview(filters, redaction)
        rule_counts = ", ".join(f"{rule}: {count}" for rule, count in sorted(preview.redaction_counts.items())) or "None"
        samples = []
        for number, record in enumerate(preview.records[:3], start=1):
            output = str(record["input"]["raw_model_output"]).replace("\n", " ")
            samples.append(f"Sample {number}: {output[:180]}")
        content = "\n".join((
            f"Eligible records: {len(preview.records)}",
            f"Redactions: {preview.redactions}",
            f"Redactions by rule: {rule_counts}",
            f"Post-redaction collisions: {preview.post_redaction_collisions}",
            "",
            *(samples or ["No eligible records to preview."]),
            "",
            "B) Back",
            "QA) Quit BenchPup completely",
        ))
        self.render_screen("Redaction Preview", content)
        self.ask("Choose an option", navigation=True)

    def dataset_redaction_screen(self, filters: DatasetFilters, redaction: RedactionConfig) -> RedactionConfig:
        while True:
            content = "\n".join((
                f"1) Literal Terms [{len(redaction.literals)} configured]",
                f"2) Paths [{'Enabled' if redaction.redact_paths else 'Disabled'}]",
                f"3) Usernames [{'Enabled' if redaction.redact_usernames else 'Disabled'}]",
                f"4) Email Addresses [{'Enabled' if redaction.redact_email else 'Disabled'}]",
                f"5) Hostnames and IP Addresses [{'Enabled' if redaction.redact_hosts_ips else 'Disabled'}]",
                f"6) Custom Regex Patterns [{len(redaction.regex_patterns)} configured]",
                "7) Preview Redactions",
                "8) Reset Redaction Rules",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Redaction", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal): return redaction
            command = self.normalized(choice)
            if command == "1": redaction = self._configure_redaction_terms(redaction, regex=False)
            elif command in {"2", "3", "4", "5"}:
                field = {"2": "redact_paths", "3": "redact_usernames", "4": "redact_email", "5": "redact_hosts_ips"}[command]
                value = self._configure_redaction_toggle({"redact_paths": "Paths", "redact_usernames": "Usernames", "redact_email": "Email Addresses", "redact_hosts_ips": "Hostnames and IP Addresses"}[field], bool(getattr(redaction, field)))
                if not isinstance(value, NavigationSignal): redaction = replace(redaction, **{field: value})
            elif command == "6": redaction = self._configure_redaction_terms(redaction, regex=True)
            elif command == "7": self.dataset_redaction_preview(filters, redaction)
            elif command == "8": redaction = RedactionConfig()
            else: self.output("Choose a number from 1 to 8, or B to return.")

    def _dataset_preview_screen(self, filters: DatasetFilters, redaction: RedactionConfig) -> NavigationSignal | None:
        preview = self.datasets.preview(filters, redaction)
        content = "\n".join((
            f"Eligible: {len(preview.records)}",
            f"Excluded: {sum(preview.excluded.values())}",
            f"Warnings: {sum(preview.warnings.values())}",
            f"Source duplicates: {preview.source_duplicates}",
            f"Fingerprint duplicates: {preview.fingerprint_duplicates}",
            f"Near duplicates: {preview.near_duplicates}",
            f"Post-redaction collisions: {preview.post_redaction_collisions}",
            f"Redactions: {preview.redactions}",
            f"Exclusions: {preview.excluded or 'None'}",
            f"Warnings: {preview.warnings or 'None'}",
            "",
            "B) Back",
            "QA) Quit BenchPup completely",
        ))
        self.render_screen("Dataset Preview", content)
        choice = self.ask("Choose an option", navigation=True)
        return choice if isinstance(choice, NavigationSignal) else None

    def dataset_builder_screen(self) -> None:
        filters, redaction = DatasetFilters(), RedactionConfig()
        while True:
            self.render_screen("Dataset Builder", "1) Build JSONL Dataset\n2) Preview Eligible Runs\n3) Configure Filters\n4) Configure Redaction\n5) Validate Existing Dataset\n\nB) Back\nQA) Quit BenchPup completely")
            choice = self.ask("Choose an option", navigation=True)
            if choice in (BACK, CANCEL, MAIN): return
            command = self.normalized(str(choice))
            if command == "3": filters = self.dataset_filters_screen(filters); continue
            if command == "4": redaction = self.dataset_redaction_screen(filters, redaction); continue
            if command == "2": self._dataset_preview_screen(filters, redaction); continue
            if command == "1":
                preview = self.datasets.preview(filters, redaction)
                if isinstance(self._dataset_preview_screen(filters, redaction), NavigationSignal): continue
                path = self.prompt_path("Dataset destination", default="dataset.jsonl", preserve_trailing_separator=True)
                if not isinstance(path, str): continue
                output_path = self.prepare_export_destination(path, "JSONL Dataset")
                if not isinstance(output_path, Path): continue
                if output_path.exists() or output_path.with_suffix(output_path.suffix + ".manifest.json").exists():
                    self.output("Dataset export cancelled; staged replacement does not overwrite existing files automatically."); continue
                try:
                    saved, manifest = self.datasets.write(output_path, preview, filters)
                    self.output(f"Dataset written: {saved}\nManifest written: {manifest}")
                except (OSError, DatasetOutputError, FileExistsError) as error: self.output(f"Dataset export failed: {error}")
                continue
            if command == "5":
                path = self.prompt_path("Existing dataset JSONL", must_exist=True, extensions=(".jsonl",))
                if isinstance(path, str):
                    validation = self.datasets.validate_dataset(path)
                    self.output(f"Valid JSONL v1 records: {validation.record_count}" if validation.state == "success" else f"Dataset validation failed: {validation.message}")
                continue
            self.output("Choose 1 to 5, or B to return.")

    def backup_data(self) -> None:
        self.render_screen("Backup BenchPup Data", "B) Back\nQA) Quit BenchPup completely")
        backup_directory = self.benchmarks.database.path.parent.parent / "backups"
        try:
            backup_directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self.output(f"Backup failed: could not create backups folder: {error}")
            return
        default = backup_directory / f"benchpup-backup-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.json"
        path = self.prompt_path("Backup destination", default=str(default), preserve_trailing_separator=True)
        if not isinstance(path, str):
            return
        output_path = self.prepare_export_destination(path, "BenchPup Backup")
        if not isinstance(output_path, Path):
            return
        if not output_path.suffix:
            output_path = output_path.with_suffix(".json")
        try:
            saved = self.archives.export(output_path, APP_VERSION)
            preview = self.archives.preview(self.archives.load(saved))
            self.output("\n✓ Backup completed successfully\n")
            self.output(f"Location:\n{saved}\n")
            self.output(f"Archive Version: {preview['archive_version']}")
            self.output(f"Schema Version: {preview['schema_version']}")
            self.output(f"BenchPup Version: {preview['benchpup_version']}")
            self.output(f"Archive Size: {self._format_file_size(saved.stat().st_size)}")
            self._show_archive_counts(preview["counts"])
        except ArchiveError as error:
            self.output(f"Backup failed: {error}")

    def restore_data(self) -> None:
        self.render_screen("Restore BenchPup Data", "B) Back\nQA) Quit BenchPup completely")
        path = self.prompt_path("Archive file", must_exist=True, extensions=(".json",))
        if not isinstance(path, str):
            return
        try:
            archive = self.archives.load(path)
            preview = self.archives.preview(archive)
        except ArchiveError as error:
            self.output(f"Restore cancelled: {error}")
            return
        self.output("\nRestore Archive\n---------------")
        self.output(f"Created: {preview['created_at']}")
        self.output(f"BenchPup Version: {preview['benchpup_version']}")
        self.output(f"Archive Version: {preview['archive_version']}")
        self.output(f"Schema Version: {preview['schema_version']}")
        self._show_archive_counts(preview["counts"])
        for warning in preview["warnings"]:
            self.output(f"Warning: {warning}")
        self.output("\n1) Preview Only\n2) Merge into Current Database\n3) Replace Current Database\nC) Cancel\nQA) Quit BenchPup completely")
        action = self.ask("Choose an option", navigation=True)
        if not isinstance(action, str) or action in {"", "1"}:
            return
        try:
            if action == "2":
                report = self.archives.merge(archive)
                self._show_restore_summary("Merge", report)
            elif action == "3":
                confirmation = self.ask("Type RESTORE to replace the current database", navigation=True)
                if confirmation != "RESTORE":
                    self.output("Replace restore cancelled.")
                    return
                safety = self.archives.replace(archive, APP_VERSION)
                self._show_restore_summary("Replace", self.archives.last_restore_report, safety)
            else:
                self.output("Choose 1, 2, 3, or C.")
        except ArchiveError as error:
            self.logger.exception("Archive restore failed")
            self.output(f"Restore failed; no partial changes were written: {error}")

    @staticmethod
    def _format_file_size(size: int) -> str:
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    def _show_archive_counts(self, counts: Mapping[str, int]) -> None:
        self.output("\nContents")
        for table in TABLES:
            self.output(f"{ARCHIVE_LABELS[table]:.<26}{counts.get(table, 0):>6}")

    def _show_restore_summary(self, mode: str, report, safety_backup: Path | None = None) -> None:
        assert report is not None
        self.output("\n✓ Restore completed successfully\n")
        self.output(f"Mode: {mode}\n")
        self.output(f"{'Entity':<26}{'Created':>8}{'Skipped':>8}{'Updated':>8}{'Failed':>8}")
        for table in TABLES:
            self.output(
                f"{ARCHIVE_LABELS[table]:<26}{report.created[table]:>8}{report.skipped[table]:>8}"
                f"{report.updated[table]:>8}{report.failed[table]:>8}"
            )
        if safety_backup:
            self.output(f"\nSafety backup created:\n{safety_backup}")

    def help(self) -> None:
        self.render_screen("Help", "Commands: add, list, view, edit, delete, reference data.\n\n"
            "Navigation\n"
            "Q   Back / Cancel current screen\n"
            "QA  Quit BenchPup completely\n"
            "    Also: qa, quit all, quit a (case-insensitive; extra spaces allowed).\n\nB) Back\nQA) Quit BenchPup completely")
        self.pause()

    def select_run(self) -> int | NavigationSignal:
        runs = self.benchmarks.runs.list()
        lines = [
            f"{number}) {run.model_snapshot.get('model_name', 'Unknown')} | "
            f"{run.benchmark_snapshot.get('name') or run.benchmark_snapshot.get('file_path', 'Unknown')}"
            for number, run in enumerate(runs, start=1)
        ]
        if not lines:
            lines.append("No benchmark runs found.")
        lines.extend(("", "B) Back", "QA) Quit BenchPup completely"))
        self.render_screen("Select Run", "\n".join(lines))
        while True:
            choice = self.ask("Choose an option", navigation=True)
            if choice in (BACK, CANCEL, MAIN):
                return BACK
            selected = self.normalized(str(choice))
            if selected.isdigit() and 1 <= int(selected) <= len(runs):
                run_id = runs[int(selected) - 1].id
                assert run_id is not None, "Persisted benchmark run is missing its ID"
                return run_id
            self.output(f"Choose a number from 1 to {len(runs)}, or B to return.")

    def show_main_menu(self) -> None:
        runs = len(self.benchmarks.runs.list())
        scoreboard_entries = len(self.catalog.scoreboard_entries.list())
        models = len(self.catalog.model_profiles.list())
        sessions = len(self.catalog.sessions.list())
        database_name = self.benchmarks.database.path.name
        self.render_screen("Main", f"Database : {database_name}\nRuns     : {runs}\nScoreboard entries : {scoreboard_entries}\nModels   : {models}\nSessions : {sessions}\n\nRuns\n----\n1) Add Run\n2) List Runs\n3) View Run\n4) Edit Run\n5) Delete Run\n\nReference Data\n--------------\n6) Sessions\n7) Models\n8) Benchmarks\n9) Prompt Templates\n10) Hardware Profiles\n\nData\n----\n11) Import\n12) Export\n13) Scoreboard\n14) Backup\n15) Restore\n16) Dataset Builder\n\nHelp\n----\nH) Help\nS) Settings\nQ) Quit\nQA) Quit BenchPup completely")

    def run(self) -> None:
        try:
            self._run_loop()
        except QuitApplication:
            self.output("Exiting BenchPup.")

    def _run_loop(self) -> None:
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
            "14": "backup", "backup": "backup", "15": "restore", "restore": "restore",
            "16": "dataset", "dataset": "dataset",
            "reference": "sessions", "reference-data": "sessions", "s": "settings", "settings": "settings", "h": "help", "help": "help",
        }
        while True:
            self.show_main_menu()
            raw = self.ask("Choose an option")
            if raw is MAIN:
                self.output("Exiting BenchPup.")
                return
            command = self.normalized(str(raw))
            if command in QUIT_WORDS: return
            command = commands.get(command)
            try:
                if command == "add": self.add_run_wizard()
                elif command == "list": self.list_runs()
                elif command in {"view", "edit", "delete"}:
                    run_id = self.select_run()
                    if not isinstance(run_id, int): continue
                    {"view": self.view_run, "edit": self.edit_run, "delete": self.delete_run}[command](run_id)
                elif command == "sessions": self.catalog_screen("Sessions", self.catalog.sessions, self.create_session)
                elif command == "models": self.catalog_screen("Model Profiles", self.catalog.model_profiles, self.create_model_profile)
                elif command == "benchmarks": self.catalog_screen("Benchmark Definitions", self.catalog.benchmark_definitions, self.create_definition)
                elif command == "prompts": self.prompt_templates_screen()
                elif command == "hardware": self.catalog_screen("Hardware Profiles", self.catalog.hardware_profiles, self.create_hardware_profile)
                elif command == "import": self.import_screen()
                elif command == "export": self.export_screen()
                elif command == "scoreboard": self.scoreboard_screen()
                elif command == "backup": self.backup_data()
                elif command == "restore": self.restore_data()
                elif command == "dataset": self.dataset_builder_screen()
                elif command == "settings": self.settings_screen()
                elif command == "help": self.help()
                else: self.output("Choose a menu number or command. Type H for help.")
            except KeyboardInterrupt:
                self.output("\nOperation cancelled.")
            except Exception:
                self.logger.exception("Unexpected CLI error")
                self.output("An unexpected error occurred.\nSee logs/error.log for details.")
