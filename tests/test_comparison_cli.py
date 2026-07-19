from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cli import ModelComparisonOptions, TerminalApp
from engine.comparisons import ComparisonService
from engine.database import EngineDatabase
from engine.domain import BenchmarkRun, BenchmarkSession, ReviewScore
from engine.reporting import BenchmarkRunAggregate
from engine.services import BenchmarkService, CatalogService


class InputQueue:
    def __init__(self, values: list[str]) -> None:
        self.values = iter(values)

    def __call__(self, _prompt: str = "") -> str:
        return next(self.values)


class ComparisonCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "cli.db"
        self.outputs: list[str] = []

    def tearDown(self) -> None:
        self.directory.cleanup()

    def app(self, inputs: list[str]) -> TerminalApp:
        return TerminalApp(self.database_path, input_fn=InputQueue(inputs), output_fn=self.outputs.append)

    @staticmethod
    def runs() -> tuple[BenchmarkRunAggregate, ...]:
        first = BenchmarkSession("First", id=1)
        second = BenchmarkSession("Second", id=2)
        values = []
        for run_id, model, session, score in (
            (1, "Alpha", first, 4.0),
            (2, "Beta", second, 3.0),
        ):
            run = BenchmarkRun(
                id=run_id,
                raw_model_output="raw output",
                session_id=session.id,
                model_snapshot={"model_name": model, "tokens_per_second": 100.0},
                benchmark_snapshot={"name": "Shared", "benchmark_type": "code_review"},
                hardware_snapshot={"name": "Rig"},
            )
            values.append(BenchmarkRunAggregate(run, ReviewScore(run_id=run_id, overall_score=score), session))
        return tuple(values)

    def test_main_menu_exposes_comparisons_and_multiselect_uses_displayed_numbers(self) -> None:
        app = self.app(["1", "2", "D"])
        app.show_main_menu()
        selected = app._comparison_multi_select(
            "Select Models",
            (("Alpha", "Alpha"), ("Beta", "Beta")),
            (),
        )
        self.assertEqual(selected, ("Alpha", "Beta"))
        combined = "\n".join(self.outputs)
        self.assertIn("18) Comparisons", combined)
        self.assertIn("Select Models", combined)

    def test_model_comparison_screen_consumes_structured_result_for_preview(self) -> None:
        app = self.app(["4", "B", "B"])
        backend = ComparisonService(app.benchmarks, app.catalog)
        result = backend.compare_models(("Alpha", "Beta"), self.runs(), generated_at="fixed")
        fake = Mock()
        fake.compare_models.return_value = result
        app.comparisons = fake

        returned = app._model_comparison_screen(ModelComparisonOptions(models=("Alpha", "Beta")))

        self.assertEqual(returned.models, ("Alpha", "Beta"))
        fake.compare_models.assert_called_once()
        combined = "\n".join(self.outputs)
        self.assertIn("Alpha | 1 | 1 | 4", combined)
        self.assertIn("Pairwise deltas: second selected model minus first selected model", combined)

    def test_comparison_menu_options_are_retained_in_memory(self) -> None:
        app = self.app(["3", "B", "B"])
        app.comparisons_screen()
        combined = "\n".join(self.outputs)
        self.assertIn("Current Comparison Options", combined)
        self.assertIn("All non-deleted BenchmarkRun snapshots", combined)


if __name__ == "__main__":
    unittest.main()
