"""Presentation pages for the BenchPup desktop shell."""

from .add_run import AddRunWizard, OptionalScoreField
from .dashboard import DashboardTableModel, DashboardView
from .placeholder import PlaceholderPage
from .run_details import RunDetailsDialog
from .runs import RunFilterProxyModel, RunsView

__all__ = (
    "AddRunWizard",
    "DashboardTableModel",
    "DashboardView",
    "OptionalScoreField",
    "PlaceholderPage",
    "RunDetailsDialog",
    "RunFilterProxyModel",
    "RunsView",
)
