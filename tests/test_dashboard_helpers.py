from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard_helpers import completing_kit_would_finish_truck, filter_dashboard_trucks
from models import Truck, TruckKit
from stages import Stage


def _kit(*, kit_id: int, name: str, stage: Stage, is_active: bool = True) -> TruckKit:
    return TruckKit(
        id=kit_id,
        truck_id=1,
        kit_template_id=None,
        parent_kit_id=None,
        kit_name=name,
        kit_order=kit_id,
        is_main_kit=(kit_id == 1),
        front_stage_id=int(stage),
        back_stage_id=int(stage),
        is_active=is_active,
    )


class DashboardHelpersTests(unittest.TestCase):
    def test_filter_dashboard_trucks_hides_completed_by_default(self) -> None:
        active_truck = Truck(id=1, truck_number="F70001", kits=[_kit(kit_id=1, name="Body", stage=Stage.WELD)])
        completed_truck = Truck(id=2, truck_number="F70002", kits=[_kit(kit_id=2, name="Body", stage=Stage.COMPLETE)])

        filtered = filter_dashboard_trucks([completed_truck, active_truck], include_completed=False)

        self.assertEqual([truck.truck_number for truck in filtered], ["F70001"])

    def test_filter_dashboard_trucks_can_include_completed(self) -> None:
        active_truck = Truck(id=1, truck_number="F70001", kits=[_kit(kit_id=1, name="Body", stage=Stage.WELD)])
        completed_truck = Truck(id=2, truck_number="F70002", kits=[_kit(kit_id=2, name="Body", stage=Stage.COMPLETE)])

        filtered = filter_dashboard_trucks([completed_truck, active_truck], include_completed=True)

        self.assertEqual([truck.truck_number for truck in filtered], ["F70001", "F70002"])

    def test_completing_kit_would_finish_truck_when_last_active_kit_moves_to_complete(self) -> None:
        truck = Truck(
            id=1,
            truck_number="F70003",
            kits=[
                _kit(kit_id=1, name="Body", stage=Stage.COMPLETE),
                _kit(kit_id=2, name="Exterior", stage=Stage.WELD),
                _kit(kit_id=3, name="Pump Mounts", stage=Stage.COMPLETE, is_active=False),
            ],
        )

        self.assertTrue(
            completing_kit_would_finish_truck(
                truck,
                kit_id=2,
                target_stage_id=int(Stage.COMPLETE),
            )
        )

    def test_completing_kit_would_finish_truck_ignores_nonfinal_or_already_complete_changes(self) -> None:
        truck = Truck(
            id=1,
            truck_number="F70004",
            kits=[
                _kit(kit_id=1, name="Body", stage=Stage.WELD),
                _kit(kit_id=2, name="Exterior", stage=Stage.WELD),
            ],
        )

        self.assertFalse(
            completing_kit_would_finish_truck(
                truck,
                kit_id=1,
                target_stage_id=int(Stage.COMPLETE),
            )
        )
        self.assertFalse(
            completing_kit_would_finish_truck(
                Truck(
                    id=truck.id,
                    truck_number=truck.truck_number,
                    kits=[_kit(kit_id=1, name="Body", stage=Stage.COMPLETE)],
                ),
                kit_id=1,
                target_stage_id=int(Stage.COMPLETE),
            )
        )


if __name__ == "__main__":
    unittest.main()
