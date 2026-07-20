"""Shared dependencies and lifecycle for one BenchPup GUI process."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:  # Support both ``python -m src.gui`` and test imports with ``src`` on PATH.
    from ..engine import (
        BenchmarkService,
        CatalogService,
        ComparisonService,
        EngineDatabase,
        ReportingService,
        StatisticsService,
        TrendService,
    )
    from ..engine.importers import CsvImportService
    from ..engine.hardware_importers import HardwareImporterRegistry
    from ..engine.settings import DefaultWorkingDirectorySettings
except ImportError:  # pragma: no cover - exercised by the top-level test import path.
    from engine import (  # type: ignore[no-redef]
        BenchmarkService,
        CatalogService,
        ComparisonService,
        EngineDatabase,
        ReportingService,
        StatisticsService,
        TrendService,
    )
    from engine.importers import CsvImportService  # type: ignore[no-redef]
    from engine.hardware_importers import HardwareImporterRegistry  # type: ignore[no-redef]
    from engine.settings import DefaultWorkingDirectorySettings  # type: ignore[no-redef]


DEFAULT_APP_VERSION = "0.4.1-Alpha"


@dataclass(frozen=True)
class GuiPaths:
    """Resolved project paths shared by the GUI application shell."""

    project_root: Path
    database_path: Path
    log_path: Path


def resolve_paths(
    project_root: str | Path | None = None,
    database_path: str | Path | None = None,
) -> GuiPaths:
    """Resolve the same default database location used by ``src/main.py``."""

    explicit_database = Path(database_path).expanduser() if database_path is not None else None
    if project_root is None:
        if explicit_database is not None and explicit_database.is_absolute():
            inferred_root = explicit_database.parent.parent if explicit_database.parent.name.lower() == "data" else explicit_database.parent
            root = inferred_root.resolve()
        else:
            root = Path(__file__).resolve().parents[2]
    else:
        root = Path(project_root).expanduser().resolve()

    resolved_database = explicit_database or (root / "data" / "benchmark.db")
    if not resolved_database.is_absolute():
        resolved_database = root / resolved_database
    resolved_database = resolved_database.resolve()
    return GuiPaths(
        project_root=root,
        database_path=resolved_database,
        log_path=(root / "logs" / "error.log").resolve(),
    )


def create_gui_logger(log_path: str | Path) -> logging.Logger:
    """Create the GUI logger using the existing ``logs/error.log`` convention."""

    path = Path(log_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("benchpup.gui")
    logger.setLevel(logging.ERROR)
    logger.propagate = False

    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve() == path:
            return logger

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.ERROR)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def close_gui_logger(logger: logging.Logger, log_path: str | Path) -> None:
    """Release the file handler owned for one GUI context."""

    if not isinstance(logger, logging.Logger):
        return
    path = Path(log_path).resolve()
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve() == path:
            logger.removeHandler(handler)
            handler.close()


@dataclass
class GuiApplicationContext:
    """One explicitly-owned set of engine services for the GUI process."""

    paths: GuiPaths
    database: EngineDatabase
    catalog: CatalogService
    benchmarks: BenchmarkService
    reporting: ReportingService
    statistics: StatisticsService
    comparisons: ComparisonService
    trends: TrendService
    csv_importer: CsvImportService
    hardware_importers: HardwareImporterRegistry
    settings: DefaultWorkingDirectorySettings
    default_working_directory: Path | None
    version: str
    logger: logging.Logger
    _closed: bool = False

    @classmethod
    def create(
        cls,
        *,
        project_root: str | Path | None = None,
        database_path: str | Path | None = None,
        version: str = DEFAULT_APP_VERSION,
        logger: logging.Logger | None = None,
        logger_factory: Callable[[Path], logging.Logger] = create_gui_logger,
    ) -> GuiApplicationContext:
        """Migrate the established database and construct each service once."""

        paths = resolve_paths(project_root, database_path)
        active_logger = logger or logger_factory(paths.log_path)
        database = EngineDatabase(paths.database_path)
        database.migrate()
        catalog = CatalogService(database)
        benchmarks = BenchmarkService(database, catalog)
        reporting = ReportingService(benchmarks, catalog)
        statistics = StatisticsService(benchmarks, catalog)
        comparisons = ComparisonService(benchmarks, catalog)
        trends = TrendService(benchmarks, catalog)
        csv_importer = CsvImportService(benchmarks)
        hardware_importers = HardwareImporterRegistry()
        settings = DefaultWorkingDirectorySettings(paths.database_path)
        return cls(
            paths=paths,
            database=database,
            catalog=catalog,
            benchmarks=benchmarks,
            reporting=reporting,
            statistics=statistics,
            comparisons=comparisons,
            trends=trends,
            csv_importer=csv_importer,
            hardware_importers=hardware_importers,
            settings=settings,
            default_working_directory=settings.get_default_working_directory(),
            version=version,
            logger=active_logger,
        )

    def close(self) -> None:
        """Close the engine boundary exactly once during GUI shutdown."""

        if self._closed:
            return
        self.database.close()
        close_gui_logger(self.logger, self.paths.log_path)
        self._closed = True
