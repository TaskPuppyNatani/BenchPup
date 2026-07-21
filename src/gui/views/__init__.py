"""Presentation pages for the BenchPup desktop shell."""

from .add_run import AddRunWizard, OptionalScoreField
from .dashboard import DashboardTableModel, DashboardView
from .dataset_builder import DatasetBuilderState, DatasetBuilderView
from .exports import ExportsView
from .hardware_profiles import HardwareProfilesView
from .imports import ImportsView
from .placeholder import PlaceholderPage
from .prompt_templates import PromptTemplatesView
from .run_details import RunDetailsDialog
from .runs import RunFilterProxyModel, RunsView

__all__ = (
    "AddRunWizard",
    "DashboardTableModel",
    "DashboardView",
    "DatasetBuilderState",
    "DatasetBuilderView",
    "ExportsView",
    "HardwareProfilesView",
    "ImportsView",
    "OptionalScoreField",
    "PlaceholderPage",
    "PromptTemplatesView",
    "RunDetailsDialog",
    "RunFilterProxyModel",
    "RunsView",
)
