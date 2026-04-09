from __future__ import annotations

import shutil
import sqlite3
import sys
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import FabricationDatabase
from models import SECONDARY_FLOW_KIT_NAMES


def _make_fixture_root() -> Path:
    base = ROOT / "_runtime" / "test_tmp" / uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    return base


class FabricationDatabaseDefaultKitTests(unittest.TestCase):
    def test_create_truck_seeds_primary_and_secondary_default_kits(self) -> None:
        root = _make_fixture_root()
        try:
            database = FabricationDatabase(root / "fabrication_flow.db")
            database.initialize()

            truck_id = database.create_truck("F70001", planned_start_date="2026-04-08")
            truck = database.load_truck_with_kits(truck_id, active_only=False)

            self.assertIsNotNone(truck)
            assert truck is not None
            kit_names = [kit.kit_name for kit in truck.kits]
            for kit_name in ("Body", "Pumphouse", "Console", "Interior", "Exterior"):
                self.assertIn(kit_name, kit_names)
            for kit_name in SECONDARY_FLOW_KIT_NAMES:
                self.assertIn(kit_name, kit_names)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_initialize_backfills_missing_secondary_templates_and_truck_kits(self) -> None:
        root = _make_fixture_root()
        try:
            db_path = root / "fabrication_flow.db"
            database = FabricationDatabase(db_path)
            database.initialize()
            truck_id = database.create_truck("F70002", planned_start_date="2026-04-08")

            with sqlite3.connect(str(db_path)) as connection:
                placeholders = ", ".join("?" for _ in SECONDARY_FLOW_KIT_NAMES)
                connection.execute(
                    f"DELETE FROM TruckKit WHERE kit_name IN ({placeholders})",
                    tuple(SECONDARY_FLOW_KIT_NAMES),
                )
                connection.execute(
                    f"DELETE FROM KitTemplate WHERE kit_name IN ({placeholders})",
                    tuple(SECONDARY_FLOW_KIT_NAMES),
                )
                connection.commit()

            database.initialize()
            truck = database.load_truck_with_kits(truck_id, active_only=False)

            self.assertIsNotNone(truck)
            assert truck is not None
            kit_names = [kit.kit_name for kit in truck.kits]
            for kit_name in SECONDARY_FLOW_KIT_NAMES:
                self.assertIn(kit_name, kit_names)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
