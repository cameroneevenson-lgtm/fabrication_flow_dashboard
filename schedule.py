from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from models import Truck

DEFAULT_DAY_ZERO_WEEK = 0.0
DEFAULT_CURRENT_WEEK = 9.0
DEFAULT_TRUCK_START_LAG_WEEKS = 2.0
DEFAULT_KIT_LAG_DURATION_WEEKS: dict[str, tuple[float, float]] = {
    "Pumphouse": (0.0, 0.6),
    "Console Pack": (0.3, 0.6),
    "Body": (0.7, 0.9),
    "Interior Pack": (1.0, 0.7),
    "Exterior Pack": (1.3, 0.7),
}
DEFAULT_KIT_LAG_DURATION_WEEKS_FALLBACK = (0.7, 0.7)
CONFIG_FILENAME = "schedule_config.json"


@dataclass
class KitScheduleStandard:
    kit_name: str
    lag_weeks: float
    duration_weeks: float


@dataclass
class ReleaseHoldItem:
    truck_number: str
    kit_name: str
    planned_start_week: float
    hold_weeks: float


@dataclass
class ScheduleInsights:
    day_zero_week: float
    current_week: float
    truck_start_lag_weeks: float
    standards: list[KitScheduleStandard]
    truck_planned_start_week_by_id: dict[int, float]
    kit_release_hold_weeks_by_id: dict[int, float]
    release_hold_items: list[ReleaseHoldItem]


@dataclass
class ScheduleConfig:
    day_zero_week: float
    current_week: float
    truck_start_lag_weeks: float
    kit_lag_duration_weeks: dict[str, tuple[float, float]]
    default_kit_lag_duration_weeks: tuple[float, float]


def _config_path() -> Path:
    return Path(__file__).resolve().parent / CONFIG_FILENAME


def _default_config_data() -> dict[str, object]:
    return {
        "day_zero_week": DEFAULT_DAY_ZERO_WEEK,
        "current_week": DEFAULT_CURRENT_WEEK,
        "truck_start_lag_weeks": DEFAULT_TRUCK_START_LAG_WEEKS,
        "default_kit_lag_duration_weeks": {
            "lag_weeks": DEFAULT_KIT_LAG_DURATION_WEEKS_FALLBACK[0],
            "duration_weeks": DEFAULT_KIT_LAG_DURATION_WEEKS_FALLBACK[1],
        },
        "kits": {
            name: {"lag_weeks": lag, "duration_weeks": duration}
            for name, (lag, duration) in DEFAULT_KIT_LAG_DURATION_WEEKS.items()
        },
    }


def ensure_schedule_config_file() -> Path:
    path = _config_path()
    if path.exists():
        return path

    path.write_text(json.dumps(_default_config_data(), indent=2), encoding="utf-8")
    return path


def _safe_float(value: object, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _parse_lag_duration_weeks(value: object, fallback: tuple[float, float]) -> tuple[float, float]:
    if not isinstance(value, dict):
        return fallback

    lag_weeks = _safe_float(value.get("lag_weeks"), fallback[0])
    duration_weeks = _safe_float(value.get("duration_weeks"), fallback[1])
    return (max(0.0, lag_weeks), max(0.0, duration_weeks))


def load_schedule_config() -> ScheduleConfig:
    config_path = ensure_schedule_config_file()

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = _default_config_data()

    if not isinstance(raw, dict):
        raw = _default_config_data()

    day_zero_week = _safe_float(raw.get("day_zero_week"), DEFAULT_DAY_ZERO_WEEK)
    current_week = max(day_zero_week, _safe_float(raw.get("current_week"), DEFAULT_CURRENT_WEEK))
    truck_start_lag_weeks = max(0.0, _safe_float(raw.get("truck_start_lag_weeks"), DEFAULT_TRUCK_START_LAG_WEEKS))

    default_kit_lag_duration_weeks = _parse_lag_duration_weeks(
        raw.get("default_kit_lag_duration_weeks"),
        DEFAULT_KIT_LAG_DURATION_WEEKS_FALLBACK,
    )

    kit_lag_duration_weeks: dict[str, tuple[float, float]] = {}
    raw_kits = raw.get("kits", {})
    if not isinstance(raw_kits, dict):
        raw_kits = {}

    for kit_name, default_pair in DEFAULT_KIT_LAG_DURATION_WEEKS.items():
        kit_lag_duration_weeks[kit_name] = _parse_lag_duration_weeks(raw_kits.get(kit_name), default_pair)

    for kit_name, value in raw_kits.items():
        if not isinstance(kit_name, str):
            continue
        if kit_name in kit_lag_duration_weeks:
            continue
        kit_lag_duration_weeks[kit_name] = _parse_lag_duration_weeks(value, default_kit_lag_duration_weeks)

    return ScheduleConfig(
        day_zero_week=round(day_zero_week, 2),
        current_week=round(current_week, 2),
        truck_start_lag_weeks=round(truck_start_lag_weeks, 2),
        kit_lag_duration_weeks={
            k: (round(v[0], 2), round(v[1], 2)) for k, v in kit_lag_duration_weeks.items()
        },
        default_kit_lag_duration_weeks=(
            round(default_kit_lag_duration_weeks[0], 2),
            round(default_kit_lag_duration_weeks[1], 2),
        ),
    )


def sort_trucks_natural(trucks: list[Truck]) -> list[Truck]:
    number_pattern = re.compile(r"(\d+)")

    def key_fn(truck: Truck) -> tuple[int, int | str]:
        match = number_pattern.search(truck.truck_number)
        if match:
            return (0, int(match.group(1)))
        return (1, truck.truck_number.lower())

    return sorted(trucks, key=key_fn)


def build_schedule_insights(trucks: list[Truck]) -> ScheduleInsights:
    config = load_schedule_config()
    ordered_trucks = sort_trucks_natural(trucks)

    truck_planned_start_week_by_id: dict[int, float] = {}
    kit_release_hold_weeks_by_id: dict[int, float] = {}
    release_hold_items: list[ReleaseHoldItem] = []

    standards = [
        KitScheduleStandard(
            kit_name=name,
            lag_weeks=lag_duration[0],
            duration_weeks=lag_duration[1],
        )
        for name, lag_duration in config.kit_lag_duration_weeks.items()
    ]

    for truck_index, truck in enumerate(ordered_trucks):
        truck_planned_start_week = config.day_zero_week + (truck_index * config.truck_start_lag_weeks)
        truck_planned_start_week = round(truck_planned_start_week, 2)
        if truck.id is not None:
            truck_planned_start_week_by_id[truck.id] = truck_planned_start_week

        for kit in truck.kits:
            if not kit.is_active:
                continue

            lag_weeks, _duration_weeks = config.kit_lag_duration_weeks.get(
                kit.kit_name,
                config.default_kit_lag_duration_weeks,
            )
            planned_start_week = round(truck_planned_start_week + lag_weeks, 2)

            if kit.release_state != "not_released":
                continue
            if kit.current_stage != "release":
                continue
            if config.current_week < planned_start_week:
                continue

            hold_weeks = round(config.current_week - planned_start_week, 2)
            if kit.id is not None:
                kit_release_hold_weeks_by_id[kit.id] = hold_weeks

            release_hold_items.append(
                ReleaseHoldItem(
                    truck_number=truck.truck_number,
                    kit_name=kit.kit_name,
                    planned_start_week=planned_start_week,
                    hold_weeks=hold_weeks,
                )
            )

    release_hold_items.sort(key=lambda item: item.hold_weeks, reverse=True)

    return ScheduleInsights(
        day_zero_week=config.day_zero_week,
        current_week=config.current_week,
        truck_start_lag_weeks=config.truck_start_lag_weeks,
        standards=standards,
        truck_planned_start_week_by_id=truck_planned_start_week_by_id,
        kit_release_hold_weeks_by_id=kit_release_hold_weeks_by_id,
        release_hold_items=release_hold_items,
    )
