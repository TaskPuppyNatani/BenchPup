import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from db import Database
from models import BenchmarkRun

class DatabaseTests(unittest.TestCase):
    def test_save_list_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "benchmarks.db"); db.initialize()
            run = BenchmarkRun(model_name="Test", overall_score=4)
            self.assertEqual(db.add_run(run), 1)
            self.assertIsNone(db.add_run(run, skip_duplicates=True))
            self.assertEqual(db.list_runs()[0].model_name, "Test")
