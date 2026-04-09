from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import FabricationDatabase
from gantt_overlay import build_overlay_rows
from models import PRIMARY_FLOW_KIT_NAMES, SECONDARY_FLOW_KIT_NAMES
from schedule import build_schedule_insights


def _make_fixture_root() -> Path:
    base = ROOT / "_runtime" / "test_tmp" / uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    return base


class GanttOverlayKitFilteringTests(unittest.TestCase):
    def test_build_overlay_rows_excludes_small_kits_when_requested(self) -> None:
        root = _make_fixture_root()
        try:
            database = FabricationDatabase(root / "fabrication_flow.db")
            database.initialize()
            truck_id = database.create_truck("F71001", planned_start_date="2026-04-08")

            truck = database.load_truck_with_kits(truck_id, active_only=False)
            assert truck is not None
            trucks = [truck]
            schedule_insights = build_schedule_insights(trucks)

            rows = build_overlay_rows(
                trucks=trucks,
                schedule_insights=schedule_insights,
                max_rows=50,
                include_small_kits=False,
            )

            row_labels = [str(row.row_label) for row in rows]
            for kit_name in PRIMARY_FLOW_KIT_NAMES:
                self.assertTrue(
                    any(f"| {kit_name}" in row_label for row_label in row_labels),
                    msg=f"Expected primary kit {kit_name} to remain on the gantt.",
                )
            for kit_name in SECONDARY_FLOW_KIT_NAMES:
                self.assertFalse(
                    any(f"| {kit_name}" in row_label for row_label in row_labels),
                    msg=f"Did not expect small kit {kit_name} on the gantt.",
                )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_build_overlay_rows_still_includes_small_kits_by_default(self) -> None:
        root = _make_fixture_root()
        try:
            database = FabricationDatabase(root / "fabrication_flow.db")
            database.initialize()
            truck_id = database.create_truck("F71002", planned_start_date="2026-04-08")

            truck = database.load_truck_with_kits(truck_id, active_only=False)
            assert truck is not None
            trucks = [truck]
            schedule_insights = build_schedule_insights(trucks)

            rows = build_overlay_rows(
                trucks=trucks,
                schedule_insights=schedule_insights,
                max_rows=50,
            )

            row_labels = [str(row.row_label) for row in rows]
            self.assertTrue(
                any(f"| {SECONDARY_FLOW_KIT_NAMES[0]}" in row_label for row_label in row_labels),
                msg="Expected default overlay behavior to remain unchanged for non-gantt callers.",
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
