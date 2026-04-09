from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import FabricationDatabase
from main_window import MainWindow


def _make_fixture_root() -> Path:
    base = ROOT / "_runtime" / "test_tmp" / uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    return base


class _AutosizeTrackingMainWindow(MainWindow):
    def __init__(self, *args, **kwargs):
        self.queue_calls = 0
        super().__init__(*args, **kwargs)

    def refresh_view(self) -> None:
        # Keep the test focused on UI autosize behavior without needing seeded gantt data.
        self._trucks = []
        self._kit_index = {}
        self._schedule_insights = None
        self._dashboard_metrics = None
        self._kit_stage_windows_by_truck = {}

    def _queue_gantt_pane_autosize(self) -> None:
        self.queue_calls += 1


class MainWindowGanttAutosizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.root = _make_fixture_root()
        self.database = FabricationDatabase(self.root / "fabrication_flow.db")
        self.database.initialize()
        self.window = _AutosizeTrackingMainWindow(self.database, runtime_dir=self.root)
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        self.app.processEvents()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_resize_event_requeues_gantt_autosize(self) -> None:
        baseline_calls = self.window.queue_calls
        self.window.resize(self.window.width() + 120, self.window.height() + 80)
        self.app.processEvents()
        self.assertGreater(self.window.queue_calls, baseline_calls)

    def test_flow_tab_change_requeues_gantt_autosize(self) -> None:
        baseline_calls = self.window.queue_calls
        self.window._flow_tabs.setCurrentIndex(1)
        self.app.processEvents()
        self.window._flow_tabs.setCurrentIndex(0)
        self.app.processEvents()
        self.assertGreater(self.window.queue_calls, baseline_calls)


if __name__ == "__main__":
    unittest.main()
