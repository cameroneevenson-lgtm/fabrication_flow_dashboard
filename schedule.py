from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from models import Truck
from stages import STAGE_SEQUENCE, Stage, stage_from_id, stage_from_key, stage_key

DEFAULT_KIT_LAG_WEEKS: dict[str, float] = {
    "Body": 0.0,
    "Pumphouse": 2.0,
    "Console": 2.5,
    "Interior": 4.0,
    "Exterior": 5.0,
}
DEFAULT_KIT_OPERATION_WINDOWS: dict[str, dict[Stage, tuple[float, float]]] = {
    "Body": {
        Stage.LASER: (0.0, 1.0),
        Stage.BEND: (0.5, 1.5),
        Stage.WELD: (1.0, 4.0),
    },
    "Pumphouse": {
        Stage.LASER: (0.0, 0.5),
        Stage.BEND: (0.5, 1.0),
        Stage.WELD: (0.0, 2.0),
    },
    "Console": {
        Stage.LASER: (0.0, 0.5),
        Stage.BEND: (0.5, 1.0),
        Stage.WELD: (1.0, 1.5),
    },
    "Interior": {
        Stage.LASER: (0.0, 1.0),
        Stage.BEND: (1.0, 2.0),
        Stage.WELD: (2.0, 3.0),
    },
    "Exterior": {
        Stage.LASER: (0.0, 1.0),
        Stage.BEND: (1.0, 2.0),
        Stage.WELD: (2.0, 3.0),
    },
}
CONFIG_FILENAME = "schedule_config.json"


@dataclass
class KitScheduleStandard:
    kit_name: str
    lag_weeks: float
    duration_weeks: float


@dataclass
class KitOperationWindow:
    kit_name: str
    stage_id: int
    start_week: float
    end_week: float


@dataclass
class ReleaseHoldItem:
    truck_number: str
    kit_name: str
    planned_start_week: float
    hold_weeks: float


@dataclass
class ConcurrencyItem:
    truck_number: str
    upstream_open_count: int


@dataclass
class ScheduleInsights:
    current_week: float
    standards: list[KitScheduleStandard]
    kit_operation_windows: list[KitOperationWindow]
    concurrency_items: list[ConcurrencyItem]
    truck_planned_start_week_by_id: dict[int, float]
    kit_release_hold_weeks_by_id: dict[int, float]
    release_hold_items: list[ReleaseHoldItem]


@dataclass
class ScheduleConfig:
    kit_lag_weeks: dict[str, float]
    kit_operation_windows: dict[str, dict[int, tuple[float, float]]]


_SCHEDULE_CONFIG_CACHE: ScheduleConfig | None = None
_SCHEDULE_CONFIG_MTIME_NS: int | None = None


def _config_path() -> Path:
    return Path(__file__).resolve().parent / CONFIG_FILENAME


def _default_config_data() -> dict[str, object]:
    return {
        "kits": {
            name: {"lag_weeks": lag}
            for name, lag in DEFAULT_KIT_LAG_WEEKS.items()
        },
        "kit_operation_windows": {
            kit_name: {
                stage_key(stage): {
                    "start_offset_weeks": values[0],
                    "end_offset_weeks": values[1],
                }
                for stage, values in stage_map.items()
            }
            for kit_name, stage_map in DEFAULT_KIT_OPERATION_WINDOWS.items()
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


def _current_year_week() -> float:
    now = datetime.now()
    iso_week = float(now.isocalendar().week)
    intra_week_fraction = (now.weekday() + (now.hour / 24.0)) / 7.0
    return round(iso_week + intra_week_fraction, 2)


def _parse_kit_lag_weeks(value: object, fallback: float) -> float:
    if not isinstance(value, dict):
        return fallback

    lag_weeks = _safe_float(value.get("lag_weeks"), fallback)
    return max(0.0, lag_weeks)


def _parse_kit_window_offsets(
    value: object,
    fallback: tuple[float, float],
    *,
    kit_lag_weeks: float,
) -> tuple[float, float]:
    if not isinstance(value, dict):
        return fallback

    if "start_offset_weeks" in value or "end_offset_weeks" in value or "duration_weeks" in value:
        start_offset = max(0.0, _safe_float(value.get("start_offset_weeks"), fallback[0]))
        if "end_offset_weeks" in value:
            end_offset = _safe_float(value.get("end_offset_weeks"), fallback[1])
        else:
            duration_weeks = _safe_float(value.get("duration_weeks"), fallback[1] - fallback[0])
            end_offset = start_offset + max(0.0, duration_weeks)
        end_offset = max(start_offset, end_offset)
        return (start_offset, end_offset)

    # Legacy shape support: start/end encoded as absolute week positions from truck start.
    start_week = max(0.0, _safe_float(value.get("start_week"), fallback[0] + kit_lag_weeks))
    end_week = max(start_week, _safe_float(value.get("end_week"), fallback[1] + kit_lag_weeks))
    start_offset = max(0.0, start_week - kit_lag_weeks)
    end_offset = max(start_offset, end_week - kit_lag_weeks)
    return (start_offset, end_offset)


def load_schedule_config() -> ScheduleConfig:
    global _SCHEDULE_CONFIG_CACHE, _SCHEDULE_CONFIG_MTIME_NS
    config_path = ensure_schedule_config_file()
    try:
        mtime_ns = int(config_path.stat().st_mtime_ns)
    except OSError:
        mtime_ns = None

    if (
        _SCHEDULE_CONFIG_CACHE is not None
        and mtime_ns is not None
        and _SCHEDULE_CONFIG_MTIME_NS == mtime_ns
    ):
        return _SCHEDULE_CONFIG_CACHE

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        raw = _default_config_data()

    if not isinstance(raw, dict):
        raw = _default_config_data()

    kit_lag_weeks: dict[str, float] = {}
    raw_kits = raw.get("kits", {})
    if not isinstance(raw_kits, dict):
        raw_kits = {}

    for kit_name, default_lag in DEFAULT_KIT_LAG_WEEKS.items():
        kit_lag_weeks[kit_name] = _parse_kit_lag_weeks(raw_kits.get(kit_name), default_lag)

    for kit_name, value in raw_kits.items():
        if not isinstance(kit_name, str):
            continue
        if kit_name in kit_lag_weeks:
            continue
        kit_lag_weeks[kit_name] = _parse_kit_lag_weeks(value, 0.0)

    raw_kit_windows = raw.get("kit_operation_windows", {})
    if not isinstance(raw_kit_windows, dict):
        raw_kit_windows = {}

    kit_operation_windows: dict[str, dict[int, tuple[float, float]]] = {}
    for kit_name, fallback_stage_map in DEFAULT_KIT_OPERATION_WINDOWS.items():
        raw_stage_map = raw_kit_windows.get(kit_name, {})
        if not isinstance(raw_stage_map, dict):
            raw_stage_map = {}
        kit_lag_week = kit_lag_weeks.get(kit_name, 0.0)
        stage_windows: dict[int, tuple[float, float]] = {}
        for stage, fallback_window in fallback_stage_map.items():
            stage_windows[int(stage)] = _parse_kit_window_offsets(
                raw_stage_map.get(stage_key(stage)),
                fallback_window,
                kit_lag_weeks=kit_lag_week,
            )
        kit_operation_windows[kit_name] = stage_windows

    for kit_name, raw_stage_map in raw_kit_windows.items():
        if not isinstance(kit_name, str):
            continue
        if kit_name in kit_operation_windows:
            continue
        if not isinstance(raw_stage_map, dict):
            continue
        kit_lag_week = kit_lag_weeks.get(kit_name, 0.0)
        stage_windows: dict[int, tuple[float, float]] = {}
        for raw_stage_key, value in raw_stage_map.items():
            stage = stage_from_key(raw_stage_key)
            if stage is None:
                continue
            stage_windows[int(stage)] = _parse_kit_window_offsets(
                value,
                (0.0, 1.0),
                kit_lag_weeks=kit_lag_week,
            )
        if stage_windows:
            kit_operation_windows[kit_name] = stage_windows

    parsed = ScheduleConfig(
        kit_lag_weeks={
            name: round(value, 2)
            for name, value in kit_lag_weeks.items()
        },
        kit_operation_windows={
            kit_name: {
                stage_id: (round(window[0], 2), round(window[1], 2))
                for stage_id, window in stage_map.items()
            }
            for kit_name, stage_map in kit_operation_windows.items()
        },
    )
    if mtime_ns is not None:
        _SCHEDULE_CONFIG_CACHE = parsed
        _SCHEDULE_CONFIG_MTIME_NS = mtime_ns
    return parsed


def sort_trucks_natural(trucks: list[Truck]) -> list[Truck]:
    number_pattern = re.compile(r"(\d+)")

    def key_fn(truck: Truck) -> tuple[int, int, int, int | str]:
        build_order = int(truck.build_order or 0)
        match = number_pattern.search(truck.truck_number)
        numeric_part = int(match.group(1)) if match else 0
        text_fallback: int | str = truck.truck_number.lower() if not match else numeric_part
        return (
            0 if build_order > 0 else 1,
            build_order if build_order > 0 else 0,
            0 if match else 1,
            text_fallback,
        )

    return sorted(trucks, key=key_fn)


def _planned_start_date_to_week(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None

    iso = parsed.isocalendar()
    intra_week_fraction = parsed.weekday() / 7.0
    return round(float(iso.week) + intra_week_fraction, 2)


def _planned_start_date_to_date(value: str) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _build_kit_operation_windows(config: ScheduleConfig) -> list[KitOperationWindow]:
    windows: list[KitOperationWindow] = []
    kit_order = {name: index for index, name in enumerate(config.kit_lag_weeks.keys())}
    stage_order_index = {int(stage): index for index, stage in enumerate(STAGE_SEQUENCE)}

    for kit_name, stage_map in config.kit_operation_windows.items():
        kit_lag_weeks = config.kit_lag_weeks.get(kit_name, 0.0)
        for stage_id, (start_offset_weeks, end_offset_weeks) in stage_map.items():
            windows.append(
                KitOperationWindow(
                    kit_name=kit_name,
                    stage_id=int(stage_from_id(stage_id)),
                    start_week=round(kit_lag_weeks + start_offset_weeks, 2),
                    end_week=round(kit_lag_weeks + end_offset_weeks, 2),
                )
            )

    windows.sort(
        key=lambda item: (
            kit_order.get(item.kit_name, 999),
            stage_order_index.get(item.stage_id, 999),
            item.start_week,
            item.end_week,
        )
    )
    return windows


def _derive_kit_duration_weeks(stage_map: dict[int, tuple[float, float]]) -> float:
    weld_window = stage_map.get(int(Stage.WELD))
    if weld_window is not None:
        return max(0.0, float(weld_window[1]))
    if not stage_map:
        return 0.0
    return max(max(0.0, float(end_offset)) for _start_offset, end_offset in stage_map.values())


def _build_concurrency_items(trucks: list[Truck]) -> list[ConcurrencyItem]:
    items: list[ConcurrencyItem] = []
    for truck in trucks:
        active_kits = [kit for kit in truck.kits if kit.is_active]
        if not active_kits:
            continue

        has_weld_started = any(stage_from_id(kit.front_stage_id) >= Stage.WELD for kit in active_kits)
        upstream_open_count = sum(
            1
            for kit in active_kits
            if stage_from_id(kit.back_stage_id) <= Stage.BEND and stage_from_id(kit.front_stage_id) < Stage.COMPLETE
        )
        if has_weld_started and upstream_open_count > 0:
            items.append(
                ConcurrencyItem(
                    truck_number=truck.truck_number,
                    upstream_open_count=upstream_open_count,
                )
            )

    return items


def build_schedule_insights(trucks: list[Truck]) -> ScheduleInsights:
    config = load_schedule_config()
    ordered_trucks = sort_trucks_natural(trucks)
    current_week = _current_year_week()

    truck_planned_start_week_by_id: dict[int, float] = {}
    kit_release_hold_weeks_by_id: dict[int, float] = {}
    release_hold_items: list[ReleaseHoldItem] = []

    standards: list[KitScheduleStandard] = []
    for kit_name, lag_weeks in config.kit_lag_weeks.items():
        stage_map = config.kit_operation_windows.get(kit_name, {})
        duration_weeks = round(_derive_kit_duration_weeks(stage_map), 2)
        standards.append(
            KitScheduleStandard(
                kit_name=kit_name,
                lag_weeks=lag_weeks,
                duration_weeks=duration_weeks,
            )
        )

    kit_operation_windows = _build_kit_operation_windows(config)
    concurrency_items = _build_concurrency_items(ordered_trucks)

    today = datetime.now().date()

    for truck in ordered_trucks:
        truck_planned_start_week = _planned_start_date_to_week(truck.planned_start_date)
        truck_planned_start_date = _planned_start_date_to_date(truck.planned_start_date)
        if truck_planned_start_week is None or truck_planned_start_date is None:
            # Unanchored trucks are intentionally excluded from schedule placement.
            continue
        if truck.id is not None:
            truck_planned_start_week_by_id[truck.id] = truck_planned_start_week

        for kit in truck.kits:
            if not kit.is_active:
                continue

            lag_weeks = config.kit_lag_weeks.get(kit.kit_name)
            if lag_weeks is None:
                # No implicit fallback for unknown kits.
                continue
            planned_start_week = round(truck_planned_start_week + lag_weeks, 2)

            front_stage = stage_from_id(kit.front_stage_id)
            back_stage = stage_from_id(kit.back_stage_id)
            if kit.release_state != "not_released":
                continue
            if not (front_stage == Stage.RELEASE and back_stage == Stage.RELEASE):
                continue

            if truck_planned_start_date is not None:
                planned_start_date = truck_planned_start_date + timedelta(days=(lag_weeks * 7.0))
                if today < planned_start_date:
                    continue
                hold_weeks = round((today - planned_start_date).days / 7.0, 2)

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
        current_week=round(current_week, 2),
        standards=standards,
        kit_operation_windows=kit_operation_windows,
        concurrency_items=concurrency_items,
        truck_planned_start_week_by_id=truck_planned_start_week_by_id,
        kit_release_hold_weeks_by_id=kit_release_hold_weeks_by_id,
        release_hold_items=release_hold_items,
    )
