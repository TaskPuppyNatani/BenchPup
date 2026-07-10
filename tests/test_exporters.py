import json, sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from exporters import export_csv, export_jsonl
from models import BenchmarkRun

class ExporterTests(unittest.TestCase):
    def test_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            runs = [BenchmarkRun(model_name="Test", overall_score=4)]
            export_csv(runs, path / "x.csv")
            export_jsonl(runs, path / "x.jsonl")
            self.assertIn("model_name", (path / "x.csv").read_text())
            self.assertEqual(json.loads((path / "x.jsonl").read_text())["input"]["model"], "Test")
