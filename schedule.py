from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from models import Truck

# Master schedule baseline is centrally defined here (not editable in the UI).
MASTER_SCHEDULE_START_DATE = date(2026, 1, 5)
TRUCK_START_LAG_DAYS = 14

KIT_LAG_DURATION_DAYS: dict[str, tuple[int, int]] = {
    "Pumphouse": (0, 4),
    "Console Pack": (2, 4),
    "Body": (5, 6),
    "Interior Pack": (7, 5),
    "Exterior Pack": (9, 5),
}
DEFAULT_KIT_LAG_DURATION = (5, 5)


@dataclass
class KitScheduleStandard:
    kit_name: str
    lag_days: int
    duration_days: int


@dataclass
class ReleaseHoldItem:
    truck_number: str
    kit_name: str
    planned_start_date: date
    hold_days: int


@dataclass
class ScheduleInsights:
    master_start_date: date
    truck_start_lag_days: int
    standards: list[KitScheduleStandard]
    truck_planned_start_by_id: dict[int, date]
    kit_release_hold_days_by_id: dict[int, int]
    release_hold_items: list[ReleaseHoldItem]


def sort_trucks_natural(trucks: list[Truck]) -> list[Truck]:
    number_pattern = re.compile(r"(\d+)")

    def key_fn(truck: Truck) -> tuple[int, int | str]:
        match = number_pattern.search(truck.truck_number)
        if match:
            return (0, int(match.group(1)))
        return (1, truck.truck_number.lower())

    return sorted(trucks, key=key_fn)


def build_schedule_insights(trucks: list[Truck], today: date | None = None) -> ScheduleInsights:
    active_date = today or date.today()
    ordered_trucks = sort_trucks_natural(trucks)

    truck_planned_start_by_id: dict[int, date] = {}
    kit_release_hold_days_by_id: dict[int, int] = {}
    release_hold_items: list[ReleaseHoldItem] = []

    standards = [
        KitScheduleStandard(
            kit_name=name,
            lag_days=lag_duration[0],
            duration_days=lag_duration[1],
        )
        for name, lag_duration in KIT_LAG_DURATION_DAYS.items()
    ]

    for truck_index, truck in enumerate(ordered_trucks):
        truck_planned_start = MASTER_SCHEDULE_START_DATE + timedelta(days=truck_index * TRUCK_START_LAG_DAYS)
        if truck.id is not None:
            truck_planned_start_by_id[truck.id] = truck_planned_start

        for kit in truck.kits:
            if not kit.is_active:
                continue

            lag_days, _duration_days = KIT_LAG_DURATION_DAYS.get(kit.kit_name, DEFAULT_KIT_LAG_DURATION)
            planned_start = truck_planned_start + timedelta(days=lag_days)

            if kit.release_state != "not_released":
                continue
            if kit.current_stage != "release":
                continue
            if active_date < planned_start:
                continue

            hold_days = (active_date - planned_start).days
            if kit.id is not None:
                kit_release_hold_days_by_id[kit.id] = hold_days

            release_hold_items.append(
                ReleaseHoldItem(
                    truck_number=truck.truck_number,
                    kit_name=kit.kit_name,
                    planned_start_date=planned_start,
                    hold_days=hold_days,
                )
            )

    release_hold_items.sort(key=lambda item: item.hold_days, reverse=True)

    return ScheduleInsights(
        master_start_date=MASTER_SCHEDULE_START_DATE,
        truck_start_lag_days=TRUCK_START_LAG_DAYS,
        standards=standards,
        truck_planned_start_by_id=truck_planned_start_by_id,
        kit_release_hold_days_by_id=kit_release_hold_days_by_id,
        release_hold_items=release_hold_items,
    )
