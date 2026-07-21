"""Typed, GUI-facing standard export workflow.

The CLI-facing exporters remain deliberately unchanged at their public
boundary.  This module adds the narrower contract needed by the desktop GUI:
immutable requests, non-mutating metadata previews, destination validation,
and staged writes with explicit overwrite confirmation.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .exporters import (
    export_scoreboard_html,
    render_benchmark_runs_csv,
    render_scoreboard_csv,
)
from .html_reporting import (
    AnalyticsSourceFamily,
    HtmlAnalyticsReportOptions,
)
from .reporting import (
    BenchmarkReportFilters,
    ReportWriteStatus,
    ReportingService,
    ScoreboardReportFilters,
    render_combined_markdown,
    write_markdown_report,
)
from .services import BenchmarkService, CatalogService
from .statistics import StatisticsService
from .trends import TrendService


class StandardExportKind(str, Enum):
    """Stable identifiers for the standard GUI export choices."""

    BENCHMARK_RUNS_CSV = "benchmark_runs_csv"
    SCOREBOARD_CSV = "scoreboard_csv"
    COMBINED_MARKDOWN = "combined_markdown"
    BENCHMARK_RUN_MARKDOWN = "benchmark_run_markdown"
    SCOREBOARD_MARKDOWN = "scoreboard_markdown"
    MODEL_LEADERBOARD_MARKDOWN = "model_leaderboard_markdown"
    SESSION_MARKDOWN = "session_markdown"
    HARDWARE_MARKDOWN = "hardware_markdown"
    SCOREBOARD_HTML = "scoreboard_html"
    HTML_ANALYTICS = "html_analytics"
    JSONL_TRAINING_DATA = "jsonl_training_data"


class StandardExportStatus(str, Enum):
    """Typed outcomes shared by preview and write operations."""

    SUCCESS = "success"
    OVERWRITE_REQUIRED = "overwrite_required"
    INVALID_REQUEST = "invalid_request"
    EMPTY_SELECTION = "empty_selection"
    TEMPORARY_WRITE_FAILED = "temporary_write_failed"
    FINALIZATION_FAILED = "finalization_failed"


EXPORT_LABELS: Mapping[StandardExportKind, str] = MappingProxyType(
    {
        StandardExportKind.BENCHMARK_RUNS_CSV: "Benchmark Runs CSV",
        StandardExportKind.SCOREBOARD_CSV: "Scoreboard CSV",
        StandardExportKind.COMBINED_MARKDOWN: "Combined Markdown",
        StandardExportKind.BENCHMARK_RUN_MARKDOWN: "Benchmark Run Markdown report",
        StandardExportKind.SCOREBOARD_MARKDOWN: "Scoreboard Markdown report",
        StandardExportKind.MODEL_LEADERBOARD_MARKDOWN: "Model Leaderboard Markdown report",
        StandardExportKind.SESSION_MARKDOWN: "Session Markdown report",
        StandardExportKind.HARDWARE_MARKDOWN: "Hardware Markdown report",
        StandardExportKind.SCOREBOARD_HTML: "Scoreboard HTML",
        StandardExportKind.HTML_ANALYTICS: "HTML Analytics",
        StandardExportKind.JSONL_TRAINING_DATA: "JSONL training data (Dataset Builder)",
    }
)

DEFAULT_EXPORT_FILENAMES: Mapping[StandardExportKind, str] = MappingProxyType(
    {
        StandardExportKind.BENCHMARK_RUNS_CSV: "benchmark_runs.csv",
        StandardExportKind.SCOREBOARD_CSV: "scoreboard.csv",
        StandardExportKind.COMBINED_MARKDOWN: "combined-report.md",
        StandardExportKind.BENCHMARK_RUN_MARKDOWN: "benchmark-run-report.md",
        StandardExportKind.SCOREBOARD_MARKDOWN: "scoreboard-report.md",
        StandardExportKind.MODEL_LEADERBOARD_MARKDOWN: "model-leaderboard.md",
        StandardExportKind.SESSION_MARKDOWN: "session-report.md",
        StandardExportKind.HARDWARE_MARKDOWN: "hardware-report.md",
        StandardExportKind.SCOREBOARD_HTML: "scoreboard.html",
        StandardExportKind.HTML_ANALYTICS: "benchpup-analytics.html",
    }
)

SUPPORTED_STANDARD_EXPORT_KINDS: tuple[StandardExportKind, ...] = tuple(
    kind for kind in StandardExportKind if kind is not StandardExportKind.JSONL_TRAINING_DATA
)


def _freeze_mapping(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class StandardExportRequest:
    """The immutable request re-resolved by both preview and write."""

    kind: StandardExportKind | str
    destination: str | Path
    benchmark_filters: BenchmarkReportFilters = field(default_factory=BenchmarkReportFilters)
    scoreboard_filters: ScoreboardReportFilters = field(default_factory=ScoreboardReportFilters)
    run_id: int | None = None
    session_id: int | None = None
    hardware_profile_id: int | None = None
    include_prompt_text: bool = False
    include_raw_model_output: bool = False
    include_attachment_metadata: bool = False
    include_model_details: bool = False
    include_hardware_details: bool = False
    analytics_options: HtmlAnalyticsReportOptions = field(default_factory=HtmlAnalyticsReportOptions)

    def __post_init__(self) -> None:
        try:
            normalized_kind: StandardExportKind | str = StandardExportKind(self.kind)
        except (TypeError, ValueError):
            normalized_kind = str(self.kind)
        object.__setattr__(self, "kind", normalized_kind)
        object.__setattr__(self, "destination", Path(self.destination))


@dataclass(frozen=True)
class StandardExportPreview:
    """Metadata-only preview; no rendered document is cached here."""

    status: StandardExportStatus
    kind: StandardExportKind | str
    destination: Path
    source_record_family: str = ""
    selected_scope: Mapping[str, str] = field(default_factory=dict)
    selected_options: Mapping[str, str] = field(default_factory=dict)
    record_count: int = 0
    empty_selection: bool = False
    summary: str = ""
    message: str = ""
    details: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_scope", _freeze_mapping(self.selected_scope))
        object.__setattr__(self, "selected_options", _freeze_mapping(self.selected_options))


@dataclass(frozen=True)
class StandardExportWriteResult:
    """Result of one staged, explicitly authorized export attempt."""

    status: StandardExportStatus
    kind: StandardExportKind | str
    destination: Path
    record_count: int = 0
    message: str = ""
    details: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status is StandardExportStatus.SUCCESS


@dataclass(frozen=True)
class _Selection:
    kind: StandardExportKind
    run_aggregates: tuple[Any, ...] = ()
    scoreboard_aggregates: tuple[Any, ...] = ()
    record_count: int = 0
    source_record_family: str = ""
    selected_scope: Mapping[str, str] = field(default_factory=dict)
    selected_options: Mapping[str, str] = field(default_factory=dict)


class StandardExportService:
    """Application-facing facade for the standard desktop export workflow."""

    def __init__(
        self,
        benchmarks: BenchmarkService,
        catalog: CatalogService | None = None,
        *,
        statistics: StatisticsService | None = None,
        trends: TrendService | None = None,
    ) -> None:
        self.benchmarks = benchmarks
        self.catalog = catalog or benchmarks.catalog
        self.reporting = ReportingService(benchmarks, self.catalog)
        self.statistics = statistics or StatisticsService(benchmarks, self.catalog)
        self.trends = trends or TrendService(benchmarks, self.catalog, self.statistics)

    @staticmethod
    def default_filename(kind: StandardExportKind | str) -> str:
        try:
            normalized = StandardExportKind(kind)
        except (TypeError, ValueError):
            return "export"
        return DEFAULT_EXPORT_FILENAMES.get(normalized, "export")

    @staticmethod
    def expected_extension(kind: StandardExportKind | str) -> str:
        try:
            normalized = StandardExportKind(kind)
        except (TypeError, ValueError):
            return ""
        return ".csv" if normalized in {StandardExportKind.BENCHMARK_RUNS_CSV, StandardExportKind.SCOREBOARD_CSV} else ".md" if normalized in {
            StandardExportKind.COMBINED_MARKDOWN,
            StandardExportKind.BENCHMARK_RUN_MARKDOWN,
            StandardExportKind.SCOREBOARD_MARKDOWN,
            StandardExportKind.MODEL_LEADERBOARD_MARKDOWN,
            StandardExportKind.SESSION_MARKDOWN,
            StandardExportKind.HARDWARE_MARKDOWN,
        } else ".html" if normalized in {StandardExportKind.SCOREBOARD_HTML, StandardExportKind.HTML_ANALYTICS} else ""

    @staticmethod
    def normalize_destination(destination: str | Path, kind: StandardExportKind | str) -> Path:
        """Normalize a destination without creating or modifying anything."""

        path = Path(destination).expanduser()
        try:
            path = path.resolve(strict=False)
        except (OSError, ValueError):
            path = Path(os.path.abspath(str(path)))
        if path.exists() and path.is_dir():
            return path
        extension = StandardExportService.expected_extension(kind)
        if extension and path.suffix.casefold() != extension:
            path = Path(f"{path}{extension}")
        return path

    def preview(self, request: StandardExportRequest) -> StandardExportPreview:
        """Resolve current records and return metadata without output writes."""

        kind, destination, error = self._validated_request(request)
        if kind is None:
            return self._preview_failure(request, destination, StandardExportStatus.INVALID_REQUEST, error)
        try:
            selection = self._select(request, kind)
        except (KeyError, OSError, ValueError) as error_value:
            return self._preview_failure(
                request,
                destination,
                StandardExportStatus.INVALID_REQUEST,
                str(error_value),
            )
        empty = selection.record_count == 0
        status = StandardExportStatus.EMPTY_SELECTION if empty else StandardExportStatus.SUCCESS
        summary = self._selection_summary(selection)
        return StandardExportPreview(
            status=status,
            kind=kind,
            destination=destination,
            source_record_family=selection.source_record_family,
            selected_scope=selection.selected_scope,
            selected_options=selection.selected_options,
            record_count=selection.record_count,
            empty_selection=empty,
            summary=summary,
            message="No records selected." if empty else "Preview ready. No files or settings were changed.",
        )

    def write(self, request: StandardExportRequest, *, overwrite: bool = False) -> StandardExportWriteResult:
        """Re-resolve the request and atomically write one standard export."""

        kind, destination, error = self._validated_request(request)
        if kind is None:
            return StandardExportWriteResult(
                StandardExportStatus.INVALID_REQUEST,
                request.kind,
                destination,
                message=error,
            )
        try:
            selection = self._select(request, kind)
        except (KeyError, OSError, ValueError) as error_value:
            return StandardExportWriteResult(
                StandardExportStatus.INVALID_REQUEST,
                kind,
                destination,
                message="The export request is no longer valid.",
                details=str(error_value),
            )
        if selection.record_count == 0:
            return StandardExportWriteResult(
                StandardExportStatus.EMPTY_SELECTION,
                kind,
                destination,
                message="No records selected.",
            )
        try:
            if kind is StandardExportKind.BENCHMARK_RUNS_CSV:
                content = render_benchmark_runs_csv(self.benchmarks, aggregates=selection.run_aggregates)
                return self._write_staged_text(kind, destination, selection.record_count, content, overwrite)
            if kind is StandardExportKind.SCOREBOARD_CSV:
                content = render_scoreboard_csv(self.catalog, aggregates=selection.scoreboard_aggregates)
                return self._write_staged_text(kind, destination, selection.record_count, content, overwrite)
            if kind is StandardExportKind.SCOREBOARD_HTML:
                return self._write_scoreboard_html(kind, destination, selection.record_count, overwrite)
            if kind is StandardExportKind.HTML_ANALYTICS:
                options = replace(
                    request.analytics_options,
                    output_destination=destination,
                    overwrite=overwrite,
                )
                report = self.reporting.html_analytics_report(
                    options=options,
                    runs=selection.run_aggregates,
                    entries=selection.scoreboard_aggregates,
                    statistics=self.statistics,
                    trends=self.trends,
                )
                result = self.reporting.write_html_analytics_report(report, destination, overwrite=overwrite)
                return self._from_report_result(kind, destination, selection.record_count, result)

            report = self._build_markdown_report(request, kind, selection)
            if isinstance(report, str):
                result = write_markdown_report(report, destination, overwrite=overwrite)
            else:
                result = self.reporting.write_markdown_report(
                    report,
                    destination,
                    overwrite=overwrite,
                    include_model_details=request.include_model_details,
                    include_hardware_details=request.include_hardware_details,
                )
            return self._from_report_result(kind, destination, selection.record_count, result)
        except (KeyError, OSError, ValueError) as error_value:
            return StandardExportWriteResult(
                StandardExportStatus.TEMPORARY_WRITE_FAILED,
                kind,
                destination,
                selection.record_count,
                message=f"Could not write {EXPORT_LABELS.get(kind, 'export')}.",
                details=str(error_value),
            )

    def _validated_request(
        self,
        request: StandardExportRequest,
    ) -> tuple[StandardExportKind | None, Path, str]:
        try:
            destination = self.normalize_destination(request.destination, request.kind)
        except (OSError, TypeError, ValueError, RuntimeError) as error:
            try:
                fallback = Path.cwd()
            except (OSError, RuntimeError):
                fallback = Path(".")
            return None, fallback, f"The export destination is invalid: {error}"
        try:
            kind = StandardExportKind(request.kind)
        except (TypeError, ValueError):
            return None, destination, "The selected export format is not supported."
        if kind is StandardExportKind.JSONL_TRAINING_DATA:
            return None, destination, "JSONL training data is available through the future Dataset Builder GUI."
        if kind not in SUPPORTED_STANDARD_EXPORT_KINDS:
            return None, destination, "The selected export format is not supported."
        try:
            if destination.exists() and destination.is_dir():
                return None, destination, "The export destination is a directory. Choose a file path."
            if not destination.parent.exists():
                return None, destination, "The destination folder does not exist. Choose an existing folder."
            if not destination.parent.is_dir():
                return None, destination, "The destination parent is not a directory."
        except (OSError, ValueError, RuntimeError) as error:
            return None, destination, f"The destination could not be inspected: {error}"
        for name, value in (
            ("run_id", request.run_id),
            ("session_id", request.session_id),
            ("hardware_profile_id", request.hardware_profile_id),
        ):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                return None, destination, f"{name} must be a positive integer."
        if kind is StandardExportKind.BENCHMARK_RUN_MARKDOWN and request.run_id is None:
            return None, destination, "Select one benchmark run."
        if kind is StandardExportKind.SESSION_MARKDOWN and request.session_id is None:
            return None, destination, "Select one session."
        if kind is StandardExportKind.HARDWARE_MARKDOWN and request.hardware_profile_id is None:
            return None, destination, "Select one hardware profile."
        if kind is StandardExportKind.SCOREBOARD_HTML and request.scoreboard_filters != ScoreboardReportFilters():
            return None, destination, "Scoreboard HTML does not support filters in this phase."
        return kind, destination, ""

    def _select(self, request: StandardExportRequest, kind: StandardExportKind) -> _Selection:
        if kind is StandardExportKind.HTML_ANALYTICS:
            return self._select_analytics(request, kind)
        runs: tuple[Any, ...] = ()
        entries: tuple[Any, ...] = ()
        if kind in {
            StandardExportKind.BENCHMARK_RUNS_CSV,
            StandardExportKind.COMBINED_MARKDOWN,
            StandardExportKind.BENCHMARK_RUN_MARKDOWN,
            StandardExportKind.MODEL_LEADERBOARD_MARKDOWN,
            StandardExportKind.SESSION_MARKDOWN,
            StandardExportKind.HARDWARE_MARKDOWN,
        }:
            runs = tuple(self.reporting.select_benchmark_runs(filters=self._benchmark_filters(request, kind)))
        if kind in {
            StandardExportKind.SCOREBOARD_CSV,
            StandardExportKind.COMBINED_MARKDOWN,
            StandardExportKind.SCOREBOARD_MARKDOWN,
            StandardExportKind.SCOREBOARD_HTML,
        }:
            entries = tuple(
                self.reporting.select_scoreboard_entries(filters=request.scoreboard_filters)
            )
        if kind is StandardExportKind.COMBINED_MARKDOWN:
            family = "BenchmarkRun + ScoreboardEntry"
        elif kind in {
            StandardExportKind.BENCHMARK_RUNS_CSV,
            StandardExportKind.BENCHMARK_RUN_MARKDOWN,
            StandardExportKind.MODEL_LEADERBOARD_MARKDOWN,
            StandardExportKind.SESSION_MARKDOWN,
            StandardExportKind.HARDWARE_MARKDOWN,
        }:
            family = "BenchmarkRun"
        else:
            family = "ScoreboardEntry"
        scope = self._scope(request, kind)
        options = self._options(request, kind)
        return _Selection(
            kind,
            runs,
            entries,
            len(runs) + len(entries),
            family,
            scope,
            options,
        )

    def _select_analytics(self, request: StandardExportRequest, kind: StandardExportKind) -> _Selection:
        options = request.analytics_options
        source = AnalyticsSourceFamily(options.source_family)
        runs: tuple[Any, ...] = ()
        entries: tuple[Any, ...] = ()
        if source in {AnalyticsSourceFamily.BENCHMARK_RUNS, AnalyticsSourceFamily.COMBINED}:
            runs = tuple(self.statistics.select_benchmark_runs(filters=options.benchmark_filters))
        if source in {AnalyticsSourceFamily.SCOREBOARD, AnalyticsSourceFamily.COMBINED}:
            entries = tuple(self.statistics.select_scoreboard_entries(filters=options.scoreboard_filters))
        return _Selection(
            kind,
            runs,
            entries,
            len(runs) + len(entries),
            "BenchmarkRun + ScoreboardEntry" if source is AnalyticsSourceFamily.COMBINED else (
                "BenchmarkRun" if source is AnalyticsSourceFamily.BENCHMARK_RUNS else "ScoreboardEntry"
            ),
            {"source": source.value},
            {"title": options.title, "trend_interval": str(options.trend_interval)},
        )

    @staticmethod
    def _benchmark_filters(request: StandardExportRequest, kind: StandardExportKind) -> BenchmarkReportFilters:
        filters = request.benchmark_filters
        if kind is StandardExportKind.BENCHMARK_RUN_MARKDOWN:
            assert request.run_id is not None
            return replace(filters, include_run_ids=frozenset({request.run_id}))
        if kind is StandardExportKind.SESSION_MARKDOWN:
            assert request.session_id is not None
            return replace(filters, session_id=request.session_id)
        if kind is StandardExportKind.HARDWARE_MARKDOWN:
            assert request.hardware_profile_id is not None
            return replace(filters, hardware_profile_id=request.hardware_profile_id)
        return filters

    @staticmethod
    def _scope(request: StandardExportRequest, kind: StandardExportKind) -> Mapping[str, str]:
        values: dict[str, str] = {}
        if request.run_id is not None:
            values["run_id"] = str(request.run_id)
        if request.session_id is not None:
            values["session_id"] = str(request.session_id)
        if request.hardware_profile_id is not None:
            values["hardware_profile_id"] = str(request.hardware_profile_id)
        if request.benchmark_filters.model:
            values["model"] = request.benchmark_filters.model
        if request.scoreboard_filters.model:
            values["scoreboard_model"] = request.scoreboard_filters.model
        if request.scoreboard_filters.batch_id is not None:
            values["batch_id"] = str(request.scoreboard_filters.batch_id)
        values["kind"] = kind.value
        return values

    @staticmethod
    def _options(request: StandardExportRequest, kind: StandardExportKind) -> Mapping[str, str]:
        values = {"kind": kind.value}
        if request.include_prompt_text:
            values["include_prompt_text"] = "true"
        if request.include_raw_model_output:
            values["include_raw_model_output"] = "true"
        if request.include_attachment_metadata:
            values["include_attachment_metadata"] = "true"
        if request.include_model_details:
            values["include_model_details"] = "true"
        if request.include_hardware_details:
            values["include_hardware_details"] = "true"
        return values

    @staticmethod
    def _selection_summary(selection: _Selection) -> str:
        if selection.kind is StandardExportKind.COMBINED_MARKDOWN:
            return f"{len(selection.run_aggregates)} benchmark run(s) and {len(selection.scoreboard_aggregates)} scoreboard entr{'y' if len(selection.scoreboard_aggregates) == 1 else 'ies'} selected."
        label = "record" if selection.record_count == 1 else "records"
        return f"{selection.record_count} {selection.source_record_family} {label} selected."

    @staticmethod
    def _preview_failure(
        request: StandardExportRequest,
        destination: Path,
        status: StandardExportStatus,
        message: str,
    ) -> StandardExportPreview:
        return StandardExportPreview(
            status=status,
            kind=request.kind,
            destination=destination,
            empty_selection=False,
            summary="",
            message=message,
        )

    def _build_markdown_report(
        self,
        request: StandardExportRequest,
        kind: StandardExportKind,
        selection: _Selection,
    ) -> Any:
        filters = self._benchmark_filters(request, kind)
        if kind is StandardExportKind.COMBINED_MARKDOWN:
            benchmark = self.reporting.benchmark_run_report(
                runs=selection.run_aggregates,
                filters=filters,
                include_prompt_text=request.include_prompt_text,
                include_raw_model_output=request.include_raw_model_output,
                include_attachment_metadata=request.include_attachment_metadata,
            )
            scoreboard = self.reporting.scoreboard_report(
                entries=selection.scoreboard_aggregates,
                filters=request.scoreboard_filters,
            )
            return render_combined_markdown(benchmark, scoreboard)
        if kind is StandardExportKind.BENCHMARK_RUN_MARKDOWN:
            return self.reporting.benchmark_run_report(
                runs=selection.run_aggregates,
                filters=filters,
                include_prompt_text=request.include_prompt_text,
                include_raw_model_output=request.include_raw_model_output,
                include_attachment_metadata=request.include_attachment_metadata,
            )
        if kind is StandardExportKind.SCOREBOARD_MARKDOWN:
            return self.reporting.scoreboard_report(
                entries=selection.scoreboard_aggregates,
                filters=request.scoreboard_filters,
            )
        if kind is StandardExportKind.MODEL_LEADERBOARD_MARKDOWN:
            return self.reporting.model_leaderboard(
                runs=selection.run_aggregates,
                filters=request.benchmark_filters,
            )
        if kind is StandardExportKind.SESSION_MARKDOWN:
            return self.reporting.session_report(
                session_id=request.session_id,
                runs=selection.run_aggregates,
                filters=filters,
                include_prompt_text=request.include_prompt_text,
                include_raw_model_output=request.include_raw_model_output,
                include_attachment_metadata=request.include_attachment_metadata,
            )
        if kind is StandardExportKind.HARDWARE_MARKDOWN:
            return self.reporting.hardware_report(
                runs=selection.run_aggregates,
                filters=filters,
                include_prompt_text=request.include_prompt_text,
                include_raw_model_output=request.include_raw_model_output,
                include_attachment_metadata=request.include_attachment_metadata,
                include_hardware_details=request.include_hardware_details,
            )
        raise ValueError("The selected export kind is not a Markdown report.")

    def _write_scoreboard_html(
        self,
        kind: StandardExportKind,
        destination: Path,
        record_count: int,
        overwrite: bool,
    ) -> StandardExportWriteResult:
        if destination.exists() and not overwrite:
            return StandardExportWriteResult(
                StandardExportStatus.OVERWRITE_REQUIRED,
                kind,
                destination,
                record_count,
                message="Existing Scoreboard HTML requires explicit overwrite confirmation.",
            )
        temporary: Path | None = None
        descriptor: int | None = None
        finalizing = False
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
            )
            temporary = Path(temporary_name)
            os.close(descriptor)
            descriptor = None
            export_scoreboard_html(self.catalog, temporary)
            finalizing = True
            os.replace(temporary, destination)
            temporary = None
            return StandardExportWriteResult(
                StandardExportStatus.SUCCESS,
                kind,
                destination,
                record_count,
                message="Scoreboard HTML written successfully.",
            )
        except OSError as error:
            return StandardExportWriteResult(
                StandardExportStatus.FINALIZATION_FAILED if finalizing else StandardExportStatus.TEMPORARY_WRITE_FAILED,
                kind,
                destination,
                record_count,
                message="Could not write Scoreboard HTML.",
                details=f"{type(error).__name__}: {error}",
            )
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _write_staged_text(
        kind: StandardExportKind,
        destination: Path,
        record_count: int,
        content: str,
        overwrite: bool,
    ) -> StandardExportWriteResult:
        if destination.exists() and not overwrite:
            return StandardExportWriteResult(
                StandardExportStatus.OVERWRITE_REQUIRED,
                kind,
                destination,
                record_count,
                message=f"Existing {EXPORT_LABELS.get(kind, 'export')} requires explicit overwrite confirmation.",
            )
        temporary: Path | None = None
        descriptor: int | None = None
        finalizing = False
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
                descriptor = None
                output.write(content)
            finalizing = True
            os.replace(temporary, destination)
            temporary = None
            return StandardExportWriteResult(
                StandardExportStatus.SUCCESS,
                kind,
                destination,
                record_count,
                message=f"{EXPORT_LABELS.get(kind, 'Export')} written successfully.",
            )
        except OSError as error:
            return StandardExportWriteResult(
                StandardExportStatus.FINALIZATION_FAILED if finalizing else StandardExportStatus.TEMPORARY_WRITE_FAILED,
                kind,
                destination,
                record_count,
                message=f"Could not write {EXPORT_LABELS.get(kind, 'export')}.",
                details=f"{type(error).__name__}: {error}",
            )
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _from_report_result(
        kind: StandardExportKind,
        destination: Path,
        record_count: int,
        result: Any,
    ) -> StandardExportWriteResult:
        if result.status is ReportWriteStatus.SUCCESS:
            status = StandardExportStatus.SUCCESS
        elif result.status is ReportWriteStatus.OVERWRITE_REQUIRED:
            status = StandardExportStatus.OVERWRITE_REQUIRED
        elif result.status is ReportWriteStatus.FINALIZE_FAILED:
            status = StandardExportStatus.FINALIZATION_FAILED
        else:
            status = StandardExportStatus.TEMPORARY_WRITE_FAILED
        return StandardExportWriteResult(
            status,
            kind,
            destination,
            record_count,
            message=result.message,
            details=result.details,
        )


__all__ = (
    "DEFAULT_EXPORT_FILENAMES",
    "EXPORT_LABELS",
    "SUPPORTED_STANDARD_EXPORT_KINDS",
    "StandardExportKind",
    "StandardExportPreview",
    "StandardExportRequest",
    "StandardExportService",
    "StandardExportStatus",
    "StandardExportWriteResult",
)
