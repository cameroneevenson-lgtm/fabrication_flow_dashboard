from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from models import STAGE_ORDER, Truck

DEFAULT_DAY_ZERO_WEEK = 0.0
DEFAULT_CURRENT_WEEK = 9.0
DEFAULT_TRUCK_START_LAG_WEEKS = 2.0
DEFAULT_KIT_LAG_DURATION_WEEKS: dict[str, tuple[float, float]] = {
    "Body": (0.0, 0.9),
    "Pumphouse": (2.0, 2.0),
    "Console Pack": (2.5, 1.5),
    "Interior Pack": (4.0, 3.0),
    "Exterior Pack": (4.0, 3.0),
}
DEFAULT_KIT_LAG_DURATION_WEEKS_FALLBACK = (0.7, 0.7)
DEFAULT_OPERATION_STANDARDS: dict[str, tuple[float, float, float]] = {
    "release": (0.0, 0.5, 1.5),
    "laser": (0.0, 1.0, 3.0),
    "bend": (0.5, 1.0, 3.5),
    "weld": (1.0, 3.0, 4.0),
}
DEFAULT_KIT_OPERATION_WINDOWS: dict[str, dict[str, tuple[float, float, str, float]]] = {
    "Body": {
        "laser": (0.0, 1.0, "full", 5.0),
        "bend": (0.5, 1.5, "full", 5.0),
        "weld": (1.0, 4.0, "full", 15.0),
    },
    "Pumphouse": {
        "laser": (2.0, 2.5, "full", 2.5),
        "bend": (2.5, 3.0, "full", 2.5),
        "weld": (2.0, 4.0, "full", 10.0),
    },
    "Console Pack": {
        "laser": (2.5, 3.0, "full", 2.5),
        "bend": (3.0, 3.5, "full", 2.5),
        "weld": (3.5, 4.0, "full", 2.5),
    },
    "Interior Pack": {
        "laser": (4.0, 5.0, "flex", 3.0),
        "bend": (5.0, 6.0, "flex", 3.0),
        "weld": (6.0, 7.0, "flex", 3.0),
    },
    "Exterior Pack": {
        "laser": (4.0, 5.0, "flex", 3.0),
        "bend": (5.0, 6.0, "flex", 3.0),
        "weld": (6.0, 7.0, "flex", 3.0),
    },
}
DEFAULT_REPEAT_CYCLE = {
    "repeat_weeks": 4.0,
    "cycle_weeks": 7.0,
    "odd_jobs_weeks": 1.0,
}
CONFIG_FILENAME = "schedule_config.json"


@dataclass
class KitScheduleStandard:
    kit_name: str
    lag_weeks: float
    duration_weeks: float


@dataclass
class OperationStandard:
    stage: str
    start_offset_weeks: float
    duration_weeks: float
    work_days: float

    @property
    def spare_days(self) -> float:
        return round((self.duration_weeks * 5.0) - self.work_days, 2)


@dataclass
class OperationOverlap:
    upstream_stage: str
    downstream_stage: str
    overlap_weeks: float


@dataclass
class KitOperationWindow:
    kit_name: str
    stage: str
    start_week: float
    end_week: float


@dataclass
class CyclePlan:
    repeat_weeks: float
    cycle_weeks: float
    odd_jobs_weeks: float
    cycle_position_week: float
    odd_jobs_window_start_week: float
    in_odd_jobs_window: bool


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
    day_zero_week: float
    current_week: float
    truck_start_lag_weeks: float
    standards: list[KitScheduleStandard]
    operation_standards: list[OperationStandard]
    operation_overlaps: list[OperationOverlap]
    kit_operation_windows: list[KitOperationWindow]
    cycle_plan: CyclePlan
    concurrency_items: list[ConcurrencyItem]
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
    operation_standards: dict[str, tuple[float, float, float]]
    kit_operation_windows: dict[str, dict[str, tuple[float, float]]]
    repeat_cycle: tuple[float, float, float]


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
        "operation_standards": {
            stage: {
                "start_offset_weeks": values[0],
                "duration_weeks": values[1],
                "work_days": values[2],
            }
            for stage, values in DEFAULT_OPERATION_STANDARDS.items()
        },
        "kit_operation_windows": {
            kit_name: {
                stage: {
                    "start_week": values[0],
                    "end_week": values[1],
                }
                for stage, values in stage_map.items()
            }
            for kit_name, stage_map in DEFAULT_KIT_OPERATION_WINDOWS.items()
        },
        "repeat_cycle": {
            "repeat_weeks": DEFAULT_REPEAT_CYCLE["repeat_weeks"],
            "cycle_weeks": DEFAULT_REPEAT_CYCLE["cycle_weeks"],
            "odd_jobs_weeks": DEFAULT_REPEAT_CYCLE["odd_jobs_weeks"],
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
    """Return current ISO week-of-year with a small intra-week fraction."""
    now = datetime.now()
    iso_week = float(now.isocalendar().week)
    intra_week_fraction = (now.weekday() + (now.hour / 24.0)) / 7.0
    return round(iso_week + intra_week_fraction, 2)


def _parse_lag_duration_weeks(value: object, fallback: tuple[float, float]) -> tuple[float, float]:
    if not isinstance(value, dict):
        return fallback

    lag_weeks = _safe_float(value.get("lag_weeks"), fallback[0])
    duration_weeks = _safe_float(value.get("duration_weeks"), fallback[1])
    return (max(0.0, lag_weeks), max(0.0, duration_weeks))


def _parse_operation_standard(
    value: object,
    fallback: tuple[float, float, float],
) -> tuple[float, float, float]:
    if not isinstance(value, dict):
        return fallback

    start_offset_weeks = _safe_float(value.get("start_offset_weeks"), fallback[0])
    duration_weeks = _safe_float(value.get("duration_weeks"), fallback[1])
    work_days = _safe_float(value.get("work_days"), fallback[2])

    return (
        max(0.0, start_offset_weeks),
        max(0.0, duration_weeks),
        max(0.0, work_days),
    )


def _parse_start_end_weeks(value: object, fallback: tuple[float, float]) -> tuple[float, float]:
    if not isinstance(value, dict):
        return fallback

    start_week = max(0.0, _safe_float(value.get("start_week"), fallback[0]))
    end_week = max(start_week, _safe_float(value.get("end_week"), fallback[1]))
    return (start_week, end_week)


def load_schedule_config() -> ScheduleConfig:
    config_path = ensure_schedule_config_file()

    try:
        # Use utf-8-sig so BOM-encoded JSON from Windows editors still loads.
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        raw = _default_config_data()

    if not isinstance(raw, dict):
        raw = _default_config_data()

    day_zero_week = _safe_float(raw.get("day_zero_week"), DEFAULT_DAY_ZERO_WEEK)
    # Auto-update from live calendar week; config current_week is ignored.
    current_week = max(day_zero_week, _current_year_week())
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

    raw_operations = raw.get("operation_standards", {})
    if not isinstance(raw_operations, dict):
        raw_operations = {}

    operation_standards: dict[str, tuple[float, float, float]] = {}
    for stage, fallback_values in DEFAULT_OPERATION_STANDARDS.items():
        operation_standards[stage] = _parse_operation_standard(raw_operations.get(stage), fallback_values)

    for stage, value in raw_operations.items():
        if not isinstance(stage, str):
            continue
        if stage in operation_standards:
            continue
        operation_standards[stage] = _parse_operation_standard(value, (0.0, 1.0, 5.0))

    raw_kit_windows = raw.get("kit_operation_windows", {})
    if not isinstance(raw_kit_windows, dict):
        raw_kit_windows = {}

    kit_operation_windows: dict[str, dict[str, tuple[float, float]]] = {}
    for kit_name, fallback_stage_map in DEFAULT_KIT_OPERATION_WINDOWS.items():
        raw_stage_map = raw_kit_windows.get(kit_name, {})
        if not isinstance(raw_stage_map, dict):
            raw_stage_map = {}
        stage_windows: dict[str, tuple[float, float]] = {}
        for stage, fallback_window in fallback_stage_map.items():
            stage_windows[stage] = _parse_start_end_weeks(raw_stage_map.get(stage), fallback_window)
        kit_operation_windows[kit_name] = stage_windows

    for kit_name, raw_stage_map in raw_kit_windows.items():
        if not isinstance(kit_name, str):
            continue
        if kit_name in kit_operation_windows:
            continue
        if not isinstance(raw_stage_map, dict):
            continue
        stage_windows: dict[str, tuple[float, float]] = {}
        for stage, value in raw_stage_map.items():
            if not isinstance(stage, str):
                continue
            stage_windows[stage] = _parse_start_end_weeks(value, (0.0, 1.0))
        if stage_windows:
            kit_operation_windows[kit_name] = stage_windows

    raw_repeat_cycle = raw.get("repeat_cycle", {})
    if not isinstance(raw_repeat_cycle, dict):
        raw_repeat_cycle = {}
    repeat_weeks = max(0.0, _safe_float(raw_repeat_cycle.get("repeat_weeks"), DEFAULT_REPEAT_CYCLE["repeat_weeks"]))
    cycle_weeks = max(1.0, _safe_float(raw_repeat_cycle.get("cycle_weeks"), DEFAULT_REPEAT_CYCLE["cycle_weeks"]))
    odd_jobs_weeks = max(0.0, _safe_float(raw_repeat_cycle.get("odd_jobs_weeks"), DEFAULT_REPEAT_CYCLE["odd_jobs_weeks"]))
    odd_jobs_weeks = min(odd_jobs_weeks, cycle_weeks)

    return ScheduleConfig(
        day_zero_week=round(day_zero_week, 2),
        current_week=round(current_week, 2),
        truck_start_lag_weeks=round(truck_start_lag_weeks, 2),
        kit_lag_duration_weeks={
            name: (round(vals[0], 2), round(vals[1], 2))
            for name, vals in kit_lag_duration_weeks.items()
        },
        default_kit_lag_duration_weeks=(
            round(default_kit_lag_duration_weeks[0], 2),
            round(default_kit_lag_duration_weeks[1], 2),
        ),
        operation_standards={
            stage: (round(vals[0], 2), round(vals[1], 2), round(vals[2], 2))
            for stage, vals in operation_standards.items()
        },
        kit_operation_windows={
            kit_name: {
                stage: (round(window[0], 2), round(window[1], 2))
                for stage, window in stage_map.items()
            }
            for kit_name, stage_map in kit_operation_windows.items()
        },
        repeat_cycle=(
            round(repeat_weeks, 2),
            round(cycle_weeks, 2),
            round(odd_jobs_weeks, 2),
        ),
    )


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


def _build_operation_standards(config: ScheduleConfig) -> list[OperationStandard]:
    ordered_stages = [stage for stage in STAGE_ORDER if stage in config.operation_standards]
    standards: list[OperationStandard] = []
    for stage in ordered_stages:
        values = config.operation_standards[stage]
        standards.append(
            OperationStandard(
                stage=stage,
                start_offset_weeks=values[0],
                duration_weeks=values[1],
                work_days=values[2],
            )
        )
    return standards


def _build_operation_overlaps(standards: list[OperationStandard]) -> list[OperationOverlap]:
    by_stage = {standard.stage: standard for standard in standards}
    overlaps: list[OperationOverlap] = []

    for upstream_stage, downstream_stage in (("laser", "bend"), ("bend", "weld")):
        upstream = by_stage.get(upstream_stage)
        downstream = by_stage.get(downstream_stage)
        if not upstream or not downstream:
            continue

        upstream_end = upstream.start_offset_weeks + upstream.duration_weeks
        overlap_weeks = round(upstream_end - downstream.start_offset_weeks, 2)
        if overlap_weeks > 0.0:
            overlaps.append(
                OperationOverlap(
                    upstream_stage=upstream_stage,
                    downstream_stage=downstream_stage,
                    overlap_weeks=overlap_weeks,
                )
            )

    return overlaps


def _build_kit_operation_windows(config: ScheduleConfig) -> list[KitOperationWindow]:
    windows: list[KitOperationWindow] = []
    kit_order = {name: index for index, name in enumerate(config.kit_lag_duration_weeks.keys())}
    stage_order_index = {stage: index for index, stage in enumerate(STAGE_ORDER)}

    for kit_name, stage_map in config.kit_operation_windows.items():
        for stage, (start_week, end_week) in stage_map.items():
            windows.append(
                KitOperationWindow(
                    kit_name=kit_name,
                    stage=stage,
                    start_week=start_week,
                    end_week=end_week,
                )
            )

    windows.sort(
        key=lambda item: (
            kit_order.get(item.kit_name, 999),
            stage_order_index.get(item.stage, 999),
            item.start_week,
            item.end_week,
        )
    )
    return windows


def _build_cycle_plan(config: ScheduleConfig) -> CyclePlan:
    repeat_weeks, cycle_weeks, odd_jobs_weeks = config.repeat_cycle
    normalized_cycle_weeks = max(1.0, cycle_weeks)
    normalized_odd_jobs_weeks = min(max(0.0, odd_jobs_weeks), normalized_cycle_weeks)

    week_delta = max(0.0, config.current_week - config.day_zero_week)
    cycle_position = round(week_delta % normalized_cycle_weeks, 2)
    odd_jobs_window_start = round(max(0.0, normalized_cycle_weeks - normalized_odd_jobs_weeks), 2)
    in_odd_jobs_window = normalized_odd_jobs_weeks > 0.0 and cycle_position >= odd_jobs_window_start

    return CyclePlan(
        repeat_weeks=round(repeat_weeks, 2),
        cycle_weeks=round(normalized_cycle_weeks, 2),
        odd_jobs_weeks=round(normalized_odd_jobs_weeks, 2),
        cycle_position_week=cycle_position,
        odd_jobs_window_start_week=odd_jobs_window_start,
        in_odd_jobs_window=in_odd_jobs_window,
    )


def _build_concurrency_items(trucks: list[Truck]) -> list[ConcurrencyItem]:
    items: list[ConcurrencyItem] = []
    for truck in trucks:
        active_kits = [kit for kit in truck.kits if kit.is_active]
        if not active_kits:
            continue

        has_weld_started = any(kit.current_stage in {"weld", "complete"} for kit in active_kits)
        upstream_open_count = sum(1 for kit in active_kits if kit.current_stage in {"release", "laser", "bend"})
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

    truck_planned_start_week_by_id: dict[int, float] = {}
    kit_release_hold_weeks_by_id: dict[int, float] = {}
    release_hold_items: list[ReleaseHoldItem] = []

    standards = [
        KitScheduleStandard(
            kit_name=name,
            lag_weeks=values[0],
            duration_weeks=values[1],
        )
        for name, values in config.kit_lag_duration_weeks.items()
    ]

    operation_standards = _build_operation_standards(config)
    operation_overlaps = _build_operation_overlaps(operation_standards)
    kit_operation_windows = _build_kit_operation_windows(config)
    cycle_plan = _build_cycle_plan(config)
    concurrency_items = _build_concurrency_items(ordered_trucks)

    today = datetime.now().date()

    for truck_index, truck in enumerate(ordered_trucks):
        truck_planned_start_week = _planned_start_date_to_week(truck.planned_start_date)
        truck_planned_start_date = _planned_start_date_to_date(truck.planned_start_date)
        if truck_planned_start_week is None:
            truck_planned_start_week = round(
                config.day_zero_week + (truck_index * config.truck_start_lag_weeks),
                2,
            )
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
            if truck_planned_start_date is not None:
                planned_start_date = truck_planned_start_date + timedelta(days=(lag_weeks * 7.0))
                if today < planned_start_date:
                    continue
                hold_weeks = round((today - planned_start_date).days / 7.0, 2)
            else:
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
        operation_standards=operation_standards,
        operation_overlaps=operation_overlaps,
        kit_operation_windows=kit_operation_windows,
        cycle_plan=cycle_plan,
        concurrency_items=concurrency_items,
        truck_planned_start_week_by_id=truck_planned_start_week_by_id,
        kit_release_hold_weeks_by_id=kit_release_hold_weeks_by_id,
        release_hold_items=release_hold_items,
    )
