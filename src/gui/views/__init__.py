"""Presentation pages for the BenchPup desktop shell."""

from .add_run import AddRunWizard, OptionalScoreField
from .dashboard import DashboardTableModel, DashboardView
from .hardware_profiles import HardwareProfilesView
from .placeholder import PlaceholderPage
from .prompt_templates import PromptTemplatesView
from .run_details import RunDetailsDialog
from .runs import RunFilterProxyModel, RunsView

__all__ = (
    "AddRunWizard",
    "DashboardTableModel",
    "DashboardView",
    "HardwareProfilesView",
    "OptionalScoreField",
    "PlaceholderPage",
    "PromptTemplatesView",
    "RunDetailsDialog",
    "RunFilterProxyModel",
    "RunsView",
)
