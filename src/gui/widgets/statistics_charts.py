"""Native QtCharts widgets for source-separated dashboard statistics.

The widgets in this module deliberately accept already-computed values from
the engine statistics service.  They own chart presentation only: no records
are selected here and no averages or distributions are calculated here.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Sequence

from PySide6.QtCharts import QAbstractBarSeries, QBarCategoryAxis, QBarSeries, QBarSet, QChart, QChartView, QValueAxis
from PySide6.QtCore import QMargins, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..theme import DEFAULT_THEME, ThemeTokens


ChartValue = tuple[str, float | None]


def format_chart_value(value: float) -> str:
    """Format a finite chart value without scientific notation or false zeros."""

    if not math.isfinite(float(value)):
        return "Unavailable"
    try:
        text = format(Decimal(str(float(value))), "f")
    except (InvalidOperation, ValueError):
        text = format(float(value), "f")
    trimmed = text.rstrip("0").rstrip(".") if "." in text else text
    return "0" if trimmed in {"", "-0"} else trimmed


class StatisticsChartPanel(QFrame):
    """A reusable one-series bar chart with explicit unavailable states."""

    def __init__(
        self,
        title: str,
        *,
        parent: QWidget | None = None,
        tokens: ThemeTokens = DEFAULT_THEME,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("statisticsChartPanel")
        self.setAccessibleName(title)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(300)
        self._title = title
        self._tokens = tokens
        self._labels: tuple[str, ...] = ()
        self._has_data = False
        self._base_font = QFont(self.font())

        self.chart = QChart()
        self.chart.setObjectName("statisticsChart")
        self.chart.setTitle(title)
        self.chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)
        self.chart.setBackgroundBrush(QBrush(QColor(tokens.surface)))
        self.chart.setPlotAreaBackgroundBrush(QBrush(QColor(tokens.surface)))
        self.chart.setPlotAreaBackgroundVisible(True)
        self.chart.setTitleBrush(QBrush(QColor(tokens.text)))
        self.chart.setTitleFont(self._font(11, QFont.Weight.DemiBold))
        self.chart.setMargins(QMargins(14, 12, 14, 24))
        self.chart.legend().setVisible(False)

        self.chart_view = QChartView(self.chart, self)
        self.chart_view.setObjectName("statisticsChartView")
        self.chart_view.setAccessibleName(title)
        self.chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.chart_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.chart_view.setMinimumHeight(260)

        self.placeholder = QLabel()
        self.placeholder.setObjectName("chartPlaceholder")
        self.placeholder.setAccessibleName(f"{title} status")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setMinimumHeight(260)
        self.placeholder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.status = QLabel()
        self.status.setObjectName("chartStatus")
        self.status.setWordWrap(True)
        self.status.setAccessibleName(f"{title} availability")
        self.status.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        layout.addWidget(self.chart_view, 1)
        layout.addWidget(self.placeholder, 1)
        layout.addWidget(self.status)

        self._set_palette()
        self.set_empty(f"{title} is unavailable.")

    @property
    def labels(self) -> tuple[str, ...]:
        """Return the complete category labels currently bound to the chart."""

        return self._labels

    @property
    def has_data(self) -> bool:
        return self._has_data

    def set_bars(
        self,
        values: Sequence[ChartValue],
        *,
        value_title: str,
        empty_message: str,
        color: str | None = None,
        unavailable_count: int = 0,
        integer_axis: bool = False,
    ) -> None:
        """Bind typed values to the existing chart objects.

        ``None`` values are omitted rather than represented as zero.  The
        caller supplies values already selected and summarized by the engine.
        """

        available = tuple(
            (str(label), float(value))
            for label, value in values
            if value is not None and math.isfinite(float(value))
        )
        self._clear_chart()
        self.chart.setTitle(self._title)
        self._labels = tuple(label for label, _value in available)
        if not available:
            self.set_empty(empty_message)
            return

        bar_set = QBarSet(value_title)
        bar_set.append([value for _label, value in available])
        bar_set.setColor(QColor(color or self._tokens.accent))
        bar_set.setLabelColor(QColor(self._tokens.text))
        bar_set.setLabelBrush(QBrush(QColor(self._tokens.text)))
        bar_set.setLabelFont(self._font(10))

        series = QBarSeries()
        series.append(bar_set)
        series.setLabelsVisible(True)
        series.setLabelsPosition(QAbstractBarSeries.LabelsPosition.LabelsOutsideEnd)
        series.setLabelsFormat("@value")
        # QtCharts uses significant digits for @value.  A generous precision
        # keeps ordinary values in decimal notation without changing them.
        series.setLabelsPrecision(12)
        self.chart.addSeries(series)

        category_axis = QBarCategoryAxis()
        category_axis.setCategories(list(self._labels))
        category_axis.setLabelsAngle(-35)
        category_axis.setTruncateLabels(False)
        category_axis.setLabelsColor(QColor(self._tokens.text))
        category_axis.setLabelsBrush(QBrush(QColor(self._tokens.text)))
        category_axis.setLabelsFont(self._font(9))
        category_axis.setGridLineColor(QColor(self._tokens.border))
        category_axis.setLinePen(QPen(QColor(self._tokens.border)))
        category_axis.setTitleBrush(QBrush(QColor(self._tokens.muted_text)))

        value_axis = QValueAxis()
        value_axis.setLabelsColor(QColor(self._tokens.text))
        value_axis.setLabelsBrush(QBrush(QColor(self._tokens.text)))
        value_axis.setLabelsFont(self._font(9))
        value_axis.setGridLineColor(QColor(self._tokens.border))
        value_axis.setLinePen(QPen(QColor(self._tokens.border)))
        value_axis.setTitleText(value_title)
        value_axis.setTitleBrush(QBrush(QColor(self._tokens.muted_text)))
        value_axis.setTitleFont(self._font(9, QFont.Weight.DemiBold))
        value_axis.setLabelFormat("%d" if integer_axis else "%.1f")
        maximum = max(value for _label, value in available)
        value_axis.setRange(0.0, max(1.0, maximum * 1.15))

        self.chart.addAxis(category_axis, Qt.AlignmentFlag.AlignBottom)
        self.chart.addAxis(value_axis, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(category_axis)
        series.attachAxis(value_axis)
        formatted_values = ", ".join(
            f"{label}: {format_chart_value(value)}" for label, value in available
        )
        self.chart_view.setToolTip(formatted_values)
        self.placeholder.setVisible(False)
        self.chart_view.setVisible(True)
        self._has_data = True
        if unavailable_count > 0:
            self.status.setText(f"{unavailable_count} value(s) unavailable.")
            self.status.setVisible(True)
        else:
            self.status.clear()
            self.status.setVisible(False)

    def set_distribution(
        self,
        counts: Sequence[ChartValue],
        *,
        empty_message: str,
        unavailable_count: int = 0,
    ) -> None:
        """Bind an engine-owned categorical distribution to the chart."""

        self.set_bars(
            counts,
            value_title="Records",
            empty_message=empty_message,
            unavailable_count=unavailable_count,
            integer_axis=True,
        )

    def set_empty(self, message: str) -> None:
        """Show a direct, accessible message without empty chart axes."""

        self._clear_chart()
        self._labels = ()
        self.chart.setTitle(self._title)
        self.placeholder.setText(message)
        self.placeholder.setVisible(True)
        self.chart_view.setVisible(False)
        self._has_data = False
        self.status.clear()
        self.status.setVisible(False)

    def _clear_chart(self) -> None:
        self.chart.removeAllSeries()
        for axis in tuple(self.chart.axes()):
            self.chart.removeAxis(axis)
        self.chart_view.setToolTip("")

    def _set_palette(self) -> None:
        palette = self.chart_view.palette()
        palette.setColor(palette.ColorRole.Window, QColor(self._tokens.surface))
        palette.setColor(palette.ColorRole.Base, QColor(self._tokens.surface))
        self.chart_view.setPalette(palette)

    def _font(self, point_size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
        font = QFont(self._base_font)
        font.setPointSize(point_size)
        font.setWeight(weight)
        return font


__all__ = ("ChartValue", "StatisticsChartPanel", "format_chart_value")
