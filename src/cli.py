from __future__ import annotations

import json
import logging
import csv
import os
import re
import sys
import webbrowser
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TypeAlias, TypedDict, cast
from prompt_toolkit import prompt as toolkit_prompt
from prompt_toolkit.completion import PathCompleter

from engine.database import EngineDatabase, MIGRATIONS
from engine.domain import ATTACHMENT_TYPES, BENCHMARK_TYPES, LEVELS, BenchmarkDefinition, BenchmarkRun, BenchmarkSession, HardwareProfile, ModelProfile, PromptTemplate, ReviewScore, RunAttachment, ScoreboardImportBatch, now, prompt_hash_for
from engine.services import BenchmarkService, CatalogService
from engine.importers import CsvImportService, ImportPreview, MAPPING_FIELDS, SUMMARY_MAPPING_FIELDS
from engine.exporters import export_benchmark_runs_csv, export_combined_markdown, export_jsonl_training_data, export_scoreboard_csv, export_scoreboard_html
from engine.hardware_importers import HardwareImporterRegistry, HardwareProfileDraft, decode_hardware_text, parse_key_value_pairs
from engine.path_completion import normalize_path, resolve_export_destination
from engine.archive import ArchiveError, ArchiveService, TABLES
from engine.prompt_file_importer import PromptFileError, decode_prompt_file, prompt_preview
from engine.settings import DefaultWorkingDirectorySettings
from engine.datasets import DatasetBuilder, DatasetFilters, DatasetWriteResult, DatasetWriteStatus, RedactionConfig
from engine.comparisons import ComparisonService, ModelComparisonResult, SessionComparisonResult
from engine.html_reporting import AnalyticsSourceFamily, HtmlAnalyticsReport, HtmlAnalyticsReportOptions
from engine.reporting import (
    BenchmarkReportFilters,
    BenchmarkRunReport,
    HardwareReport,
    ModelLeaderboardReport,
    ReportWriteResult,
    ReportWriteStatus,
    ReportTemplateOptions,
    ReportingService,
    ScoreboardReport,
    ScoreboardReportFilters,
    SessionReport,
)
from engine.statistics import (
    BenchmarkStatisticsFilters,
    ScoreboardStatisticsFilters,
    TimeBucketGranularity,
    model_snapshot_name,
    normalize_model_identity,
)
from engine.trends import TrendGrouping, TrendReport, TrendService

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


@dataclass(frozen=True)
class BenchmarkReportOptions:
    """Session-local options for detailed benchmark-run reports."""

    title: str = "Benchmark Run Report"
    filters: BenchmarkReportFilters = field(default_factory=BenchmarkReportFilters)
    include_prompt_text: bool = False
    include_raw_model_output: bool = False
    include_attachment_metadata: bool = False
    template_id: str = "standard"
    destination: str = ""


@dataclass(frozen=True)
class ScoreboardReportOptions:
    """Session-local options for historical scoreboard reports."""

    title: str = "Historical Scoreboard Report"
    filters: ScoreboardReportFilters = field(default_factory=ScoreboardReportFilters)
    template_id: str = "standard"
    destination: str = ""


@dataclass(frozen=True)
class LeaderboardReportOptions:
    """Session-local options for model leaderboard reports."""

    title: str = "Model Leaderboard"
    filters: BenchmarkReportFilters = field(default_factory=BenchmarkReportFilters)
    include_model_details: bool = False
    template_id: str = "standard"
    destination: str = ""


@dataclass(frozen=True)
class SessionReportOptions:
    """Session-local options for one session report."""

    title: str = "Session Report"
    filters: BenchmarkReportFilters = field(default_factory=BenchmarkReportFilters)
    include_prompt_text: bool = False
    include_raw_model_output: bool = False
    include_attachment_metadata: bool = False
    template_id: str = "standard"
    destination: str = ""


@dataclass(frozen=True)
class HardwareReportOptions:
    """Session-local options for historical hardware reports."""

    title: str = "Hardware Report"
    filters: BenchmarkReportFilters = field(default_factory=BenchmarkReportFilters)
    include_prompt_text: bool = False
    include_raw_model_output: bool = False
    include_attachment_metadata: bool = False
    include_hardware_details: bool = False
    template_id: str = "standard"
    destination: str = ""


@dataclass(frozen=True)
class ModelComparisonOptions:
    """Session-local options for a snapshot-based model comparison."""

    title: str = "Model Comparison"
    models: tuple[str, ...] = ()
    filters: BenchmarkStatisticsFilters = field(default_factory=BenchmarkStatisticsFilters)
    destination: str = ""

    @property
    def selected_models(self) -> tuple[str, ...]:
        return self.models


@dataclass(frozen=True)
class SessionComparisonOptions:
    """Session-local options for a session comparison."""

    title: str = "Session Comparison"
    sessions: tuple[int, ...] = ()
    filters: BenchmarkStatisticsFilters = field(default_factory=BenchmarkStatisticsFilters)
    destination: str = ""

    @property
    def selected_sessions(self) -> tuple[int, ...]:
        return self.sessions


@dataclass(frozen=True)
class BenchmarkTrendOptions:
    """Session-local options for historical BenchmarkRun trends."""

    title: str = "Benchmark Run Trends"
    interval: TimeBucketGranularity = TimeBucketGranularity.DAY
    grouping: TrendGrouping = TrendGrouping.OVERALL
    filters: BenchmarkStatisticsFilters = field(default_factory=BenchmarkStatisticsFilters)
    include_empty_buckets: bool = False
    include_series_details: bool = False
    destination: str = ""


@dataclass(frozen=True)
class ScoreboardTrendOptions:
    """Session-local options for historical ScoreboardEntry trends."""

    title: str = "Historical Scoreboard Trends"
    interval: TimeBucketGranularity = TimeBucketGranularity.DAY
    grouping: TrendGrouping = TrendGrouping.OVERALL
    filters: ScoreboardStatisticsFilters = field(default_factory=ScoreboardStatisticsFilters)
    include_empty_buckets: bool = False
    include_series_details: bool = False
    destination: str = ""


REPORT_BENCHMARK_TYPE_LABELS = {
    "code_review": "Code review",
    "code_generation": "Code generation",
    "revision": "Revision",
    "review_the_review": "Review the review",
}
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
        self.reporting = ReportingService(self.benchmarks, self.catalog)
        self.comparisons = ComparisonService(self.benchmarks, self.catalog)
        self.trends = TrendService(self.benchmarks, self.catalog)
        self.importer = CsvImportService(self.benchmarks)
        self.hardware_importers = HardwareImporterRegistry()
        self.archives = ArchiveService(database)
        self.datasets = DatasetBuilder(self.benchmarks, benchpup_version=APP_VERSION, schema_version=max(version for version, _ in MIGRATIONS))
        self.settings = DefaultWorkingDirectorySettings(database_path)
        self._benchmark_report_options = BenchmarkReportOptions()
        self._scoreboard_report_options = ScoreboardReportOptions()
        self._leaderboard_report_options = LeaderboardReportOptions()
        self._session_report_options = SessionReportOptions()
        self._hardware_report_options = HardwareReportOptions()
        self._html_analytics_options = HtmlAnalyticsReportOptions()
        self._model_comparison_options = ModelComparisonOptions()
        self._session_comparison_options = SessionComparisonOptions()
        self._benchmark_trend_options = BenchmarkTrendOptions()
        self._scoreboard_trend_options = ScoreboardTrendOptions()
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
            "Detailed Benchmark Run Report": ("benchmark-run-report.md", ".md"),
            "Historical Scoreboard Report": ("scoreboard-report.md", ".md"),
            "Benchmark Run Trends": ("benchmark-run-trends.md", ".md"),
            "Historical Scoreboard Trends": ("scoreboard-trends.md", ".md"),
            "Model Leaderboard": ("model-leaderboard.md", ".md"),
            "Model Comparison": ("model-comparison.md", ".md"),
            "Session Report": ("session-report.md", ".md"),
            "Session Comparison": ("session-comparison.md", ".md"),
            "Hardware Report": ("hardware-report.md", ".md"),
            "HTML Analytics Report": ("benchpup-analytics.html", ".html"),
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
            prompt_hash=prompt_hash_for(prompt_text), benchmark_type=benchmark_type, notes=notes,
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
        prompt_hash = prompt_hash_for(prompt_text)
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
        prompt_hash = prompt_hash_for(prompt_text)
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
        self.render_screen("Export", "1) Benchmark Runs CSV\n2) Scoreboard CSV\n3) JSONL Training Data\n4) Markdown Report\n5) Scoreboard HTML\n6) HTML Analytics Report\n\nB) Back\nQA) Quit BenchPup completely")
        choice = self.ask("Choose an option", navigation=True)
        if choice in (BACK, CANCEL, MAIN): return
        if self.normalized(str(choice)) == "6":
            self._html_analytics_options = self._html_analytics_screen(self._html_analytics_options)
            return
        exporters = {"1": ("Benchmark Runs CSV", export_benchmark_runs_csv), "2": ("Scoreboard CSV", export_scoreboard_csv), "3": ("JSONL training data", export_jsonl_training_data), "4": ("Markdown report", export_combined_markdown), "5": ("Scoreboard HTML", export_scoreboard_html)}
        selected = exporters.get(self.normalized(str(choice)))
        if not selected: self.output("Choose 1, 2, 3, 4, 5, or 6."); return
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
    def _html_analytics_source_label(value: AnalyticsSourceFamily | str) -> str:
        source = value.value if isinstance(value, AnalyticsSourceFamily) else str(value)
        return {
            AnalyticsSourceFamily.BENCHMARK_RUNS.value: "Benchmark Runs",
            AnalyticsSourceFamily.SCOREBOARD.value: "Historical Scoreboard",
            AnalyticsSourceFamily.COMBINED.value: "Combined dashboard",
        }.get(source, source)

    def _html_analytics_source_choice(
        self,
        current: AnalyticsSourceFamily | str,
    ) -> AnalyticsSourceFamily | NavigationSignal:
        choices = [
            ("Benchmark Runs", AnalyticsSourceFamily.BENCHMARK_RUNS),
            ("Historical Scoreboard", AnalyticsSourceFamily.SCOREBOARD),
            ("Combined dashboard (separate source-family sections)", AnalyticsSourceFamily.COMBINED),
        ]
        selected = self._report_vertical_choice("HTML Analytics Source", choices, current)
        if isinstance(selected, NavigationSignal):
            return selected
        return selected if isinstance(selected, AnalyticsSourceFamily) else AnalyticsSourceFamily.BENCHMARK_RUNS

    def _html_analytics_benchmark_filters_screen(
        self,
        filters: BenchmarkStatisticsFilters,
    ) -> BenchmarkStatisticsFilters:
        """Reuse the existing catalog/snapshot filter screen for analytics."""

        base = BenchmarkReportFilters(
            benchmark_type=filters.benchmark_type,
            benchmark=filters.benchmark,
            session_id=filters.session_id,
            session=filters.session,
            hardware_profile_id=filters.hardware_profile_id,
            hardware=filters.hardware,
            model=filters.model,
            include_run_ids=filters.include_run_ids,
            exclude_run_ids=filters.exclude_run_ids,
            include_deleted=filters.include_deleted,
        )
        updated = self._report_filters_screen(base)
        return replace(
            filters,
            benchmark_type=updated.benchmark_type,
            benchmark=updated.benchmark,
            session_id=updated.session_id,
            session=updated.session,
            hardware_profile_id=updated.hardware_profile_id,
            hardware=updated.hardware,
            model=updated.model,
            include_run_ids=updated.include_run_ids,
            exclude_run_ids=updated.exclude_run_ids,
            include_deleted=updated.include_deleted,
        )

    def _html_analytics_scoreboard_filters_screen(
        self,
        filters: ScoreboardStatisticsFilters,
    ) -> ScoreboardStatisticsFilters:
        while True:
            batch = self.catalog.scoreboard_import_batches.get(filters.batch_id) if filters.batch_id is not None else None
            content = "\n".join((
                f"1) Scoreboard Import Batch [{batch.name if batch else ('Unavailable selection' if filters.batch_id is not None else 'Not set')} ]",
                f"2) Model Text Filter [{self._report_value(filters.model)}]",
                "3) Reset Scoreboard Filters",
                "",
                f"Active: {self._report_value(filters.model) if filters.model else 'All non-deleted scoreboard entries'}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("HTML Analytics Scoreboard Filters", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return filters
            command = self.normalized(choice)
            if command == "1":
                selected = self._report_catalog_choice(
                    "Select Analytics Import Batch",
                    self.catalog.scoreboard_import_batches.list(),
                    lambda item: f"{item.name} ({item.source_file})",
                    lambda item: item.id,
                    filters.batch_id,
                )
                if not isinstance(selected, NavigationSignal):
                    filters = replace(filters, batch_id=selected)
            elif command == "2":
                value = self.ask("Model text filter (blank clears)", navigation=True)
                if isinstance(value, str):
                    filters = replace(filters, model=value)
            elif command == "3":
                filters = ScoreboardStatisticsFilters()
            else:
                self.output("Choose 1, 2, or 3, or B to return.")

    def _html_analytics_sections_screen(
        self,
        options: HtmlAnalyticsReportOptions,
    ) -> HtmlAnalyticsReportOptions:
        toggles = (
            ("3", "include_detailed_tables", "Detailed tables"),
            ("4", "include_model_quality_chart", "Model quality/score chart"),
            ("5", "include_speed_chart", "Speed charts"),
            ("6", "include_score_distribution", "Score distributions"),
            ("7", "include_categorical_distributions", "Categorical distributions"),
            ("8", "include_trends", "Trend charts"),
            ("9", "include_hardware_summary", "Hardware summary"),
            ("10", "compact_layout", "Compact layout"),
        )
        while True:
            lines = [
                f"1) Source [{self._html_analytics_source_label(options.source_family)}]",
                "2) Report Title",
            ]
            lines.extend(
                f"{number}) {label} [{'Yes' if bool(getattr(options, field_name)) else 'No'}]"
                for number, field_name, label in toggles
            )
            lines.extend(("11) Reset presentation options", "", "B) Back", "QA) Quit BenchPup completely"))
            self.render_screen("HTML Analytics Sections", "\n".join(lines))
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                selected = self._html_analytics_source_choice(cast(AnalyticsSourceFamily, options.source_family))
                if not isinstance(selected, NavigationSignal):
                    options = replace(
                        options,
                        source_family=selected,
                        include_benchmark_run_dashboard=None,
                        include_scoreboard_dashboard=None,
                    )
            elif command == "2":
                value = self.ask("Report title", navigation=True, default=options.title)
                if isinstance(value, str) and value.strip():
                    options = replace(options, title=value.strip())
            elif command in {number for number, _, _ in toggles}:
                field_name = next(field_name for number, field_name, _ in toggles if number == command)
                value = self._configure_redaction_toggle(str(next(label for number, _, label in toggles if number == command)), bool(getattr(options, field_name)))
                if not isinstance(value, NavigationSignal):
                    options = replace(options, **{field_name: value})
            elif command == "11":
                options = HtmlAnalyticsReportOptions(source_family=options.source_family)
            else:
                self.output("Choose a number from 1 to 11, or B to return.")

    def _html_analytics_options_screen(
        self,
        options: HtmlAnalyticsReportOptions,
    ) -> HtmlAnalyticsReportOptions:
        while True:
            content = "\n".join((
                f"1) Presentation and Source [{self._html_analytics_source_label(options.source_family)}]",
                "2) Benchmark Run Filters",
                "3) Scoreboard Filters",
                f"4) Trend Interval [{self._trend_interval_label(options.interval)}]",
                "5) Reset All Analytics Options",
                "",
                f"Title: {options.title}",
                f"Detailed tables: {'Included' if options.include_detailed_tables else 'Excluded'}",
                f"Quality charts: {'Included' if options.include_model_quality_chart else 'Excluded'}",
                f"Speed charts: {'Included' if options.include_speed_chart else 'Excluded'}",
                f"Score distributions: {'Included' if options.include_score_distribution else 'Excluded'}",
                f"Categorical distributions: {'Included' if options.include_categorical_distributions else 'Excluded'}",
                f"Trends: {'Included' if options.include_trends else 'Excluded'}",
                f"Hardware summary: {'Included' if options.include_hardware_summary else 'Excluded'}",
                f"Compact layout: {'Enabled' if options.compact_layout else 'Disabled'}",
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("HTML Analytics Report Options", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                options = self._html_analytics_sections_screen(options)
            elif command == "2":
                options = replace(options, benchmark_filters=self._html_analytics_benchmark_filters_screen(options.benchmark_filters))
            elif command == "3":
                options = replace(options, scoreboard_filters=self._html_analytics_scoreboard_filters_screen(options.scoreboard_filters))
            elif command == "4":
                choices = [
                    ("Day", TimeBucketGranularity.DAY),
                    ("Week", TimeBucketGranularity.WEEK),
                    ("Month", TimeBucketGranularity.MONTH),
                ]
                selected = self._report_vertical_choice("HTML Analytics Trend Interval", choices, options.interval)
                if not isinstance(selected, NavigationSignal) and selected is not None:
                    options = replace(options, trend_interval=selected)
            elif command == "5":
                options = HtmlAnalyticsReportOptions(source_family=options.source_family)
            else:
                self.output("Choose a number from 1 to 5, or B to return.")

    def _html_analytics_selection_lines(
        self,
        report: HtmlAnalyticsReport,
        options: HtmlAnalyticsReportOptions,
    ) -> tuple[str, ...]:
        lines: list[str] = [
            f"Report title: {report.title}",
            f"Source: {self._html_analytics_source_label(report.source_family)}",
            f"Generated at: {report.generated_at}",
        ]
        for dashboard in report.dashboards:
            metadata = dashboard.metadata
            lines.extend((
                "",
                f"{dashboard.title} ({dashboard.source_record_family})",
                f"  Contributing records: {metadata.contributing_record_count}",
                f"  Scored records: {metadata.scored_record_count}",
                f"  Models: {', '.join(metadata.represented_models) or 'None represented'}",
                f"  Benchmarks: {', '.join(metadata.represented_benchmarks) or 'None represented'}",
                f"  Sessions: {', '.join(metadata.represented_sessions) or 'None represented'}",
                f"  Hardware: {', '.join(metadata.represented_hardware_environments) or 'None represented'}",
                f"  Import batches: {', '.join(metadata.represented_import_batches) or 'None represented'}",
                f"  Date range: {metadata.date_range[0] or 'Unavailable'} to {metadata.date_range[1] or 'Unavailable'}",
                f"  Included charts: {', '.join(chart.title for chart in dashboard.charts) or 'None'}",
                f"  Omitted charts: {', '.join(f'{item.title} ({item.reason})' for item in dashboard.omitted_charts) or 'None'}",
                f"  Active filters: {'; '.join(f'{key}={value}' for key, value in metadata.active_filters.items()) or 'None'}",
                f"  Coverage warnings: {'; '.join(metadata.coverage_warnings) or 'None'}",
            ))
        if not report.dashboards:
            lines.append("No dashboards were selected.")
        lines.extend((
            "",
            f"Detailed tables: {'Included' if options.include_detailed_tables else 'Excluded'}",
            "Missing values remain unavailable; the report never treats them as zero.",
        ))
        return tuple(lines)

    def _html_analytics_write_result_screen(self, result: ReportWriteResult) -> None:
        if result.status is ReportWriteStatus.SUCCESS:
            title = "HTML Analytics Write Complete"
            explanation = "The standalone HTML analytics report was staged and finalized successfully."
        elif result.status is ReportWriteStatus.OVERWRITE_REQUIRED:
            title = "HTML Analytics Write Requires Confirmation"
            explanation = "The destination already exists; no replacement was written."
        elif result.status is ReportWriteStatus.TEMP_WRITE_FAILED:
            title = "HTML Analytics Temporary Write Failed"
            explanation = "The report could not be staged; the final destination was not replaced."
        elif result.status is ReportWriteStatus.FINALIZE_FAILED:
            title = "HTML Analytics Finalization Failed"
            explanation = "The staged report could not be finalized; inspect the destination before retrying."
        else:
            title = "HTML Analytics Write Failed"
            explanation = "The HTML analytics writer returned an unrecognized failure status."
        lines = [explanation, "", f"Status: {result.status.value}", f"Path: {result.path}"]
        if result.message:
            lines.append(f"Message: {result.message}")
        if result.details:
            lines.append(f"Details: {result.details}")
        lines.extend(("", "B) Back", "QA) Quit BenchPup completely"))
        self.render_screen(title, "\n".join(lines))
        self.ask("Choose an option", navigation=True)

    def _write_html_analytics_workflow(
        self,
        report: HtmlAnalyticsReport,
        options: HtmlAnalyticsReportOptions,
    ) -> str | None:
        selection_lines = self._html_analytics_selection_lines(report, options)
        self.render_screen("HTML Analytics Selection", "\n".join(selection_lines))
        path = self.prompt_path(
            "HTML Analytics destination",
            default=str(options.output_path) if options.output_path else None,
            preserve_trailing_separator=True,
        )
        if not isinstance(path, str):
            return None
        output_path = self.prepare_export_destination(path, "HTML Analytics Report")
        if not isinstance(output_path, Path):
            return None
        self.render_screen(
            "Confirm HTML Analytics Report",
            "\n".join((*selection_lines, "", f"Destination: {output_path}", "The report will be written as one staged UTF-8 HTML file.", "", "Write this report?", "B) Back", "QA) Quit BenchPup completely")),
        )
        confirm = self.yes_no("Confirm HTML Analytics Report", navigation=True)
        if confirm is not True:
            return None
        try:
            result = self.reporting.write_html_analytics_report(report, output_path)
        except (OSError, ValueError) as error:
            self._report_error_screen("HTML Analytics Failed", f"Report generation or writing failed: {error}")
            return str(output_path)
        if result.status is ReportWriteStatus.OVERWRITE_REQUIRED:
            self.render_screen(
                "Confirm HTML Analytics Overwrite",
                f"An existing report is at:\n{output_path}\n\nReplace it only after staged validation?\n\nB) Back\nQA) Quit BenchPup completely",
            )
            overwrite = self.yes_no("Replace existing HTML analytics report", navigation=True)
            if overwrite is True:
                try:
                    result = self.reporting.write_html_analytics_report(report, output_path, overwrite=True)
                except (OSError, ValueError) as error:
                    self._report_error_screen("HTML Analytics Overwrite Failed", f"Report overwrite failed: {error}")
                    return str(output_path)
            elif isinstance(overwrite, NavigationSignal):
                return None
        self._html_analytics_write_result_screen(result)
        return str(output_path)

    def _html_analytics_screen(self, options: HtmlAnalyticsReportOptions) -> HtmlAnalyticsReportOptions:
        while True:
            content = "\n".join((
                "1) Configure HTML Analytics",
                "2) Preview Typed Analytics",
                "3) Write Standalone HTML Analytics",
                "",
                f"Source: {self._html_analytics_source_label(options.source_family)}",
                f"Trend interval: {self._trend_interval_label(options.interval)}",
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("HTML Analytics Report", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                options = self._html_analytics_options_screen(options)
                continue
            if command not in {"2", "3"}:
                self.output("Choose 1, 2, or 3, or B to return.")
                continue
            try:
                report = self.reporting.html_analytics_report(options=options)
            except (OSError, ValueError) as error:
                self._report_error_screen("HTML Analytics Failed", f"Could not build the analytics report: {error}")
                continue
            selection_lines = self._html_analytics_selection_lines(report, options)
            if command == "2":
                self._report_selection_preview("HTML Analytics Preview", selection_lines)
                continue
            if not report.dashboards:
                self._report_empty_screen("No Analytics Dashboards", selection_lines + ("No dashboard source was selected.",))
                continue
            destination = self._write_html_analytics_workflow(report, options)
            if destination is not None:
                options = replace(options, output_destination=destination, destination=destination)

    @staticmethod
    def _report_value(value: object) -> str:
        if value is None or value == "" or value == frozenset():
            return "Not set"
        if isinstance(value, bool):
            return "Enabled" if value else "Disabled"
        return str(value)

    @staticmethod
    def _report_number(value: float | int | None) -> str:
        if value is None:
            return "Not available"
        return f"{value:g}" if isinstance(value, float) else str(value)

    @staticmethod
    def _report_benchmark_type_label(value: str) -> str:
        return REPORT_BENCHMARK_TYPE_LABELS.get(value, value)

    def _report_vertical_choice(
        self,
        title: str,
        choices: list[tuple[str, Any]],
        current: Any,
    ) -> Any | None | NavigationSignal:
        current_label = next((label for label, value in choices if value == current), self._report_value(current))
        lines = [f"Current: {current_label}", ""]
        if choices:
            lines.extend(f"{number}) {label}" for number, (label, _) in enumerate(choices, start=1))
        else:
            lines.append("No catalog records are available.")
        clear_number = len(choices) + 1
        lines.extend((f"{clear_number}) Clear filter", "", "B) Back", "QA) Quit BenchPup completely"))
        self.render_screen(title, "\n".join(lines))
        while True:
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return choice
            command = self.normalized(choice)
            if command.isdigit() and 1 <= int(command) <= len(choices):
                return choices[int(command) - 1][1]
            if command == str(clear_number):
                return None
            self.output(f"Choose a number from 1 to {clear_number}, or B to return.")

    def _report_catalog_choice(
        self,
        title: str,
        items: list[Any],
        label: Callable[[Any], str],
        value: Callable[[Any], Any],
        current: Any,
    ) -> Any | None | NavigationSignal:
        choices = [(label(item), value(item)) for item in items if value(item) is not None]
        return self._report_vertical_choice(title, choices, current)

    def _report_filter_summary(self, filters: BenchmarkReportFilters) -> tuple[str, ...]:
        values: list[str] = []
        if filters.benchmark_type:
            values.append(f"Benchmark type: {self._report_benchmark_type_label(filters.benchmark_type)}")
        if filters.benchmark:
            values.append(f"Benchmark: {filters.benchmark}")
        if filters.session_id is not None:
            session = self.catalog.sessions.get(filters.session_id)
            values.append(f"Session: {session.title if session else 'Unavailable selection'}")
        elif filters.session:
            values.append(f"Session text: {filters.session}")
        if filters.hardware_profile_id is not None:
            hardware = self.catalog.hardware_profiles.get(filters.hardware_profile_id)
            values.append(f"Hardware profile: {hardware.name if hardware else 'Unavailable selection'}")
        elif filters.hardware:
            values.append(f"Hardware snapshot: {filters.hardware}")
        if filters.model:
            values.append(f"Model: {filters.model}")
        return tuple(values or ("All non-deleted benchmark runs",))

    def _report_scoreboard_filter_summary(self, filters: ScoreboardReportFilters) -> tuple[str, ...]:
        values: list[str] = []
        if filters.batch_id is not None:
            batch = self.catalog.scoreboard_import_batches.get(filters.batch_id)
            values.append(f"Import batch: {batch.name if batch else 'Unavailable selection'}")
        if filters.model:
            values.append(f"Model text: {filters.model}")
        return tuple(values or ("All non-deleted scoreboard entries",))

    def _report_filters_screen(self, filters: BenchmarkReportFilters) -> BenchmarkReportFilters:
        benchmark_type_choices = [
            (self._report_benchmark_type_label(value), value)
            for value in BENCHMARK_TYPES
        ]
        while True:
            active = "; ".join(self._report_filter_summary(filters))
            session = self.catalog.sessions.get(filters.session_id) if filters.session_id is not None else None
            hardware = self.catalog.hardware_profiles.get(filters.hardware_profile_id) if filters.hardware_profile_id is not None else None
            content = "\n".join((
                f"1) Benchmark Type [{self._report_benchmark_type_label(filters.benchmark_type) if filters.benchmark_type else 'Not set'}]",
                f"2) Benchmark [{self._report_value(filters.benchmark)}]",
                f"3) Benchmark Snapshot Text [{self._report_value(filters.benchmark if filters.benchmark else '')}]",
                f"4) Session [{session.title if session else ('Unavailable selection' if filters.session_id is not None else 'Not set')}]",
                f"5) Hardware Profile [{hardware.name if hardware else ('Unavailable selection' if filters.hardware_profile_id is not None else 'Not set')}]",
                f"6) Hardware Snapshot Text [{self._report_value(filters.hardware)}]",
                f"7) Model Profile [{self._report_value(filters.model)}]",
                f"8) Model Snapshot Text [{self._report_value(filters.model)}]",
                "9) Reset Selection Filters",
                "",
                f"Active: {active}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Report Selection Filters", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return filters
            command = self.normalized(choice)
            if command == "1":
                selected = self._report_vertical_choice("Benchmark Type", benchmark_type_choices, filters.benchmark_type or None)
                if not isinstance(selected, NavigationSignal):
                    filters = replace(filters, benchmark_type=selected or "")
            elif command == "2":
                selected = self._report_catalog_choice(
                    "Select Benchmark",
                    self.catalog.benchmark_definitions.list(),
                    lambda item: f"{item.name} ({item.file_path})",
                    lambda item: item.name,
                    filters.benchmark or None,
                )
                if not isinstance(selected, NavigationSignal):
                    filters = replace(filters, benchmark=selected or "")
            elif command == "3":
                value = self.ask("Benchmark snapshot text (blank clears)", navigation=True)
                if isinstance(value, str):
                    filters = replace(filters, benchmark=value)
            elif command == "4":
                selected = self._report_catalog_choice(
                    "Select Session",
                    self.catalog.sessions.list(),
                    lambda item: item.title,
                    lambda item: item.id,
                    filters.session_id,
                )
                if not isinstance(selected, NavigationSignal):
                    filters = replace(filters, session_id=selected)
            elif command == "5":
                selected = self._report_catalog_choice(
                    "Select Hardware Profile",
                    self.catalog.hardware_profiles.list(),
                    lambda item: item.name,
                    lambda item: item.id,
                    filters.hardware_profile_id,
                )
                if not isinstance(selected, NavigationSignal):
                    filters = replace(filters, hardware_profile_id=selected, hardware="")
            elif command == "6":
                value = self.ask("Hardware snapshot text (blank clears)", navigation=True)
                if isinstance(value, str):
                    filters = replace(filters, hardware_profile_id=None, hardware=value)
            elif command == "7":
                selected = self._report_catalog_choice(
                    "Select Model",
                    self.catalog.model_profiles.list(),
                    lambda item: f"{item.name} ({item.model_name})",
                    lambda item: item.model_name,
                    filters.model or None,
                )
                if not isinstance(selected, NavigationSignal):
                    filters = replace(filters, model=selected or "")
            elif command == "8":
                value = self.ask("Model snapshot text (blank clears)", navigation=True)
                if isinstance(value, str):
                    filters = replace(filters, model=value)
            elif command == "9":
                filters = BenchmarkReportFilters()
            else:
                self.output("Choose a number from 1 to 9, or B to return.")

    def _comparison_filter_summary(self, filters: BenchmarkStatisticsFilters) -> tuple[str, ...]:
        values: list[str] = []
        if filters.benchmark_type:
            values.append(f"Benchmark type: {self._report_benchmark_type_label(filters.benchmark_type)}")
        if filters.benchmark:
            values.append(f"Benchmark: {filters.benchmark}")
        if filters.session_id is not None:
            session = self.catalog.sessions.get(filters.session_id)
            values.append(f"Session: {session.title if session else 'Unavailable selection'}")
        elif filters.session:
            values.append(f"Session text: {filters.session}")
        if filters.hardware_profile_id is not None:
            hardware = self.catalog.hardware_profiles.get(filters.hardware_profile_id)
            values.append(f"Hardware profile: {hardware.name if hardware else 'Unavailable selection'}")
        elif filters.hardware:
            values.append(f"Hardware snapshot: {filters.hardware}")
        if filters.date_from:
            values.append(f"Created from: {filters.date_from}")
        if filters.date_to:
            values.append(f"Created to: {filters.date_to}")
        if filters.minimum_score is not None:
            values.append(f"Minimum overall score: {filters.minimum_score:g}")
        if filters.maximum_score is not None:
            values.append(f"Maximum overall score: {filters.maximum_score:g}")
        if filters.hallucination:
            values.append(f"Hallucination: {filters.hallucination}")
        if filters.reliability:
            values.append(f"Reliability: {filters.reliability}")
        if filters.include_deleted:
            values.append("Deleted runs: included")
        return tuple(values or ("All non-deleted BenchmarkRun snapshots",))

    def _comparison_filters_screen(
        self,
        filters: BenchmarkStatisticsFilters,
        *,
        comparison_type: str,
        clear_model: bool = True,
    ) -> BenchmarkStatisticsFilters:
        """Configure shared BenchmarkRun filters for comparisons or trends."""

        while True:
            content = "\n".join((
                "1) Benchmark, type, session, and hardware selectors",
                f"2) Created date range [{self._report_value(filters.date_from)} → {self._report_value(filters.date_to)}]",
                f"3) Overall score range [{self._report_value(filters.minimum_score)} → {self._report_value(filters.maximum_score)}]",
                f"4) Hallucination level [{self._report_value(filters.hallucination)}]",
                f"5) Reliability level [{self._report_value(filters.reliability)}]",
                "6) Reset Comparison Filters",
                "",
                f"Comparison: {comparison_type}",
                *self._comparison_filter_summary(filters),
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Comparison Filters" if clear_model else "Trend Filters", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return filters
            command = self.normalized(choice)
            if command == "1":
                base = BenchmarkReportFilters(
                    benchmark=filters.benchmark,
                    benchmark_type=filters.benchmark_type,
                    session=filters.session,
                    session_id=filters.session_id,
                    hardware=filters.hardware,
                    hardware_profile_id=filters.hardware_profile_id,
                    include_run_ids=filters.include_run_ids,
                    exclude_run_ids=filters.exclude_run_ids,
                    include_deleted=filters.include_deleted,
                )
                configured = self._report_filters_screen(base)
                filters = replace(
                    filters,
                    benchmark=configured.benchmark,
                    benchmark_type=configured.benchmark_type,
                    session=configured.session,
                    session_id=configured.session_id,
                    hardware=configured.hardware,
                    hardware_profile_id=configured.hardware_profile_id,
                    include_run_ids=configured.include_run_ids,
                    exclude_run_ids=configured.exclude_run_ids,
                    # Entity selection is handled by ComparisonService; trends
                    # retain the optional model filter.
                    model="" if clear_model else configured.model,
                )
            elif command == "2":
                start = self.ask("Created start (ISO date/time; blank clears)", navigation=True)
                if isinstance(start, NavigationSignal):
                    continue
                end = self.ask("Created end (ISO date/time; blank clears)", navigation=True)
                if isinstance(end, NavigationSignal):
                    continue
                filters = replace(filters, date_from=start or None, date_to=end or None)
            elif command == "3":
                minimum = self.ask_float("Minimum overall score (blank clears)", navigation=True)
                if isinstance(minimum, NavigationSignal):
                    continue
                maximum = self.ask_float("Maximum overall score (blank clears)", navigation=True)
                if isinstance(maximum, NavigationSignal):
                    continue
                filters = replace(filters, minimum_score=minimum, maximum_score=maximum)
            elif command == "4":
                selected = self._report_vertical_choice(
                    "Hallucination Level",
                    [(level, level) for level in LEVELS],
                    filters.hallucination or None,
                )
                if not isinstance(selected, NavigationSignal):
                    filters = replace(filters, hallucination=selected or "")
            elif command == "5":
                selected = self._report_vertical_choice(
                    "Reliability Level",
                    [(level, level) for level in LEVELS],
                    filters.reliability or None,
                )
                if not isinstance(selected, NavigationSignal):
                    filters = replace(filters, reliability=selected or "")
            elif command == "6":
                filters = BenchmarkStatisticsFilters()
            else:
                self.output("Choose a number from 1 to 6, or B to return.")

    @staticmethod
    def _trend_interval_label(value: TimeBucketGranularity | str) -> str:
        active = value if isinstance(value, TimeBucketGranularity) else TimeBucketGranularity(str(value).strip().casefold())
        return {
            TimeBucketGranularity.DAY: "Day (UTC)",
            TimeBucketGranularity.WEEK: "Week (UTC Monday start)",
            TimeBucketGranularity.MONTH: "Month (UTC)",
        }[active]

    @staticmethod
    def _trend_grouping_label(value: TrendGrouping | str) -> str:
        active = value if isinstance(value, TrendGrouping) else TrendGrouping(str(value).strip().casefold().replace("-", "_").replace(" ", "_"))
        return {
            TrendGrouping.OVERALL: "Overall",
            TrendGrouping.MODEL: "Model",
            TrendGrouping.BENCHMARK: "Benchmark",
            TrendGrouping.BENCHMARK_TYPE: "Benchmark type",
            TrendGrouping.SESSION: "Session",
            TrendGrouping.HARDWARE: "Hardware environment",
            TrendGrouping.IMPORT_BATCH: "Import batch",
        }[active]

    def _trend_benchmark_filters_screen(
        self,
        filters: BenchmarkStatisticsFilters,
    ) -> BenchmarkStatisticsFilters:
        return self._comparison_filters_screen(
            filters,
            comparison_type="BenchmarkRun trends",
            clear_model=False,
        )

    def _trend_scoreboard_filter_summary(self, filters: ScoreboardStatisticsFilters) -> tuple[str, ...]:
        values: list[str] = []
        if filters.model:
            values.append(f"Model text: {filters.model}")
        if filters.batch_id is not None:
            batch = self.catalog.scoreboard_import_batches.get(filters.batch_id)
            values.append(f"Import batch: {batch.name if batch else 'Unavailable selection'}")
        if filters.date_from:
            values.append(f"Imported from: {filters.date_from}")
        if filters.date_to:
            values.append(f"Imported to: {filters.date_to}")
        if filters.minimum_score is not None:
            values.append(f"Minimum score: {filters.minimum_score:g}")
        if filters.maximum_score is not None:
            values.append(f"Maximum score: {filters.maximum_score:g}")
        if filters.hallucination:
            values.append(f"Hallucination: {filters.hallucination}")
        if filters.consistency:
            values.append(f"Consistency: {filters.consistency}")
        if filters.reliability:
            values.append(f"Reliability: {filters.reliability}")
        if filters.include_deleted:
            values.append("Deleted entries/batches: included")
        return tuple(values or ("All non-deleted scoreboard entries",))

    def _trend_scoreboard_filters_screen(
        self,
        filters: ScoreboardStatisticsFilters,
    ) -> ScoreboardStatisticsFilters:
        while True:
            content = "\n".join((
                f"1) Model text [{self._report_value(filters.model)}]",
                f"2) Import batch [{self._report_value(filters.batch_id)}]",
                f"3) Imported date range [{self._report_value(filters.date_from)} to {self._report_value(filters.date_to)}]",
                f"4) Score range [{self._report_value(filters.minimum_score)} to {self._report_value(filters.maximum_score)}]",
                f"5) Hallucination level [{self._report_value(filters.hallucination)}]",
                f"6) Consistency [{self._report_value(filters.consistency)}]",
                f"7) Reliability level [{self._report_value(filters.reliability)}]",
                "8) Reset Trend Filters",
                "",
                *self._trend_scoreboard_filter_summary(filters),
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Trend Filters", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return filters
            command = self.normalized(choice)
            if command == "1":
                value = self.ask("Model text (blank clears)", navigation=True)
                if isinstance(value, str):
                    filters = replace(filters, model=value)
            elif command == "2":
                selected = self._report_catalog_choice(
                    "Select Import Batch",
                    self.catalog.scoreboard_import_batches.list(),
                    lambda item: f"{item.name} ({item.source_file})",
                    lambda item: item.id,
                    filters.batch_id,
                )
                if not isinstance(selected, NavigationSignal):
                    filters = replace(filters, batch_id=selected)
            elif command == "3":
                start = self.ask("Imported start (ISO date/time; blank clears)", navigation=True)
                if isinstance(start, NavigationSignal):
                    continue
                end = self.ask("Imported end (ISO date/time; blank clears)", navigation=True)
                if isinstance(end, NavigationSignal):
                    continue
                filters = replace(filters, date_from=start or None, date_to=end or None)
            elif command == "4":
                minimum = self.ask_float("Minimum score (blank clears)", navigation=True)
                if isinstance(minimum, NavigationSignal):
                    continue
                maximum = self.ask_float("Maximum score (blank clears)", navigation=True)
                if isinstance(maximum, NavigationSignal):
                    continue
                filters = replace(filters, minimum_score=minimum, maximum_score=maximum)
            elif command in {"5", "6", "7"}:
                field_name = {"5": "hallucination", "6": "consistency", "7": "reliability"}[command]
                selected = self._report_vertical_choice(
                    field_name.replace("_", " ").title(),
                    [(level, level) for level in LEVELS],
                    getattr(filters, field_name) or None,
                )
                if not isinstance(selected, NavigationSignal):
                    filters = replace(filters, **{field_name: selected or ""})
            elif command == "8":
                filters = ScoreboardStatisticsFilters()
            else:
                self.output("Choose a number from 1 to 8, or B to return.")

    def _benchmark_trend_options_screen(self, options: BenchmarkTrendOptions) -> BenchmarkTrendOptions:
        interval_choices = [
            (self._trend_interval_label(value), value)
            for value in TimeBucketGranularity
        ]
        grouping_choices = [
            (self._trend_grouping_label(value), value)
            for value in (
                TrendGrouping.OVERALL,
                TrendGrouping.MODEL,
                TrendGrouping.BENCHMARK,
                TrendGrouping.BENCHMARK_TYPE,
                TrendGrouping.SESSION,
                TrendGrouping.HARDWARE,
            )
        ]
        while True:
            content = "\n".join((
                f"1) Trend title [{options.title}]",
                f"2) Interval [{self._trend_interval_label(options.interval)}]",
                f"3) Grouping [{self._trend_grouping_label(options.grouping)}]",
                "4) Configure filters",
                f"5) Include empty buckets [{'Yes' if options.include_empty_buckets else 'No'}]",
                f"6) Detailed series sections [{'Yes' if options.include_series_details else 'No'}]",
                "7) Reset Trend Options",
                "",
                *self._comparison_filter_summary(options.filters),
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Benchmark Run Trend Options", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                value = self.ask("Trend title", navigation=True, default=options.title)
                if isinstance(value, str) and value:
                    options = replace(options, title=value)
            elif command == "2":
                selected = self._report_vertical_choice("Trend Interval", interval_choices, options.interval)
                if not isinstance(selected, NavigationSignal) and selected is not None:
                    options = replace(options, interval=selected)
            elif command == "3":
                selected = self._report_vertical_choice("BenchmarkRun Trend Grouping", grouping_choices, options.grouping)
                if not isinstance(selected, NavigationSignal) and selected is not None:
                    options = replace(options, grouping=selected)
            elif command == "4":
                options = replace(options, filters=self._trend_benchmark_filters_screen(options.filters))
            elif command in {"5", "6"}:
                field_name = "include_empty_buckets" if command == "5" else "include_series_details"
                label = "Include Empty Buckets" if command == "5" else "Detailed Series Sections"
                value = self._configure_redaction_toggle(label, bool(getattr(options, field_name)))
                if not isinstance(value, NavigationSignal):
                    options = replace(options, **{field_name: value})
            elif command == "7":
                options = BenchmarkTrendOptions()
            else:
                self.output("Choose a number from 1 to 7, or B to return.")

    def _scoreboard_trend_options_screen(self, options: ScoreboardTrendOptions) -> ScoreboardTrendOptions:
        interval_choices = [
            (self._trend_interval_label(value), value)
            for value in TimeBucketGranularity
        ]
        grouping_choices = [
            (self._trend_grouping_label(value), value)
            for value in (TrendGrouping.OVERALL, TrendGrouping.MODEL, TrendGrouping.IMPORT_BATCH)
        ]
        while True:
            content = "\n".join((
                f"1) Trend title [{options.title}]",
                f"2) Interval [{self._trend_interval_label(options.interval)}]",
                f"3) Grouping [{self._trend_grouping_label(options.grouping)}]",
                "4) Configure filters",
                f"5) Include empty buckets [{'Yes' if options.include_empty_buckets else 'No'}]",
                f"6) Detailed series sections [{'Yes' if options.include_series_details else 'No'}]",
                "7) Reset Trend Options",
                "",
                *self._trend_scoreboard_filter_summary(options.filters),
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Historical Scoreboard Trend Options", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                value = self.ask("Trend title", navigation=True, default=options.title)
                if isinstance(value, str) and value:
                    options = replace(options, title=value)
            elif command == "2":
                selected = self._report_vertical_choice("Trend Interval", interval_choices, options.interval)
                if not isinstance(selected, NavigationSignal) and selected is not None:
                    options = replace(options, interval=selected)
            elif command == "3":
                selected = self._report_vertical_choice("Scoreboard Trend Grouping", grouping_choices, options.grouping)
                if not isinstance(selected, NavigationSignal) and selected is not None:
                    options = replace(options, grouping=selected)
            elif command == "4":
                options = replace(options, filters=self._trend_scoreboard_filters_screen(options.filters))
            elif command in {"5", "6"}:
                field_name = "include_empty_buckets" if command == "5" else "include_series_details"
                label = "Include Empty Buckets" if command == "5" else "Detailed Series Sections"
                value = self._configure_redaction_toggle(label, bool(getattr(options, field_name)))
                if not isinstance(value, NavigationSignal):
                    options = replace(options, **{field_name: value})
            elif command == "7":
                options = ScoreboardTrendOptions()
            else:
                self.output("Choose a number from 1 to 7, or B to return.")

    def _comparison_multi_select(
        self,
        title: str,
        choices: Sequence[tuple[Any, str]],
        selected: Sequence[Any],
    ) -> tuple[Any, ...] | NavigationSignal:
        selected_values = list(dict.fromkeys(selected))
        choice_values = [value for value, _ in choices]
        while True:
            lines = [
                "Toggle a number, then choose D when at least two entries are selected.",
                "",
            ]
            if choices:
                lines.extend(
                    f"{number}) [{'x' if value in selected_values else ' '}] {label}"
                    for number, (value, label) in enumerate(choices, start=1)
                )
            else:
                lines.append("No eligible entries are available.")
            lines.extend(("", f"Selected: {len(selected_values)}", "D) Done", "B) Back", "QA) Quit BenchPup completely"))
            self.render_screen(title, "\n".join(lines))
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return choice
            command = self.normalized(choice)
            if command in {"d", "done"}:
                return tuple(value for value in choice_values if value in selected_values)
            if command.isdigit() and 1 <= int(command) <= len(choices):
                value = choice_values[int(command) - 1]
                if value in selected_values:
                    selected_values.remove(value)
                else:
                    selected_values.append(value)
                continue
            self.output(f"Choose a number from 1 to {len(choices)}, D when finished, or B to return.")

    def _available_comparison_models(self) -> tuple[tuple[str, str], ...]:
        labels: dict[str, str] = {}
        for aggregate in self.comparisons.statistics.select_benchmark_runs():
            label = model_snapshot_name(aggregate.run.model_snapshot)
            key = normalize_model_identity(label)
            if key == "unknown":
                continue
            current = labels.get(key)
            labels[key] = label if current is None else min((current, label), key=lambda value: (value.casefold(), value))
        return tuple(
            (labels[key], labels[key])
            for key in sorted(labels, key=lambda item: (labels[item].casefold(), labels[item], item))
        )

    def _available_comparison_sessions(self) -> tuple[tuple[int, str], ...]:
        return tuple(
            (session.id, session.title)
            for session in sorted(
                self.catalog.sessions.list(),
                key=lambda item: ((item.title or "").casefold(), item.title or "", item.id or 0),
            )
            if session.id is not None
        )

    def _report_template_label(self, template_id: str) -> str:
        template = self.reporting.report_template(template_id)
        if template is None:
            return self._report_value(template_id)
        return f"{template.name} — {template.description}"

    def _report_template_choice(self, current: str) -> str | NavigationSignal:
        choices = [
            (f"{template.name} — {template.description}", template.template_id)
            for template in self.reporting.report_templates()
        ]
        selected = self._report_vertical_choice("Report Template", choices, current)
        return selected if isinstance(selected, NavigationSignal) else str(selected or current)

    def _apply_report_template(self, template_id: str) -> ReportTemplateOptions:
        return self.reporting.apply_template(template_id)

    def _report_template_options_for(
        self,
        template_id: str,
        **overrides: bool,
    ) -> ReportTemplateOptions:
        applied = self.reporting.apply_template(template_id)
        if not isinstance(applied, ReportTemplateOptions):
            applied = ReportTemplateOptions(template_id=template_id)
        for name, value in overrides.items():
            if hasattr(applied, name):
                setattr(applied, name, value)
        return applied

    def _report_template_argument(
        self,
        template_id: str,
        **overrides: bool,
    ) -> dict[str, Any]:
        if template_id == "standard":
            return {}
        return {"template_options": self._report_template_options_for(template_id, **overrides)}

    def _benchmark_report_options_screen(self, options: BenchmarkReportOptions) -> BenchmarkReportOptions:
        while True:
            content = "\n".join((
                f"1) Report Title [{options.title}]",
                "2) Configure Selection Filters",
                f"3) Include Prompt Text [{'Yes' if options.include_prompt_text else 'No'}]",
                f"4) Include Raw Model Output [{'Yes' if options.include_raw_model_output else 'No'}]",
                f"5) Include Attachment Metadata [{'Yes' if options.include_attachment_metadata else 'No'}]",
                f"6) Report Template [{self._report_template_label(options.template_id)}]",
                "7) Reset Report Options",
                "",
                *self._report_filter_summary(options.filters),
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Detailed Benchmark Run Report Options", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                value = self.ask("Report title", navigation=True, default=options.title)
                if isinstance(value, str) and value.strip():
                    options = replace(options, title=value.strip())
            elif command == "2":
                options = replace(options, filters=self._report_filters_screen(options.filters))
            elif command in {"3", "4", "5"}:
                field_name = {
                    "3": "include_prompt_text",
                    "4": "include_raw_model_output",
                    "5": "include_attachment_metadata",
                }[command]
                value = self._configure_redaction_toggle(
                    {
                        "include_prompt_text": "Include Prompt Text",
                        "include_raw_model_output": "Include Raw Model Output",
                        "include_attachment_metadata": "Include Attachment Metadata",
                    }[field_name],
                    bool(getattr(options, field_name)),
                )
                if not isinstance(value, NavigationSignal):
                    options = replace(options, **{field_name: value})
            elif command == "6":
                selected = self._report_template_choice(options.template_id)
                if not isinstance(selected, NavigationSignal):
                    applied = self._apply_report_template(selected)
                    options = replace(
                        options,
                        template_id=applied.template_id,
                        include_prompt_text=applied.include_prompt_text,
                        include_raw_model_output=applied.include_raw_model_output,
                        include_attachment_metadata=applied.include_attachment_metadata,
                    )
            elif command == "7":
                options = BenchmarkReportOptions()
            else:
                self.output("Choose a number from 1 to 7, or B to return.")

    def _scoreboard_report_options_screen(self, options: ScoreboardReportOptions) -> ScoreboardReportOptions:
        while True:
            content = "\n".join((
                f"1) Report Title [{options.title}]",
                "2) Scoreboard Import Batch",
                f"3) Model Text Filter [{self._report_value(options.filters.model)}]",
                f"4) Report Template [{self._report_template_label(options.template_id)}]",
                "5) Reset Report Options",
                "",
                *self._report_scoreboard_filter_summary(options.filters),
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Historical Scoreboard Report Options", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                value = self.ask("Report title", navigation=True, default=options.title)
                if isinstance(value, str) and value.strip():
                    options = replace(options, title=value.strip())
            elif command == "2":
                selected = self._report_catalog_choice(
                    "Select Scoreboard Import Batch",
                    self.catalog.scoreboard_import_batches.list(),
                    lambda item: f"{item.name} ({item.source_file})",
                    lambda item: item.id,
                    options.filters.batch_id,
                )
                if not isinstance(selected, NavigationSignal):
                    options = replace(options, filters=replace(options.filters, batch_id=selected))
            elif command == "3":
                value = self.ask("Model text filter (blank clears)", navigation=True)
                if isinstance(value, str):
                    options = replace(options, filters=replace(options.filters, model=value))
            elif command == "4":
                selected = self._report_template_choice(options.template_id)
                if not isinstance(selected, NavigationSignal):
                    applied = self._apply_report_template(selected)
                    options = replace(options, template_id=applied.template_id)
            elif command == "5":
                options = ScoreboardReportOptions()
            else:
                self.output("Choose a number from 1 to 5, or B to return.")

    def _leaderboard_report_options_screen(self, options: LeaderboardReportOptions) -> LeaderboardReportOptions:
        while True:
            content = "\n".join((
                f"1) Report Title [{options.title}]",
                "2) Configure Selection Filters",
                f"3) Include Per-Model Detail Sections [{'Yes' if options.include_model_details else 'No'}]",
                f"4) Report Template [{self._report_template_label(options.template_id)}]",
                "5) Reset Report Options",
                "",
                *self._report_filter_summary(options.filters),
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Model Leaderboard Options", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                value = self.ask("Report title", navigation=True, default=options.title)
                if isinstance(value, str) and value.strip():
                    options = replace(options, title=value.strip())
            elif command == "2":
                options = replace(options, filters=self._report_filters_screen(options.filters))
            elif command == "3":
                value = self._configure_redaction_toggle("Include Per-Model Detail Sections", options.include_model_details)
                if not isinstance(value, NavigationSignal):
                    options = replace(options, include_model_details=value)
            elif command == "4":
                selected = self._report_template_choice(options.template_id)
                if not isinstance(selected, NavigationSignal):
                    applied = self._apply_report_template(selected)
                    options = replace(
                        options,
                        template_id=applied.template_id,
                        include_model_details=applied.include_model_details,
                    )
            elif command == "5":
                options = LeaderboardReportOptions()
            else:
                self.output("Choose a number from 1 to 5, or B to return.")

    def _session_report_options_screen(self, options: SessionReportOptions) -> SessionReportOptions:
        while True:
            session = self.catalog.sessions.get(options.filters.session_id) if options.filters.session_id is not None else None
            content = "\n".join((
                f"1) Select Session [{session.title if session else 'Not set'}]",
                "2) Configure Additional Filters",
                f"3) Report Title [{options.title}]",
                f"4) Include Prompt Text [{'Yes' if options.include_prompt_text else 'No'}]",
                f"5) Include Raw Model Output [{'Yes' if options.include_raw_model_output else 'No'}]",
                f"6) Include Attachment Metadata [{'Yes' if options.include_attachment_metadata else 'No'}]",
                f"7) Report Template [{self._report_template_label(options.template_id)}]",
                "8) Reset Report Options",
                "",
                *self._report_filter_summary(options.filters),
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Session Report Options", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                selected = self._report_catalog_choice(
                    "Select Session",
                    self.catalog.sessions.list(),
                    lambda item: f"{item.title} ({item.started_at or 'date unavailable'})",
                    lambda item: item.id,
                    options.filters.session_id,
                )
                if not isinstance(selected, NavigationSignal):
                    options = replace(options, filters=replace(options.filters, session_id=selected))
            elif command == "2":
                options = replace(options, filters=self._report_filters_screen(options.filters))
            elif command == "3":
                value = self.ask("Report title", navigation=True, default=options.title)
                if isinstance(value, str) and value.strip():
                    options = replace(options, title=value.strip())
            elif command in {"4", "5", "6"}:
                field_name = {
                    "4": "include_prompt_text",
                    "5": "include_raw_model_output",
                    "6": "include_attachment_metadata",
                }[command]
                value = self._configure_redaction_toggle(
                    {
                        "include_prompt_text": "Include Prompt Text",
                        "include_raw_model_output": "Include Raw Model Output",
                        "include_attachment_metadata": "Include Attachment Metadata",
                    }[field_name],
                    bool(getattr(options, field_name)),
                )
                if not isinstance(value, NavigationSignal):
                    options = replace(options, **{field_name: value})
            elif command == "7":
                selected = self._report_template_choice(options.template_id)
                if not isinstance(selected, NavigationSignal):
                    applied = self._apply_report_template(selected)
                    options = replace(
                        options,
                        template_id=applied.template_id,
                        include_prompt_text=applied.include_prompt_text,
                        include_raw_model_output=applied.include_raw_model_output,
                        include_attachment_metadata=applied.include_attachment_metadata,
                    )
            elif command == "8":
                options = SessionReportOptions()
            else:
                self.output("Choose a number from 1 to 8, or B to return.")

    def _hardware_report_options_screen(self, options: HardwareReportOptions) -> HardwareReportOptions:
        while True:
            content = "\n".join((
                f"1) Report Title [{options.title}]",
                "2) Configure Hardware and Selection Filters",
                f"3) Include Per-Hardware Detail Sections [{'Yes' if options.include_hardware_details else 'No'}]",
                f"4) Include Prompt Text [{'Yes' if options.include_prompt_text else 'No'}]",
                f"5) Include Raw Model Output [{'Yes' if options.include_raw_model_output else 'No'}]",
                f"6) Include Attachment Metadata [{'Yes' if options.include_attachment_metadata else 'No'}]",
                f"7) Report Template [{self._report_template_label(options.template_id)}]",
                "8) Reset Report Options",
                "",
                *self._report_filter_summary(options.filters),
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Hardware Report Options", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                value = self.ask("Report title", navigation=True, default=options.title)
                if isinstance(value, str) and value.strip():
                    options = replace(options, title=value.strip())
            elif command == "2":
                options = replace(options, filters=self._report_filters_screen(options.filters))
            elif command == "3":
                value = self._configure_redaction_toggle("Include Per-Hardware Detail Sections", options.include_hardware_details)
                if not isinstance(value, NavigationSignal):
                    options = replace(options, include_hardware_details=value)
            elif command in {"4", "5", "6"}:
                field_name = {
                    "4": "include_prompt_text",
                    "5": "include_raw_model_output",
                    "6": "include_attachment_metadata",
                }[command]
                value = self._configure_redaction_toggle(
                    {
                        "include_prompt_text": "Include Prompt Text",
                        "include_raw_model_output": "Include Raw Model Output",
                        "include_attachment_metadata": "Include Attachment Metadata",
                    }[field_name],
                    bool(getattr(options, field_name)),
                )
                if not isinstance(value, NavigationSignal):
                    options = replace(options, **{field_name: value})
            elif command == "7":
                selected = self._report_template_choice(options.template_id)
                if not isinstance(selected, NavigationSignal):
                    applied = self._apply_report_template(selected)
                    options = replace(
                        options,
                        template_id=applied.template_id,
                        include_hardware_details=applied.include_hardware_details,
                        include_prompt_text=applied.include_prompt_text,
                        include_raw_model_output=applied.include_raw_model_output,
                        include_attachment_metadata=applied.include_attachment_metadata,
                    )
            elif command == "8":
                options = HardwareReportOptions()
            else:
                self.output("Choose a number from 1 to 8, or B to return.")

    def _report_configuration_lines(
        self,
        benchmark: BenchmarkReportOptions,
        scoreboard: ScoreboardReportOptions,
        leaderboard: LeaderboardReportOptions,
        session: SessionReportOptions,
        hardware: HardwareReportOptions,
    ) -> tuple[str, ...]:
        return (
            "Detailed Benchmark Run Report",
            f"  Title: {benchmark.title}",
            f"  Filters: {'; '.join(self._report_filter_summary(benchmark.filters))}",
            f"  Prompt text: {'Included' if benchmark.include_prompt_text else 'Excluded'}",
            f"  Raw model output: {'Included' if benchmark.include_raw_model_output else 'Excluded'}",
            f"  Attachment metadata: {'Included' if benchmark.include_attachment_metadata else 'Excluded'}",
            f"  Template: {self._report_template_label(benchmark.template_id)}",
            f"  Destination: {self._report_value(benchmark.destination)}",
            "",
            "Historical Scoreboard Report",
            f"  Title: {scoreboard.title}",
            f"  Filters: {'; '.join(self._report_scoreboard_filter_summary(scoreboard.filters))}",
            f"  Template: {self._report_template_label(scoreboard.template_id)}",
            f"  Destination: {self._report_value(scoreboard.destination)}",
            "",
            "Model Leaderboard",
            f"  Title: {leaderboard.title}",
            f"  Filters: {'; '.join(self._report_filter_summary(leaderboard.filters))}",
            f"  Per-model details: {'Included' if leaderboard.include_model_details else 'Excluded'}",
            f"  Template: {self._report_template_label(leaderboard.template_id)}",
            f"  Destination: {self._report_value(leaderboard.destination)}",
            "",
            "Session Report",
            f"  Title: {session.title}",
            f"  Session: {self._report_filter_summary(session.filters)[0] if session.filters.session_id is not None else 'Not selected'}",
            f"  Template: {self._report_template_label(session.template_id)}",
            f"  Prompt text: {'Included' if session.include_prompt_text else 'Excluded'}",
            f"  Raw model output: {'Included' if session.include_raw_model_output else 'Excluded'}",
            f"  Attachment metadata: {'Included' if session.include_attachment_metadata else 'Excluded'}",
            f"  Destination: {self._report_value(session.destination)}",
            "",
            "Hardware Report",
            f"  Title: {hardware.title}",
            f"  Filters: {'; '.join(self._report_filter_summary(hardware.filters))}",
            f"  Per-hardware details: {'Included' if hardware.include_hardware_details else 'Excluded'}",
            f"  Template: {self._report_template_label(hardware.template_id)}",
            f"  Destination: {self._report_value(hardware.destination)}",
            "",
            "Overwrite: explicit confirmation is required for an existing file.",
            "",
            "B) Back",
            "QA) Quit BenchPup completely",
        )

    @staticmethod
    def _report_models(values: list[str]) -> str:
        distinct = tuple(dict.fromkeys(value for value in values if value))
        return ", ".join(distinct) if distinct else "None represented"

    def _benchmark_selection_lines(self, report: BenchmarkRunReport, options: BenchmarkReportOptions) -> tuple[str, ...]:
        return (
            f"Selected run count: {report.metadata.record_count}",
            f"Scored run count: {report.summary.scored_count} of {report.summary.count}",
            f"Selected models: {self._report_models([item.model_name for item in report.records])}",
            f"Filters: {'; '.join(self._report_filter_summary(options.filters))}",
            f"Prompt text: {'Included' if options.include_prompt_text else 'Excluded'}",
            f"Raw model output: {'Included' if options.include_raw_model_output else 'Excluded'}",
            f"Attachment metadata: {'Included' if options.include_attachment_metadata else 'Excluded'}",
            f"Template: {self._report_template_label(options.template_id)}",
            "Soft-deleted runs: excluded by the reporting engine.",
        )

    def _scoreboard_selection_lines(self, report: ScoreboardReport, options: ScoreboardReportOptions) -> tuple[str, ...]:
        batch_labels = tuple(dict.fromkeys(section.label for section in report.batch_sections))
        return (
            f"Selected entry count: {report.metadata.record_count}",
            f"Scored entry count: {report.summary.scored_count} of {report.summary.count}",
            f"Represented models: {self._report_models([entry.entry.model_name for entry in report.entries])}",
            f"Import batches: {', '.join(batch_labels) if batch_labels else 'None represented'}",
            f"Filters: {'; '.join(self._report_scoreboard_filter_summary(options.filters))}",
            "Missing scores remain unavailable; they are not treated as zero.",
            "Soft-deleted entries and batches: excluded by the reporting engine.",
            f"Template: {self._report_template_label(options.template_id)}",
        )

    def _leaderboard_selection_lines(self, report: ModelLeaderboardReport, options: LeaderboardReportOptions) -> tuple[str, ...]:
        top_model = report.models[0].model_name if report.models else "None available"
        unscored = tuple(entry.model_name for entry in report.models if entry.scored_run_count == 0)
        return (
            f"Ranked model count: {len(report.models)}",
            f"Contributing run count: {report.metadata.record_count}",
            f"Top-ranked model: {top_model}",
            f"Filters: {'; '.join(self._report_filter_summary(options.filters))}",
            f"Per-model detail sections: {'Included' if options.include_model_details else 'Excluded'}",
            f"Template: {self._report_template_label(options.template_id)}",
            "Ranking: average overall score descending; scored-run count descending; median overall score descending; deterministic model-name ordering.",
            f"Models without scored runs: {', '.join(unscored) if unscored else 'None represented'}",
            "Missing scores and speeds remain unavailable; they are not treated as zero.",
        )

    def _session_selection_lines(self, report: SessionReport, options: SessionReportOptions) -> tuple[str, ...]:
        period = " → ".join(
            value for value in (report.session.started_at, report.session.completed_at) if value
        ) or "Date range unavailable"
        return (
            f"Session: {report.session.title or self._report_value(report.session.id)}",
            f"Session period: {period}",
            f"Eligible run count: {report.metadata.record_count}",
            f"Scored run count: {report.summary.scored_count} of {report.summary.count}",
            f"Represented models: {self._report_models(list(report.represented_models))}",
            f"Represented benchmarks: {self._report_models(list(report.represented_benchmarks))}",
            f"Represented hardware: {self._report_models(list(report.represented_hardware))}",
            f"Average score: {self._report_number(report.summary.average)}; median: {self._report_number(report.summary.median)}",
            f"Average tokens/s: {self._report_number(report.average_tokens_per_second)}",
            f"Filters: {'; '.join(self._report_filter_summary(options.filters))}",
            f"Template: {self._report_template_label(options.template_id)}",
            f"Prompt text: {'Included' if options.include_prompt_text else 'Excluded'}",
            f"Raw model output: {'Included' if options.include_raw_model_output else 'Excluded'}",
            f"Attachment metadata: {'Included' if options.include_attachment_metadata else 'Excluded'}",
            "Soft-deleted sessions and runs: excluded by the reporting engine.",
        )

    def _hardware_selection_lines(self, report: HardwareReport, options: HardwareReportOptions) -> tuple[str, ...]:
        fastest = report.fastest_group.label if report.fastest_group else "Not calculable"
        highest = report.highest_average_score_group.label if report.highest_average_score_group else "Not calculable"
        return (
            f"Hardware group count: {len(report.groups)}",
            f"Contributing run count: {report.metadata.record_count}",
            f"Scored run count: {report.summary.scored_count} of {report.summary.count}",
            f"Represented models: {self._report_models(list(report.represented_models))}",
            f"Fastest group: {fastest}",
            f"Highest average-score group: {highest}",
            f"Filters: {'; '.join(self._report_filter_summary(options.filters))}",
            f"Per-hardware detail sections: {'Included' if options.include_hardware_details else 'Excluded'}",
            f"Template: {self._report_template_label(options.template_id)}",
            "Missing scores and speeds remain unavailable; they are not treated as zero.",
            "Historical hardware snapshots are authoritative; distinct snapshots remain distinct groups.",
        )

    def _report_empty_screen(self, title: str, lines: tuple[str, ...]) -> None:
        self.render_screen(title, "\n".join((*lines, "", "B) Back", "QA) Quit BenchPup completely")))
        self.ask("Choose an option", navigation=True)

    def _report_error_screen(self, title: str, message: str) -> None:
        self.render_screen(title, f"{message}\n\nB) Back\nQA) Quit BenchPup completely")
        self.ask("Choose an option", navigation=True)

    def _report_selection_preview(self, title: str, lines: tuple[str, ...]) -> None:
        self.render_screen(title, "\n".join((*lines, "", "B) Back", "QA) Quit BenchPup completely")))
        self.ask("Choose an option", navigation=True)

    def _report_write_result_screen(self, result: ReportWriteResult) -> None:
        if result.status is ReportWriteStatus.SUCCESS:
            title = "Report Write Complete"
            explanation = "The staged UTF-8 Markdown report was finalized successfully."
        elif result.status is ReportWriteStatus.OVERWRITE_REQUIRED:
            title = "Report Write Requires Confirmation"
            explanation = "The destination already exists; no replacement was written."
        elif result.status is ReportWriteStatus.TEMP_WRITE_FAILED:
            title = "Report Temporary Write Failed"
            explanation = "The report could not be staged; the final destination was not replaced."
        elif result.status is ReportWriteStatus.FINALIZE_FAILED:
            title = "Report Finalization Failed"
            explanation = "The staged report could not be finalized; inspect the destination before retrying."
        else:
            title = "Report Write Failed"
            explanation = "The report writer returned an unrecognized failure status."
        lines = [
            explanation,
            "",
            f"Status: {result.status.value}",
            f"Path: {result.path}",
        ]
        if result.message:
            lines.append(f"Message: {result.message}")
        if result.details:
            lines.append(f"Details: {result.details}")
        lines.extend(("", "B) Back", "QA) Quit BenchPup completely"))
        self.render_screen(title, "\n".join(lines))
        self.ask("Choose an option", navigation=True)

    def _write_report_workflow(
        self,
        report: BenchmarkRunReport | ScoreboardReport | ModelLeaderboardReport | SessionReport | HardwareReport | ModelComparisonResult | SessionComparisonResult | TrendReport,
        *,
        report_name: str,
        selection_lines: tuple[str, ...],
        destination: str,
        include_model_details: bool = False,
        template_options: ReportTemplateOptions | None = None,
    ) -> str | None:
        self.render_screen(f"{report_name} Selection", "\n".join(selection_lines))
        path = self.prompt_path(
            "Markdown destination",
            default=destination or None,
            preserve_trailing_separator=True,
        )
        if not isinstance(path, str):
            return None
        output_path = self.prepare_export_destination(path, report_name)
        if not isinstance(output_path, Path):
            return None
        self.render_screen(
            f"Confirm {report_name}",
            "\n".join((
                *selection_lines,
                "",
                f"Destination: {output_path}",
                "The report will be written with staged UTF-8 output.",
                "",
                "Write this report?",
                "B) Back",
                "QA) Quit BenchPup completely",
            )),
        )
        confirm = self.yes_no(f"Confirm {report_name}", navigation=True)
        if confirm is not True:
            return None
        try:
            result = self.reporting.write_markdown_report(
                report,
                output_path,
                include_model_details=include_model_details,
                template_options=template_options,
            )
        except (OSError, ValueError) as error:
            self._report_error_screen(f"{report_name} Failed", f"Report generation or writing failed: {error}")
            return str(output_path)
        if result.status is ReportWriteStatus.OVERWRITE_REQUIRED:
            self.render_screen(
                "Confirm Report Overwrite",
                f"An existing report is at:\n{output_path}\n\nReplace it only after staged validation?\n\nB) Back\nQA) Quit BenchPup completely",
            )
            overwrite = self.yes_no("Replace existing report", navigation=True)
            if overwrite is True:
                try:
                    result = self.reporting.write_markdown_report(
                        report,
                        output_path,
                        overwrite=True,
                        include_model_details=include_model_details,
                        template_options=template_options,
                    )
                except (OSError, ValueError) as error:
                    self._report_error_screen("Report Overwrite Failed", f"Report overwrite failed: {error}")
                    return str(output_path)
            elif isinstance(overwrite, NavigationSignal):
                return None
        self._report_write_result_screen(result)
        return str(output_path)

    def _detailed_benchmark_report_screen(self, options: BenchmarkReportOptions) -> BenchmarkReportOptions:
        while True:
            content = "\n".join((
                "1) Configure Report Options",
                "2) Preview Selection",
                "3) Write Markdown Report",
                "",
                *self._report_filter_summary(options.filters),
                f"Prompt text: {'Included' if options.include_prompt_text else 'Excluded'}",
                f"Raw model output: {'Included' if options.include_raw_model_output else 'Excluded'}",
                f"Attachment metadata: {'Included' if options.include_attachment_metadata else 'Excluded'}",
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Detailed Benchmark Run Report", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                options = self._benchmark_report_options_screen(options)
                continue
            if command not in {"2", "3"}:
                self.output("Choose 1, 2, or 3, or B to return.")
                continue
            try:
                report = self.reporting.benchmark_run_report(
                    filters=options.filters,
                    include_prompt_text=options.include_prompt_text,
                    include_raw_model_output=options.include_raw_model_output,
                    include_attachment_metadata=options.include_attachment_metadata,
                    title=options.title,
                    **self._report_template_argument(
                        options.template_id,
                        include_prompt_text=options.include_prompt_text,
                        include_raw_model_output=options.include_raw_model_output,
                        include_attachment_metadata=options.include_attachment_metadata,
                    ),
                )
            except (OSError, ValueError) as error:
                self._report_error_screen("Detailed Report Failed", f"Could not build the report: {error}")
                continue
            selection_lines = self._benchmark_selection_lines(report, options)
            if command == "2":
                self._report_selection_preview("Detailed Benchmark Run Selection", selection_lines)
                continue
            if not report.records:
                self._report_empty_screen("No Benchmark Runs", selection_lines + ("No benchmark runs match the current selection.",))
                continue
            destination = self._write_report_workflow(
                report,
                report_name="Detailed Benchmark Run Report",
                selection_lines=selection_lines,
                destination=options.destination,
                template_options=self._report_template_options_for(
                    options.template_id,
                    include_prompt_text=options.include_prompt_text,
                    include_raw_model_output=options.include_raw_model_output,
                    include_attachment_metadata=options.include_attachment_metadata,
                ),
            )
            if destination is not None:
                options = replace(options, destination=destination)

    def _historical_scoreboard_report_screen(self, options: ScoreboardReportOptions) -> ScoreboardReportOptions:
        while True:
            content = "\n".join((
                "1) Configure Report Options",
                "2) Preview Selection",
                "3) Write Markdown Report",
                "",
                *self._report_scoreboard_filter_summary(options.filters),
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Historical Scoreboard Report", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                options = self._scoreboard_report_options_screen(options)
                continue
            if command not in {"2", "3"}:
                self.output("Choose 1, 2, or 3, or B to return.")
                continue
            try:
                report = self.reporting.scoreboard_report(
                    filters=options.filters,
                    title=options.title,
                    **self._report_template_argument(options.template_id),
                )
            except (OSError, ValueError) as error:
                self._report_error_screen("Scoreboard Report Failed", f"Could not build the report: {error}")
                continue
            selection_lines = self._scoreboard_selection_lines(report, options)
            if command == "2":
                self._report_selection_preview("Historical Scoreboard Selection", selection_lines)
                continue
            if not report.entries:
                self._report_empty_screen("No Scoreboard Entries", selection_lines + ("No scoreboard entries match the current selection.",))
                continue
            destination = self._write_report_workflow(
                report,
                report_name="Historical Scoreboard Report",
                selection_lines=selection_lines,
                destination=options.destination,
                template_options=self._report_template_options_for(options.template_id),
            )
            if destination is not None:
                options = replace(options, destination=destination)

    def _leaderboard_terminal_preview(self, report: ModelLeaderboardReport, options: LeaderboardReportOptions) -> None:
        lines = list(self._leaderboard_selection_lines(report, options))
        lines.extend(("", "Leaderboard preview:"))
        if not report.models:
            lines.append("No models are represented by the current selection.")
        else:
            for entry in report.models[:10]:
                lines.append(
                    f"{entry.rank}) {entry.model_name} | average={self._report_number(entry.average_overall_score)} "
                    f"| scored runs={entry.scored_run_count} | median={self._report_number(entry.median_overall_score)}"
                )
            if len(report.models) > 10:
                lines.append(f"... plus {len(report.models) - 10} more ranked model(s).")
        self._report_selection_preview("Model Leaderboard Preview", tuple(lines))

    def _model_leaderboard_report_screen(self, options: LeaderboardReportOptions) -> LeaderboardReportOptions:
        while True:
            content = "\n".join((
                "1) Configure Report Options",
                "2) Preview Ranking",
                "3) View Concise Terminal Preview",
                "4) Save Markdown Leaderboard",
                "",
                *self._report_filter_summary(options.filters),
                f"Per-model details: {'Included' if options.include_model_details else 'Excluded'}",
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Model Leaderboard", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                options = self._leaderboard_report_options_screen(options)
                continue
            if command not in {"2", "3", "4"}:
                self.output("Choose 1, 2, 3, or 4, or B to return.")
                continue
            try:
                report = self.reporting.model_leaderboard(
                    filters=options.filters,
                    title=options.title,
                    **self._report_template_argument(options.template_id),
                )
            except (OSError, ValueError) as error:
                self._report_error_screen("Leaderboard Failed", f"Could not build the leaderboard: {error}")
                continue
            selection_lines = self._leaderboard_selection_lines(report, options)
            if command == "2":
                if not report.models:
                    self._report_empty_screen("No Leaderboard Models", selection_lines + ("No models match the current selection.",))
                elif not any(entry.scored_run_count for entry in report.models):
                    self._report_empty_screen("No Eligible Leaderboard Scores", selection_lines + ("No eligible scored runs match the current selection.",))
                else:
                    self._report_selection_preview("Model Leaderboard Ranking", selection_lines)
                continue
            if command == "3":
                self._leaderboard_terminal_preview(report, options)
                continue
            if not report.models or not any(entry.scored_run_count for entry in report.models):
                self._report_empty_screen("No Eligible Leaderboard Scores", selection_lines + ("No eligible scored runs match the current selection.",))
                continue
            destination = self._write_report_workflow(
                report,
                report_name="Model Leaderboard",
                selection_lines=selection_lines,
                destination=options.destination,
                include_model_details=options.include_model_details,
                template_options=self._report_template_options_for(
                    options.template_id,
                    include_model_details=options.include_model_details,
                ),
            )
            if destination is not None:
                options = replace(options, destination=destination)

    def _session_report_screen(self, options: SessionReportOptions) -> SessionReportOptions:
        while True:
            session = self.catalog.sessions.get(options.filters.session_id) if options.filters.session_id is not None else None
            content = "\n".join((
                "1) Configure Session Report Options",
                "2) Preview Session Report",
                "3) Write Markdown Report",
                "",
                f"Session: {session.title if session else 'Not selected'}",
                *self._report_filter_summary(options.filters),
                f"Template: {self._report_template_label(options.template_id)}",
                f"Prompt text: {'Included' if options.include_prompt_text else 'Excluded'}",
                f"Raw model output: {'Included' if options.include_raw_model_output else 'Excluded'}",
                f"Attachment metadata: {'Included' if options.include_attachment_metadata else 'Excluded'}",
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Session Report", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                options = self._session_report_options_screen(options)
                continue
            if command not in {"2", "3"}:
                self.output("Choose 1, 2, or 3, or B to return.")
                continue
            if options.filters.session_id is None:
                self._report_empty_screen(
                    "No Session Selected",
                    ("Select a session before previewing or writing a session report.",),
                )
                continue
            try:
                report = self.reporting.session_report(
                    options.filters.session_id,
                    filters=options.filters,
                    include_prompt_text=options.include_prompt_text,
                    include_raw_model_output=options.include_raw_model_output,
                    include_attachment_metadata=options.include_attachment_metadata,
                    title=options.title,
                    **self._report_template_argument(
                        options.template_id,
                        include_prompt_text=options.include_prompt_text,
                        include_raw_model_output=options.include_raw_model_output,
                        include_attachment_metadata=options.include_attachment_metadata,
                    ),
                )
            except (OSError, ValueError) as error:
                self._report_error_screen("Session Report Failed", f"Could not build the session report: {error}")
                continue
            selection_lines = self._session_selection_lines(report, options)
            if command == "2":
                self._report_selection_preview("Session Report Preview", selection_lines)
                continue
            if not report.records:
                self._report_empty_screen(
                    "No Eligible Session Runs",
                    selection_lines + ("The selected session has no eligible benchmark runs.",),
                )
                continue
            destination = self._write_report_workflow(
                report,
                report_name="Session Report",
                selection_lines=selection_lines,
                destination=options.destination,
                template_options=self._report_template_options_for(
                    options.template_id,
                    include_prompt_text=options.include_prompt_text,
                    include_raw_model_output=options.include_raw_model_output,
                    include_attachment_metadata=options.include_attachment_metadata,
                ),
            )
            if destination is not None:
                options = replace(options, destination=destination)

    def _hardware_report_screen(self, options: HardwareReportOptions) -> HardwareReportOptions:
        while True:
            content = "\n".join((
                "1) Configure Hardware Report Options",
                "2) Preview Hardware Report",
                "3) Write Markdown Report",
                "",
                *self._report_filter_summary(options.filters),
                f"Template: {self._report_template_label(options.template_id)}",
                f"Per-hardware details: {'Included' if options.include_hardware_details else 'Excluded'}",
                f"Prompt text: {'Included' if options.include_prompt_text else 'Excluded'}",
                f"Raw model output: {'Included' if options.include_raw_model_output else 'Excluded'}",
                f"Attachment metadata: {'Included' if options.include_attachment_metadata else 'Excluded'}",
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Hardware Report", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                options = self._hardware_report_options_screen(options)
                continue
            if command not in {"2", "3"}:
                self.output("Choose 1, 2, or 3, or B to return.")
                continue
            try:
                report = self.reporting.hardware_report(
                    filters=options.filters,
                    include_prompt_text=options.include_prompt_text,
                    include_raw_model_output=options.include_raw_model_output,
                    include_attachment_metadata=options.include_attachment_metadata,
                    include_hardware_details=options.include_hardware_details,
                    title=options.title,
                    **self._report_template_argument(
                        options.template_id,
                        include_prompt_text=options.include_prompt_text,
                        include_raw_model_output=options.include_raw_model_output,
                        include_attachment_metadata=options.include_attachment_metadata,
                        include_hardware_details=options.include_hardware_details,
                    ),
                )
            except (OSError, ValueError) as error:
                self._report_error_screen("Hardware Report Failed", f"Could not build the hardware report: {error}")
                continue
            selection_lines = self._hardware_selection_lines(report, options)
            if command == "2":
                self._report_selection_preview("Hardware Report Preview", selection_lines)
                continue
            if not report.groups:
                self._report_empty_screen(
                    "No Hardware Report Runs",
                    selection_lines + ("No eligible benchmark runs have hardware report data.",),
                )
                continue
            destination = self._write_report_workflow(
                report,
                report_name="Hardware Report",
                selection_lines=selection_lines,
                destination=options.destination,
                template_options=self._report_template_options_for(
                    options.template_id,
                    include_hardware_details=options.include_hardware_details,
                    include_prompt_text=options.include_prompt_text,
                    include_raw_model_output=options.include_raw_model_output,
                    include_attachment_metadata=options.include_attachment_metadata,
                ),
            )
            if destination is not None:
                options = replace(options, destination=destination)

    def _model_comparison_selection_lines(
        self,
        report: ModelComparisonResult,
        options: ModelComparisonOptions,
    ) -> tuple[str, ...]:
        lines = [
            f"Models: {', '.join(options.models) if options.models else 'None selected'}",
            *self._comparison_filter_summary(options.filters),
            f"Contributing records: {report.metadata.contributing_record_count}",
            f"Shared benchmarks: {report.alignment.shared_benchmark_count}",
            f"Non-overlapping benchmark identities: {report.alignment.excluded_benchmark_count}",
        ]
        if report.ranking:
            ranked = ", ".join(
                f"{entry.rank or 'unranked'}. {entry.label}"
                for entry in report.ranking
            )
            lines.append(f"Ranking: {ranked}")
        return tuple(lines)

    def _session_comparison_selection_lines(
        self,
        report: SessionComparisonResult,
        options: SessionComparisonOptions,
    ) -> tuple[str, ...]:
        labels = [session.label for session in report.selected_sessions]
        return (
            f"Sessions: {', '.join(labels) if labels else 'None selected'}",
            *self._comparison_filter_summary(options.filters),
            f"Contributing records: {report.metadata.contributing_record_count}",
            f"Shared models: {report.alignment.shared_model_count}",
            f"Shared benchmarks: {report.alignment.shared_benchmark_count}",
            f"Shared model/benchmark pairs: {report.alignment.shared_model_benchmark_pair_count}",
        )

    def _model_comparison_terminal_lines(self, report: ModelComparisonResult) -> tuple[str, ...]:
        lines = [
            f"Selected models: {', '.join(report.selected_models)}",
            f"Contributing BenchmarkRun records: {report.metadata.contributing_record_count}",
            f"Shared benchmarks: {report.alignment.shared_benchmark_count}",
            "",
            "Model | Runs | Scored | Mean score | Median score | Mean tokens/s",
            "-" * 72,
        ]
        for entity in report.entities:
            lines.append(
                f"{entity.label} | {entity.record_count} | {entity.scored_count} | "
                f"{self._report_number(entity.overall_score.mean)} | "
                f"{self._report_number(entity.overall_score.median)} | "
                f"{self._report_number(entity.tokens_per_second.mean)}"
            )
        lines.extend(("", f"Shared benchmark summaries: {len(report.alignment.aligned_benchmarks)}"))
        if report.pairwise is not None:
            lines.append("Pairwise deltas: second selected model minus first selected model")
        lines.append("Missing values are shown as unavailable; no zero fill is applied.")
        return tuple(lines)

    def _session_comparison_terminal_lines(self, report: SessionComparisonResult) -> tuple[str, ...]:
        lines = [
            f"Selected sessions: {', '.join(report.metadata.selected_entities)}",
            f"Contributing BenchmarkRun records: {report.metadata.contributing_record_count}",
            f"Shared models: {report.alignment.shared_model_count}",
            f"Shared benchmarks: {report.alignment.shared_benchmark_count}",
            f"Shared model/benchmark pairs: {report.alignment.shared_model_benchmark_pair_count}",
            "",
            "Session | Runs | Scored | Mean score | Median score | Mean tokens/s",
            "-" * 72,
        ]
        for entity in report.entities:
            lines.append(
                f"{entity.label} | {entity.record_count} | {entity.scored_count} | "
                f"{self._report_number(entity.overall_score.mean)} | "
                f"{self._report_number(entity.overall_score.median)} | "
                f"{self._report_number(entity.tokens_per_second.mean)}"
            )
        if report.pairwise is not None:
            lines.extend(("", "Pairwise deltas: second selected session minus first selected session"))
        lines.append("Missing values are shown as unavailable; no zero fill is applied.")
        return tuple(lines)

    def _model_comparison_screen(self, options: ModelComparisonOptions) -> ModelComparisonOptions:
        while True:
            content = "\n".join((
                "1) Select Models",
                "2) Configure Comparison Filters",
                "3) Preview Comparison Selection",
                "4) View Terminal Comparison",
                "5) Write Markdown Report",
                "",
                f"Selected models: {', '.join(options.models) if options.models else 'None'}",
                *self._comparison_filter_summary(options.filters),
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Model Comparison", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                choices = self._available_comparison_models()
                selected = self._comparison_multi_select("Select Models", choices, options.models)
                if isinstance(selected, NavigationSignal):
                    continue
                if len(selected) < 2:
                    self.output("Select at least two distinct models before continuing.")
                else:
                    options = replace(options, models=tuple(str(value) for value in selected))
            elif command == "2":
                options = replace(
                    options,
                    filters=self._comparison_filters_screen(options.filters, comparison_type="models"),
                )
            elif command not in {"3", "4", "5"}:
                self.output("Choose a number from 1 to 5, or B to return.")
                continue
            if command in {"3", "4", "5"}:
                if len(options.models) < 2:
                    self.output("Select at least two distinct models before building a comparison.")
                    continue
                try:
                    report = self.comparisons.compare_models(
                        options.models,
                        filters=options.filters,
                        title=options.title,
                    )
                except (OSError, ValueError) as error:
                    self._report_error_screen("Model Comparison Failed", f"Could not build the model comparison: {error}")
                    continue
                selection_lines = self._model_comparison_selection_lines(report, options)
                if command == "3":
                    self._report_selection_preview("Model Comparison Selection", selection_lines)
                elif command == "4":
                    self._report_selection_preview("Model Comparison", self._model_comparison_terminal_lines(report))
                elif report.metadata.contributing_record_count == 0:
                    self._report_empty_screen(
                        "No Eligible Model Comparison Runs",
                        selection_lines + ("No eligible BenchmarkRun snapshots match the current comparison.",),
                    )
                else:
                    destination = self._write_report_workflow(
                        report,
                        report_name="Model Comparison",
                        selection_lines=selection_lines,
                        destination=options.destination,
                    )
                    if destination is not None:
                        options = replace(options, destination=destination)

    def _session_comparison_screen(self, options: SessionComparisonOptions) -> SessionComparisonOptions:
        while True:
            labels = []
            for session_id in options.sessions:
                session = self.catalog.sessions.get(session_id)
                labels.append(session.title if session else f"Session {session_id}")
            content = "\n".join((
                "1) Select Sessions",
                "2) Configure Comparison Filters",
                "3) Preview Comparison Selection",
                "4) View Terminal Comparison",
                "5) Write Markdown Report",
                "",
                f"Selected sessions: {', '.join(labels) if labels else 'None'}",
                *self._comparison_filter_summary(options.filters),
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Session Comparison", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                selected = self._comparison_multi_select(
                    "Select Sessions",
                    self._available_comparison_sessions(),
                    options.sessions,
                )
                if isinstance(selected, NavigationSignal):
                    continue
                if len(selected) < 2:
                    self.output("Select at least two distinct sessions before continuing.")
                else:
                    options = replace(options, sessions=tuple(int(value) for value in selected))
            elif command == "2":
                options = replace(
                    options,
                    filters=self._comparison_filters_screen(options.filters, comparison_type="sessions"),
                )
            elif command not in {"3", "4", "5"}:
                self.output("Choose a number from 1 to 5, or B to return.")
                continue
            if command in {"3", "4", "5"}:
                if len(options.sessions) < 2:
                    self.output("Select at least two distinct sessions before building a comparison.")
                    continue
                try:
                    report = self.comparisons.compare_sessions(
                        options.sessions,
                        filters=options.filters,
                        title=options.title,
                    )
                except (OSError, ValueError) as error:
                    self._report_error_screen("Session Comparison Failed", f"Could not build the session comparison: {error}")
                    continue
                selection_lines = self._session_comparison_selection_lines(report, options)
                if command == "3":
                    self._report_selection_preview("Session Comparison Selection", selection_lines)
                elif command == "4":
                    self._report_selection_preview("Session Comparison", self._session_comparison_terminal_lines(report))
                elif report.metadata.contributing_record_count == 0:
                    self._report_empty_screen(
                        "No Eligible Session Comparison Runs",
                        selection_lines + ("No eligible BenchmarkRun snapshots match the current comparison.",),
                    )
                else:
                    destination = self._write_report_workflow(
                        report,
                        report_name="Session Comparison",
                        selection_lines=selection_lines,
                        destination=options.destination,
                    )
                    if destination is not None:
                        options = replace(options, destination=destination)

    def _comparison_options_lines(
        self,
        model_options: ModelComparisonOptions,
        session_options: SessionComparisonOptions,
    ) -> tuple[str, ...]:
        session_labels = tuple(
            session.title if (session := self.catalog.sessions.get(session_id)) else f"Unavailable session {session_id}"
            for session_id in session_options.sessions
        )
        return (
            "Model comparison:",
            f"  Models: {', '.join(model_options.models) if model_options.models else 'None selected'}",
            f"  Filters: {'; '.join(self._comparison_filter_summary(model_options.filters))}",
            f"  Destination: {self._report_value(model_options.destination)}",
            "",
            "Session comparison:",
            f"  Sessions: {', '.join(session_labels) if session_labels else 'None selected'}",
            f"  Filters: {'; '.join(self._comparison_filter_summary(session_options.filters))}",
            f"  Destination: {self._report_value(session_options.destination)}",
        )

    def comparisons_screen(self) -> None:
        """Navigate model and session comparisons while retaining options in memory."""

        model_options = self._model_comparison_options
        session_options = self._session_comparison_options
        while True:
            content = "\n".join((
                "1) Compare Models",
                "2) Compare Sessions",
                "3) View Current Comparison Options",
                "",
                "Comparisons use immutable BenchmarkRun snapshots and exclude deleted runs by default.",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Comparisons", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                self._model_comparison_options = model_options
                self._session_comparison_options = session_options
                return
            command = self.normalized(choice)
            if command == "1":
                model_options = self._model_comparison_screen(model_options)
                self._model_comparison_options = model_options
            elif command == "2":
                session_options = self._session_comparison_screen(session_options)
                self._session_comparison_options = session_options
            elif command == "3":
                self.render_screen("Current Comparison Options", "\n".join(self._comparison_options_lines(model_options, session_options)))
                self.ask("Choose an option", navigation=True)
            else:
                self.output("Choose 1, 2, or 3, or B to return.")

    def comparison_screen(self) -> None:
        """Compatibility alias for callers using the singular screen name."""

        self.comparisons_screen()

    def _trend_selection_lines(
        self,
        report: TrendReport,
        options: BenchmarkTrendOptions | ScoreboardTrendOptions,
    ) -> tuple[str, ...]:
        metadata = report.metadata
        interval = self._trend_interval_label(options.interval)
        grouping = self._trend_grouping_label(options.grouping)
        filters = (
            self._comparison_filter_summary(options.filters)
            if isinstance(options, BenchmarkTrendOptions)
            else self._trend_scoreboard_filter_summary(options.filters)
        )
        first_score = report.aggregate_series.first_available_score_summary
        last_score = report.aggregate_series.last_available_score_summary
        first_speed = report.aggregate_series.first_available_speed_summary
        last_speed = report.aggregate_series.last_available_speed_summary
        range_label = "Date range" if isinstance(options, BenchmarkTrendOptions) else "Import-date range"
        return (
            f"Contributing {'run' if isinstance(options, BenchmarkTrendOptions) else 'entry'} count: {metadata.contributing_record_count}",
            f"Excluded missing/invalid timestamp count: {metadata.excluded_timestamp_count}",
            f"Series count: {len(report.series)}",
            f"Populated bucket count: {report.populated_bucket_count}",
            f"{range_label}: {metadata.date_from or 'Not available'} to {metadata.date_to or 'Not available'}",
            f"Interval: {interval}",
            f"Grouping: {grouping}",
            f"Filters: {'; '.join(filters)}",
            f"First score mean: {self._report_number(first_score.mean if first_score else None)}",
            f"Last score mean: {self._report_number(last_score.mean if last_score else None)}",
            f"Score delta (last minus first): {self._report_number(report.aggregate_series.score_absolute_delta)}",
            f"First speed mean: {self._report_number(first_speed.mean if first_speed else None)}",
            f"Last speed mean: {self._report_number(last_speed.mean if last_speed else None)}",
            f"Speed delta (last minus first): {self._report_number(report.aggregate_series.speed_absolute_delta)}",
            f"Coverage warnings: {', '.join(report.coverage_warnings) if report.coverage_warnings else 'None'}",
            f"Empty buckets: {'Included' if report.include_empty_buckets else 'Excluded'}",
        )

    def _trend_terminal_lines(
        self,
        report: TrendReport,
        options: BenchmarkTrendOptions | ScoreboardTrendOptions,
    ) -> tuple[str, ...]:
        lines = list(self._trend_selection_lines(report, options))
        lines.extend(("", "Overall bucket preview:"))
        if not report.aggregate_series.points:
            lines.append("No valid timestamp buckets are represented.")
        else:
            for point in report.aggregate_series.points[:12]:
                lines.append(
                    f"{point.label or point.bucket_start} | records={point.record_count} "
                    f"| scored={point.scored_count} "
                    f"| score mean={self._report_number(point.overall_score.mean)} "
                    f"| score median={self._report_number(point.overall_score.median)} "
                    f"| speed mean={self._report_number(point.tokens_per_second.mean)}"
                )
            if len(report.aggregate_series.points) > 12:
                lines.append(f"... plus {len(report.aggregate_series.points) - 12} more bucket(s).")
        lines.extend(("", "Series deltas:"))
        if not report.series:
            lines.append("No grouped series are represented.")
        else:
            for series in report.series:
                lines.append(
                    f"{series.label} | records={series.total_contributing_records} "
                    f"| score delta={self._report_number(series.score_absolute_delta)} "
                    f"| speed delta={self._report_number(series.speed_absolute_delta)}"
                )
        return tuple(lines)

    def _trend_template_options(
        self,
        options: BenchmarkTrendOptions | ScoreboardTrendOptions,
    ) -> ReportTemplateOptions:
        return ReportTemplateOptions(include_record_details=options.include_series_details)

    def _benchmark_trend_screen(self, options: BenchmarkTrendOptions) -> BenchmarkTrendOptions:
        while True:
            content = "\n".join((
                "1) Configure Trend Options",
                "2) Preview Trend Selection",
                "3) View Concise Terminal Trend",
                "4) Write Markdown Trend Report",
                "",
                *self._comparison_filter_summary(options.filters),
                f"Interval: {self._trend_interval_label(options.interval)}",
                f"Grouping: {self._trend_grouping_label(options.grouping)}",
                f"Include empty buckets: {'Yes' if options.include_empty_buckets else 'No'}",
                f"Detailed series sections: {'Yes' if options.include_series_details else 'No'}",
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Benchmark Run Trends", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                options = self._benchmark_trend_options_screen(options)
                continue
            if command not in {"2", "3", "4"}:
                self.output("Choose 1, 2, 3, or 4, or B to return.")
                continue
            try:
                report = self.trends.benchmark_run_trend(
                    interval=options.interval,
                    grouping=options.grouping,
                    filters=options.filters,
                    include_empty_buckets=options.include_empty_buckets,
                    title=options.title,
                )
            except (OSError, ValueError) as error:
                self._report_error_screen("Benchmark Run Trends Failed", f"Could not build the trend report: {error}")
                continue
            selection_lines = self._trend_selection_lines(report, options)
            if command == "2":
                self._report_selection_preview("Benchmark Run Trend Selection", selection_lines)
                continue
            if command == "3":
                self._report_selection_preview("Benchmark Run Trend Preview", self._trend_terminal_lines(report, options))
                continue
            if report.contributing_record_count == 0:
                self._report_empty_screen(
                    "No BenchmarkRun Trend Data",
                    selection_lines + (
                        "No valid timestamped BenchmarkRun snapshots match the current trend options.",
                    ),
                )
                continue
            destination = self._write_report_workflow(
                report,
                report_name="Benchmark Run Trends",
                selection_lines=selection_lines,
                destination=options.destination,
                template_options=self._trend_template_options(options),
            )
            if destination is not None:
                options = replace(options, destination=destination)

    def _scoreboard_trend_screen(self, options: ScoreboardTrendOptions) -> ScoreboardTrendOptions:
        while True:
            content = "\n".join((
                "1) Configure Trend Options",
                "2) Preview Trend Selection",
                "3) View Concise Terminal Trend",
                "4) Write Markdown Trend Report",
                "",
                *self._trend_scoreboard_filter_summary(options.filters),
                f"Interval: {self._trend_interval_label(options.interval)}",
                f"Grouping: {self._trend_grouping_label(options.grouping)}",
                f"Include empty buckets: {'Yes' if options.include_empty_buckets else 'No'}",
                f"Detailed series sections: {'Yes' if options.include_series_details else 'No'}",
                f"Destination: {self._report_value(options.destination)}",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Historical Scoreboard Trends", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                return options
            command = self.normalized(choice)
            if command == "1":
                options = self._scoreboard_trend_options_screen(options)
                continue
            if command not in {"2", "3", "4"}:
                self.output("Choose 1, 2, 3, or 4, or B to return.")
                continue
            try:
                report = self.trends.scoreboard_entry_trend(
                    interval=options.interval,
                    grouping=options.grouping,
                    filters=options.filters,
                    include_empty_buckets=options.include_empty_buckets,
                    title=options.title,
                )
            except (OSError, ValueError) as error:
                self._report_error_screen("Scoreboard Trends Failed", f"Could not build the trend report: {error}")
                continue
            selection_lines = self._trend_selection_lines(report, options)
            if command == "2":
                self._report_selection_preview("Historical Scoreboard Trend Selection", selection_lines)
                continue
            if command == "3":
                self._report_selection_preview("Historical Scoreboard Trend Preview", self._trend_terminal_lines(report, options))
                continue
            if report.contributing_record_count == 0:
                self._report_empty_screen(
                    "No Scoreboard Trend Data",
                    selection_lines + (
                        "No valid timestamped ScoreboardEntry records match the current trend options.",
                    ),
                )
                continue
            destination = self._write_report_workflow(
                report,
                report_name="Historical Scoreboard Trends",
                selection_lines=selection_lines,
                destination=options.destination,
                template_options=self._trend_template_options(options),
            )
            if destination is not None:
                options = replace(options, destination=destination)

    def _trend_options_lines(
        self,
        benchmark: BenchmarkTrendOptions,
        scoreboard: ScoreboardTrendOptions,
    ) -> tuple[str, ...]:
        return (
            "Benchmark Run Trends",
            f"  Title: {benchmark.title}",
            f"  Interval: {self._trend_interval_label(benchmark.interval)}",
            f"  Grouping: {self._trend_grouping_label(benchmark.grouping)}",
            f"  Filters: {'; '.join(self._comparison_filter_summary(benchmark.filters))}",
            f"  Empty buckets: {'Included' if benchmark.include_empty_buckets else 'Excluded'}",
            f"  Detailed series sections: {'Included' if benchmark.include_series_details else 'Excluded'}",
            f"  Destination: {self._report_value(benchmark.destination)}",
            "",
            "Historical Scoreboard Trends",
            f"  Title: {scoreboard.title}",
            f"  Interval: {self._trend_interval_label(scoreboard.interval)}",
            f"  Grouping: {self._trend_grouping_label(scoreboard.grouping)}",
            f"  Filters: {'; '.join(self._trend_scoreboard_filter_summary(scoreboard.filters))}",
            f"  Empty buckets: {'Included' if scoreboard.include_empty_buckets else 'Excluded'}",
            f"  Detailed series sections: {'Included' if scoreboard.include_series_details else 'Excluded'}",
            f"  Destination: {self._report_value(scoreboard.destination)}",
            "",
            "Overwrite: explicit confirmation is required for an existing file.",
            "",
            "B) Back",
            "QA) Quit BenchPup completely",
        )

    def trends_screen(self) -> None:
        """Navigate UI-independent BenchmarkRun and ScoreboardEntry trends."""

        benchmark_options = self._benchmark_trend_options
        scoreboard_options = self._scoreboard_trend_options
        while True:
            content = "\n".join((
                "1) Benchmark Run Trends",
                "2) Historical Scoreboard Trends",
                "3) View Current Trend Options",
                "",
                "Trend options are session-local and never written to SQLite.",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Trends", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                self._benchmark_trend_options = benchmark_options
                self._scoreboard_trend_options = scoreboard_options
                return
            command = self.normalized(choice)
            if command == "1":
                benchmark_options = self._benchmark_trend_screen(benchmark_options)
                self._benchmark_trend_options = benchmark_options
            elif command == "2":
                scoreboard_options = self._scoreboard_trend_screen(scoreboard_options)
                self._scoreboard_trend_options = scoreboard_options
            elif command == "3":
                self.render_screen("Current Trend Options", "\n".join(self._trend_options_lines(benchmark_options, scoreboard_options)))
                self.ask("Choose an option", navigation=True)
            else:
                self.output("Choose 1, 2, or 3, or B to return.")

    def trend_screen(self) -> None:
        """Compatibility alias for callers using the singular screen name."""

        self.trends_screen()

    def reporting_screen(self) -> None:
        """Navigate report workflows while keeping configuration in memory only."""

        benchmark_options = self._benchmark_report_options
        scoreboard_options = self._scoreboard_report_options
        leaderboard_options = self._leaderboard_report_options
        session_options = self._session_report_options
        hardware_options = self._hardware_report_options
        while True:
            content = "\n".join((
                "1) Detailed Benchmark Run Report",
                "2) Historical Scoreboard Report",
                "3) Model Leaderboard",
                "4) Session Report",
                "5) Hardware Report",
                "6) View Current Report Options",
                "",
                "Options are session-local and are never written to SQLite.",
                "",
                "B) Back",
                "QA) Quit BenchPup completely",
            ))
            self.render_screen("Reporting", content)
            choice = self.ask("Choose an option", navigation=True)
            if isinstance(choice, NavigationSignal):
                self._benchmark_report_options = benchmark_options
                self._scoreboard_report_options = scoreboard_options
                self._leaderboard_report_options = leaderboard_options
                self._session_report_options = session_options
                self._hardware_report_options = hardware_options
                return
            command = self.normalized(choice)
            if command == "1":
                benchmark_options = self._detailed_benchmark_report_screen(benchmark_options)
                self._benchmark_report_options = benchmark_options
            elif command == "2":
                scoreboard_options = self._historical_scoreboard_report_screen(scoreboard_options)
                self._scoreboard_report_options = scoreboard_options
            elif command == "3":
                leaderboard_options = self._model_leaderboard_report_screen(leaderboard_options)
                self._leaderboard_report_options = leaderboard_options
            elif command == "4":
                session_options = self._session_report_screen(session_options)
                self._session_report_options = session_options
            elif command == "5":
                hardware_options = self._hardware_report_screen(hardware_options)
                self._hardware_report_options = hardware_options
            elif command == "6":
                self.render_screen(
                    "Current Report Options",
                    "\n".join(
                        self._report_configuration_lines(
                            benchmark_options,
                            scoreboard_options,
                            leaderboard_options,
                            session_options,
                            hardware_options,
                        )
                    ),
                )
                self.ask("Choose an option", navigation=True)
            else:
                self.output("Choose 1, 2, 3, 4, 5, or 6, or B to return.")

    def reports_screen(self) -> None:
        """Compatibility alias for callers that use the shorter screen name."""

        self.reporting_screen()

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

    def _dataset_configuration_summary(self, filters: DatasetFilters, redaction: RedactionConfig) -> tuple[str, ...]:
        filter_values = (
            ("Minimum overall", filters.min_overall), ("Maximum hallucination", filters.max_hallucination),
            ("Minimum reliability", filters.min_reliability), ("Verdict", filters.verdict),
            ("Benchmark type", filters.benchmark_type), ("Model", filters.model), ("Session ID", filters.session_id),
            ("Date range", f"{filters.date_from or '-'} to {filters.date_to or '-'}" if filters.date_from or filters.date_to else ""),
            ("Prompt template ID", filters.prompt_template_id), ("Hardware profile ID", filters.hardware_profile_id),
            ("Include run IDs", filters.include_run_ids), ("Exclude run IDs", filters.exclude_run_ids),
        )
        active_filters = [f"{label}: {self._dataset_filter_value(value)}" for label, value in filter_values if value not in (None, "", frozenset())]
        active_filters.append("Exact duplicates: retain" if filters.keep_source_duplicates else "Exact duplicates: skip")
        enabled_rules = []
        if redaction.literals: enabled_rules.append(f"literals ({len(redaction.literals)})")
        if redaction.redact_paths: enabled_rules.append("paths")
        if redaction.redact_usernames: enabled_rules.append("usernames")
        if redaction.redact_email: enabled_rules.append("email addresses")
        if redaction.redact_hosts_ips: enabled_rules.append("hostnames/IP addresses")
        if redaction.regex_patterns: enabled_rules.append(f"custom regex ({len(redaction.regex_patterns)})")
        return (
            f"Active filters: {'; '.join(active_filters)}",
            f"Active redaction: {', '.join(enabled_rules) if enabled_rules else 'None'}",
        )

    @staticmethod
    def _dataset_count_lines(label: str, counts: dict[str, int]) -> list[str]:
        if not counts:
            return [f"{label}: None"]
        return [f"{label}:", *(f"  {code}: {count}" for code, count in sorted(counts.items()))]

    def _dataset_preview_screen(self, filters: DatasetFilters, redaction: RedactionConfig, *, preview: Any | None = None) -> NavigationSignal | None:
        active_preview = self.datasets.preview(filters, redaction) if preview is None else preview
        candidate_count = len(self.benchmarks.runs.list(include_deleted=True))
        rule_counts = ", ".join(f"{rule}: {count}" for rule, count in sorted(active_preview.redaction_counts.items())) or "None"
        record_lines = []
        for number, record in enumerate(active_preview.records[:10], start=1):
            model = record["input"]["model"].get("model_name", "Unknown model")
            benchmark = record["input"]["benchmark"].get("name") or record["input"]["benchmark"].get("file_path", "Unknown benchmark")
            output_length = len(str(record["input"]["raw_model_output"]))
            record_lines.append(f"{number}) {model} — {benchmark} ({output_length} output characters)")
        if len(active_preview.records) > len(record_lines):
            record_lines.append(f"... plus {len(active_preview.records) - len(record_lines)} more eligible record(s).")
        content = "\n".join((
            f"Total candidate runs: {candidate_count}",
            f"Eligible records: {len(active_preview.records)}",
            f"Excluded records: {sum(active_preview.excluded.values())}",
            f"Warning count: {sum(active_preview.warnings.values())}",
            f"Source-content duplicates: {active_preview.source_duplicates}",
            f"Fingerprint duplicates: {active_preview.fingerprint_duplicates}",
            f"Near duplicates: {active_preview.near_duplicates}",
            f"Post-redaction collisions: {active_preview.post_redaction_collisions}",
            f"Redactions: {active_preview.redactions}",
            f"Redactions by rule: {rule_counts}",
            "",
            *self._dataset_count_lines("Exclusions", active_preview.excluded),
            *self._dataset_count_lines("Warnings", active_preview.warnings),
            "",
            *self._dataset_configuration_summary(filters, redaction),
            "",
            "Eligible record summary:",
            *(record_lines or ["No eligible records."]),
            "",
            "B) Back",
            "QA) Quit BenchPup completely",
        ))
        self.render_screen("Dataset Preview", content)
        choice = self.ask("Choose an option", navigation=True)
        return choice if isinstance(choice, NavigationSignal) else None

    def _dataset_write_result_screen(self, result: DatasetWriteResult, filters: DatasetFilters, redaction: RedactionConfig) -> NavigationSignal | None:
        status_lines: dict[DatasetWriteStatus, tuple[str, str]] = {
            DatasetWriteStatus.SUCCESS: ("Dataset Build Complete", "Dataset and manifest finalized successfully."),
            DatasetWriteStatus.OVERWRITE_REQUIRED: ("Dataset Build Requires Confirmation", "An existing dataset or manifest requires explicit overwrite confirmation."),
            DatasetWriteStatus.VALIDATION_FAILED: ("Dataset Build Validation Failed", "Temporary output did not validate; final files were not replaced."),
            DatasetWriteStatus.TEMP_WRITE_FAILED: ("Dataset Build Temporary Write Failed", "Temporary output could not be written."),
            DatasetWriteStatus.TEMP_CLEANUP_FAILED: ("Dataset Build Cleanup Failed", "The build did not complete and one or more temporary files remain."),
            DatasetWriteStatus.JSONL_FINALIZE_FAILED: ("Dataset JSONL Finalization Failed", "The JSONL file was not finalized; the manifest was not finalized."),
            DatasetWriteStatus.PARTIAL_FINALIZATION: ("Dataset Build Partially Finalized", "The JSONL finalized, but the complete dataset/manifest operation did not finish."),
        }
        title, explanation = status_lines[result.status]
        lines = [explanation, "", f"JSONL path: {result.jsonl_path}", f"Manifest path: {result.manifest_path}"]
        if result.record_count:
            lines.append(f"Record count: {result.record_count}")
        if result.sha256:
            lines.append(f"SHA-256: {result.sha256}")
        lines.extend((
            f"JSONL finalized: {'Yes' if result.jsonl_finalized else 'No'}",
            f"Manifest finalized: {'Yes' if result.manifest_finalized else 'No'}",
            f"Temporary cleanup succeeded: {'Yes' if result.cleanup_succeeded else 'No'}",
        ))
        if result.message:
            lines.append(f"Message: {result.message}")
        if result.details:
            lines.append(f"Details: {result.details}")
        if result.remaining_temp_paths:
            lines.append("Remaining temporary paths:")
            lines.extend(f"  {path}" for path in result.remaining_temp_paths)
        lines.extend(("", *self._dataset_configuration_summary(filters, redaction), "", "B) Back", "QA) Quit BenchPup completely"))
        self.render_screen(title, "\n".join(lines))
        choice = self.ask("Choose an option", navigation=True)
        return choice if isinstance(choice, NavigationSignal) else None

    def dataset_build_workflow(self, filters: DatasetFilters, redaction: RedactionConfig) -> None:
        preview = self.datasets.preview(filters, redaction)
        summary = "\n".join((
            f"Eligible records ready to build: {len(preview.records)}",
            f"Excluded records: {sum(preview.excluded.values())}",
            f"Redactions: {preview.redactions}",
            "",
            *self._dataset_configuration_summary(filters, redaction),
            "",
            "B) Back",
            "QA) Quit BenchPup completely",
        ))
        self.render_screen("Build JSONL Dataset", summary)
        path = self.prompt_path("Dataset destination", default="dataset.jsonl", preserve_trailing_separator=True)
        if not isinstance(path, str):
            return
        output_path = self.prepare_export_destination(path, "JSONL Dataset")
        if not isinstance(output_path, Path):
            return
        manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
        self.render_screen("Confirm Dataset Build", "\n".join((
            f"JSONL path: {output_path}",
            f"Manifest path: {manifest_path}",
            f"Eligible record count: {len(preview.records)}",
            "",
            *self._dataset_configuration_summary(filters, redaction),
            "",
            "Write both staged output files?",
            "B) Back",
            "QA) Quit BenchPup completely",
        )))
        confirm = self.yes_no("Confirm dataset build", navigation=True)
        if confirm is not True:
            return
        result = self.datasets.write_dataset(output_path, filters=filters, redaction_config=redaction)
        if result.status is DatasetWriteStatus.OVERWRITE_REQUIRED:
            overwrite = self.yes_no("Existing dataset or manifest found. Replace both only after staged validation", navigation=True)
            if overwrite is not True:
                self._dataset_write_result_screen(result, filters, redaction)
                return
            result = self.datasets.write_dataset(output_path, filters=filters, redaction_config=redaction, overwrite=True)
        self._dataset_write_result_screen(result, filters, redaction)

    def dataset_validate_workflow(self) -> None:
        path = self.prompt_path("Existing dataset JSONL", must_exist=True, extensions=(".jsonl",))
        if not isinstance(path, str):
            return
        jsonl_path = Path(path)
        dataset_validation = self.datasets.validate_dataset(jsonl_path)
        manifest_path = jsonl_path.with_suffix(jsonl_path.suffix + ".manifest.json")
        manifest_validation = None
        pair_validation = None
        if manifest_path.exists():
            manifest_validation = self.datasets.validate_manifest(manifest_path)
            if dataset_validation.state == "success" and manifest_validation.state == "success":
                pair_validation = self.datasets.verify_dataset_manifest_pair(jsonl_path, manifest_path)
        else:
            use_manifest = self.yes_no("No companion manifest found. Validate a manifest from another path", navigation=True)
            if use_manifest is True:
                supplied = self.prompt_path("Manifest file", must_exist=True, extensions=(".json",))
                if isinstance(supplied, str):
                    manifest_path = Path(supplied)
                    manifest_validation = self.datasets.validate_manifest(manifest_path)
                    if dataset_validation.state == "success" and manifest_validation.state == "success":
                        pair_validation = self.datasets.verify_dataset_manifest_pair(jsonl_path, manifest_path)
        lines = [f"Dataset: {'Valid' if dataset_validation.state == 'success' else 'Invalid'}"]
        if dataset_validation.state == "success":
            lines.extend((f"Record count: {dataset_validation.record_count}", "Blank lines are ignored during JSONL validation."))
        elif dataset_validation.message:
            lines.append(f"Dataset error: {dataset_validation.message}")
        if manifest_validation is None:
            lines.append("Manifest: Not supplied or not found.")
        else:
            lines.append(f"Manifest: {'Valid' if manifest_validation.state == 'success' else 'Invalid'}")
            if manifest_validation.message:
                lines.append(f"Manifest error: {manifest_validation.message}")
            lines.append(f"Manifest path: {manifest_path}")
        if pair_validation is not None:
            lines.append(f"Dataset/manifest pair: {'Valid' if pair_validation.state == 'success' else 'Invalid'}")
            if pair_validation.message:
                lines.append(f"Pair error: {pair_validation.message}")
        lines.extend(("", "B) Back", "QA) Quit BenchPup completely"))
        self.render_screen("Validate Existing Dataset", "\n".join(lines))
        self.ask("Choose an option", navigation=True)

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
            if command == "1": self.dataset_build_workflow(filters, redaction); continue
            if command == "5": self.dataset_validate_workflow(); continue
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
        self.render_screen("Main", f"Database : {database_name}\nRuns     : {runs}\nScoreboard entries : {scoreboard_entries}\nModels   : {models}\nSessions : {sessions}\n\nRuns\n----\n1) Add Run\n2) List Runs\n3) View Run\n4) Edit Run\n5) Delete Run\n\nReference Data\n--------------\n6) Sessions\n7) Models\n8) Benchmarks\n9) Prompt Templates\n10) Hardware Profiles\n\nData\n----\n11) Import\n12) Export\n13) Scoreboard\n14) Backup\n15) Restore\n16) Dataset Builder\n17) Reports\n18) Comparisons\n19) Trends\n\nHelp\n----\nH) Help\nS) Settings\nQ) Quit\nQA) Quit BenchPup completely")

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
            "17": "reports", "report": "reports", "reports": "reports", "reporting": "reports",
            "18": "comparisons", "comparison": "comparisons", "comparisons": "comparisons",
            "19": "trends", "trend": "trends", "trends": "trends",
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
                elif command == "reports": self.reporting_screen()
                elif command == "comparisons": self.comparisons_screen()
                elif command == "trends": self.trends_screen()
                elif command == "settings": self.settings_screen()
                elif command == "help": self.help()
                else: self.output("Choose a menu number or command. Type H for help.")
            except KeyboardInterrupt:
                self.output("\nOperation cancelled.")
            except Exception:
                self.logger.exception("Unexpected CLI error")
                self.output("An unexpected error occurred.\nSee logs/error.log for details.")
