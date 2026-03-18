from __future__ import annotations

import re
from dataclasses import dataclass, field

from models import Truck, TruckKit
from schedule import ScheduleInsights, build_schedule_insights
from stages import FABRICATION_STAGE_POSITION_SCALE, Stage, stage_from_id, stage_label

WELD_FEED_B_KIT_NAMES = {"console", "interior", "exterior"}


@dataclass
class BendBufferHealth:
    kit_count: int
    level: str
    drivers: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class WeldFeedStatus:
    score: float
    level: str
    drivers: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class AttentionItem:
    priority: int
    title: str
    detail: str


@dataclass
class DashboardMetrics:
    laser_buffer: BendBufferHealth
    bend_buffer: BendBufferHealth
    weld_feed_a: WeldFeedStatus
    weld_feed_b: WeldFeedStatus
    attention_items: list[AttentionItem]


@dataclass
class SnapshotSyncSummary:
    ahead_kits: int
    in_sync_kits: int
    behind_kits: int


@dataclass
class SnapshotTruckRow:
    truck_number: str
    main_stage: str
    sync_status: str
    risk_category: str
    issue_summary: str
    tone: str


@dataclass
class SnapshotMetrics:
    sync_summary: SnapshotSyncSummary
    truck_rows: list[SnapshotTruckRow]


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


def compute_dashboard_metrics(
    trucks: list[Truck],
    schedule_insights: ScheduleInsights | None = None,
) -> DashboardMetrics:
    ordered_trucks = sort_trucks_natural(trucks)
    insights = schedule_insights or build_schedule_insights(ordered_trucks)

    laser_buffer = _compute_laser_buffer(ordered_trucks)
    bend_buffer = _compute_bend_buffer(ordered_trucks)
    weld_feed_a = _compute_weld_feed_a(ordered_trucks)
    weld_feed_b = _compute_weld_feed(ordered_trucks, feed="b")
    attention_items = _build_attention_items(
        bend_buffer=bend_buffer,
        schedule_insights=insights,
    )

    return DashboardMetrics(
        laser_buffer=laser_buffer,
        bend_buffer=bend_buffer,
        weld_feed_a=weld_feed_a,
        weld_feed_b=weld_feed_b,
        attention_items=attention_items,
    )


def compute_snapshot_metrics(
    trucks: list[Truck],
    schedule_insights: ScheduleInsights | None = None,
    dashboard_metrics: DashboardMetrics | None = None,
) -> SnapshotMetrics:
    ordered_trucks = sort_trucks_natural(trucks)
    insights = schedule_insights or build_schedule_insights(ordered_trucks)
    _metrics = dashboard_metrics or compute_dashboard_metrics(ordered_trucks, schedule_insights=insights)

    hold_count_by_truck: dict[str, int] = {}
    for item in insights.release_hold_items:
        hold_count_by_truck[item.truck_number] = hold_count_by_truck.get(item.truck_number, 0) + 1

    concurrency_by_truck = {
        item.truck_number: int(item.upstream_open_count)
        for item in insights.concurrency_items
    }

    window_by_kit_name: dict[str, list[tuple[Stage, float, float]]] = {}
    for window in insights.kit_operation_windows:
        key = str(window.kit_name or "").strip().lower()
        if not key:
            continue
        stage = stage_from_id(window.stage_id)
        window_by_kit_name.setdefault(key, []).append(
            (stage, float(window.start_week), float(window.end_week))
        )

    for windows in window_by_kit_name.values():
        windows.sort(key=lambda item: (item[1], item[2], int(item[0])))

    behind_kits = 0
    ahead_kits = 0
    in_sync_kits = 0
    truck_rows: list[SnapshotTruckRow] = []

    for truck in ordered_trucks:
        truck_id = truck.id
        truck_start_week = insights.truck_planned_start_week_by_id.get(int(truck_id or -1))

        truck_blocked_count = 0
        truck_behind_count = 0
        main_sync_status = "In Sync"
        main_kit = _find_main_body_kit(truck)
        main_stage = "-"

        for kit in truck.kits:
            if not kit.is_active:
                continue
            if str(kit.blocker or "").strip():
                truck_blocked_count += 1

            expected_stage = _expected_stage_for_kit(
                kit=kit,
                current_week=insights.current_week,
                truck_start_week=truck_start_week,
                windows_by_kit_name=window_by_kit_name,
            )
            if expected_stage is None:
                in_sync_kits += 1
                continue

            actual_stage = stage_from_id(kit.front_stage_id)
            sync_key = _sync_key(actual_stage=actual_stage, expected_stage=expected_stage)
            if sync_key == "behind":
                behind_kits += 1
                truck_behind_count += 1
            elif sync_key == "ahead":
                ahead_kits += 1
            else:
                in_sync_kits += 1

            if main_kit and kit.id == main_kit.id:
                main_sync_status = _sync_label(sync_key)

        if main_kit:
            main_stage = stage_label(main_kit.front_stage_id)

        late_release_count = hold_count_by_truck.get(truck.truck_number, 0)
        overlap_open_count = concurrency_by_truck.get(truck.truck_number, 0)

        if late_release_count > 0:
            risk_category = "Late Release"
            issue_summary = f"{late_release_count} kit(s) late release."
            tone = "problem"
        elif truck_blocked_count > 0:
            risk_category = "Blocked"
            issue_summary = f"{truck_blocked_count} blocked kit(s)."
            tone = "problem"
        elif truck_behind_count > 0:
            risk_category = "Fabrication Behind"
            issue_summary = f"{truck_behind_count} kit(s) behind master schedule."
            tone = "problem"
        elif overlap_open_count > 0:
            risk_category = "Overlapping Flow"
            issue_summary = f"{overlap_open_count} upstream kit(s) still open."
            tone = "caution"
        else:
            risk_category = "In Sync"
            issue_summary = "None."
            tone = "ok"

        truck_rows.append(
            SnapshotTruckRow(
                truck_number=truck.truck_number,
                main_stage=main_stage,
                sync_status=main_sync_status,
                risk_category=risk_category,
                issue_summary=issue_summary,
                tone=tone,
            )
        )

    return SnapshotMetrics(
        sync_summary=SnapshotSyncSummary(
            ahead_kits=ahead_kits,
            in_sync_kits=in_sync_kits,
            behind_kits=behind_kits,
        ),
        truck_rows=truck_rows,
    )


def _find_main_body_kit(truck: Truck) -> TruckKit | None:
    for kit in truck.kits:
        if not kit.is_active:
            continue
        if kit.is_main_kit or kit.kit_name.lower() == "body":
            return kit
    return None


def _driver_label(truck: Truck, kit: TruckKit, note: str | None = None) -> str:
    truck_number = str(truck.truck_number or "").strip()
    kit_name = str(kit.kit_name or "").strip()
    base = f"{truck_number} {kit_name}".strip()
    if note:
        return f"{base} ({note})"
    return base


def _stage_index(value: int | Stage) -> int:
    stage = stage_from_id(value)
    order = {
        Stage.RELEASE: 0,
        Stage.LASER: 1,
        Stage.BEND: 2,
        Stage.WELD: 3,
        Stage.COMPLETE: 4,
    }
    return order.get(stage, 0)


def _sync_key(actual_stage: Stage, expected_stage: Stage) -> str:
    actual = _stage_index(actual_stage)
    expected = _stage_index(expected_stage)
    if actual < expected:
        return "behind"
    if actual > expected:
        return "ahead"
    return "in_sync"


def _sync_label(sync_key: str) -> str:
    if sync_key == "behind":
        return "Behind"
    if sync_key == "ahead":
        return "Ahead"
    return "In Sync"


def _expected_stage_for_kit(
    kit: TruckKit,
    current_week: float,
    truck_start_week: float | None,
    windows_by_kit_name: dict[str, list[tuple[Stage, float, float]]],
) -> Stage | None:
    if truck_start_week is None:
        return None
    kit_key = str(kit.kit_name or "").strip().lower()
    windows = windows_by_kit_name.get(kit_key, [])
    if not windows:
        return None

    absolute: list[tuple[Stage, float, float]] = [
        (stage, truck_start_week + start_week, truck_start_week + end_week)
        for stage, start_week, end_week in windows
    ]
    absolute.sort(key=lambda item: (item[1], item[2], int(item[0])))

    min_start = min(item[1] for item in absolute)
    max_end = max(item[2] for item in absolute)
    if current_week < min_start:
        return Stage.RELEASE
    if current_week >= max_end:
        return Stage.COMPLETE

    for stage, start_week, end_week in absolute:
        if start_week <= current_week <= end_week:
            return stage

    expected = Stage.RELEASE
    for stage, start_week, _end_week in absolute:
        if current_week >= start_week:
            expected = stage
    return expected


def _compute_bend_buffer(trucks: list[Truck]) -> BendBufferHealth:
    front_buffer_count = 0
    has_body_tail_in_buffer = False
    front_drivers: list[str] = []
    tail_driver = ""

    for truck in trucks:
        for kit in truck.kits:
            if not kit.is_active:
                continue
            if kit.release_state == "not_released":
                continue

            is_body = bool(kit.is_main_kit or kit.kit_name.strip().lower() == "body")
            front_stage = stage_from_id(kit.front_stage_id)
            if front_stage in {Stage.LASER, Stage.BEND}:
                front_buffer_count += 1
                front_drivers.append(_driver_label(truck, kit))

            if is_body:
                back_stage = stage_from_id(kit.back_stage_id)
                if back_stage in {Stage.LASER, Stage.BEND}:
                    has_body_tail_in_buffer = True
                    if not tail_driver:
                        tail_driver = _driver_label(truck, kit, "tail")

    if front_buffer_count >= 2:
        count = front_buffer_count
        level = "healthy"
        drivers = tuple(front_drivers)
    elif front_buffer_count > 0:
        count = front_buffer_count
        level = "low"
        drivers = tuple(front_drivers)
    elif has_body_tail_in_buffer:
        # Prevent a hard "dry" signal when the body tail is still feeding laser/bend.
        count = 1
        level = "low"
        drivers = (tail_driver,) if tail_driver else ()
    else:
        count = 0
        level = "dry"
        drivers = ()

    return BendBufferHealth(kit_count=count, level=level, drivers=drivers)


def _compute_laser_buffer(trucks: list[Truck]) -> BendBufferHealth:
    laser_count = 0
    has_body_tail_in_laser = False
    front_drivers: list[str] = []
    tail_driver = ""

    for truck in trucks:
        for kit in truck.kits:
            if not kit.is_active:
                continue
            if kit.release_state == "not_released":
                continue

            is_body = bool(kit.is_main_kit or kit.kit_name.strip().lower() == "body")
            front_stage = stage_from_id(kit.front_stage_id)
            if front_stage == Stage.LASER:
                laser_count += 1
                front_drivers.append(_driver_label(truck, kit))

            if is_body:
                back_stage = stage_from_id(kit.back_stage_id)
                if back_stage == Stage.LASER:
                    has_body_tail_in_laser = True
                    if not tail_driver:
                        tail_driver = _driver_label(truck, kit, "tail")

    if laser_count >= 3:
        count = laser_count
        level = "healthy"
        drivers = tuple(front_drivers)
    elif laser_count > 0:
        count = laser_count
        level = "low"
        drivers = tuple(front_drivers)
    elif has_body_tail_in_laser:
        count = 1
        level = "low"
        drivers = (tail_driver,) if tail_driver else ()
    else:
        count = 0
        level = "dry"
        drivers = ()

    return BendBufferHealth(kit_count=count, level=level, drivers=drivers)


def _kit_in_weld_feed(kit: TruckKit, feed: str | None) -> bool:
    if feed is None:
        return True
    kit_name = str(kit.kit_name or "").strip().lower()
    is_feed_b = kit_name in WELD_FEED_B_KIT_NAMES
    if feed == "b":
        return is_feed_b
    if feed == "a":
        return not is_feed_b
    return True


def _is_body_ready_to_start(kit: TruckKit | None) -> bool:
    if kit is None or not kit.is_active:
        return False
    return bool(
        kit.release_state == "released"
        or stage_from_id(kit.front_stage_id) > Stage.RELEASE
    )


def _stage_progress_percent(kit: TruckKit, stage: Stage) -> int:
    positions = FABRICATION_STAGE_POSITION_SCALE.get(stage)
    if not positions:
        return 0

    current_value = int(getattr(kit, "front_position", positions[0]) or positions[0])
    if current_value in positions:
        index = positions.index(current_value)
    else:
        index = min(range(len(positions)), key=lambda idx: abs(positions[idx] - current_value))

    display_steps = [0, 10, 50, 90, 100]
    if len(positions) == len(display_steps):
        return int(display_steps[index])
    if len(positions) <= 1:
        return 100
    return int(round((float(index) / float(len(positions) - 1)) * 100.0))


def _weld_feed_contribution(kit: TruckKit) -> tuple[float, str | None]:
    front_stage = stage_from_id(kit.front_stage_id)
    if front_stage == Stage.WELD:
        return (1.0, "weld")
    if front_stage == Stage.BEND:
        return (1.0, "bend")
    if front_stage == Stage.LASER:
        return (0.6, "laser")
    if front_stage == Stage.RELEASE and str(kit.release_state or "").strip().lower() == "released":
        return (0.25, "released")
    return (0.0, None)


def _compute_weld_feed_a(trucks: list[Truck]) -> WeldFeedStatus:
    main_bodies: list[tuple[Truck, TruckKit]] = []
    for truck in trucks:
        main_kit = _find_main_body_kit(truck)
        if main_kit is None or not main_kit.is_active:
            continue
        main_bodies.append((truck, main_kit))

    if not main_bodies:
        return WeldFeedStatus(score=0.0, level="watch")

    current_weld_index = -1
    for index, (_truck, kit) in enumerate(main_bodies):
        if stage_from_id(kit.front_stage_id) == Stage.WELD:
            current_weld_index = index
            break

    if current_weld_index >= 0:
        current_truck, current_body = main_bodies[current_weld_index]
        next_entry = main_bodies[current_weld_index + 1] if (current_weld_index + 1) < len(main_bodies) else None
        next_body = next_entry[1] if next_entry is not None else None
        next_ready = _is_body_ready_to_start(next_body)
        progress_percent = _stage_progress_percent(current_body, Stage.WELD)
        drivers = [_driver_label(current_truck, current_body, f"weld {progress_percent}%")]
        if next_entry is not None:
            next_truck, next_body = next_entry
            next_note = "next ready" if next_ready else "next blocked"
            drivers.append(_driver_label(next_truck, next_body, next_note))

        if next_ready:
            level = "healthy"
        else:
            level = "low" if progress_percent >= 50 else "watch"
        return WeldFeedStatus(score=float(progress_percent), level=level, drivers=tuple(drivers))

    next_entry = next(
        ((truck, kit) for truck, kit in main_bodies if stage_from_id(kit.front_stage_id) != Stage.COMPLETE),
        None,
    )
    if next_entry is None:
        return WeldFeedStatus(score=0.0, level="watch")
    next_truck, next_body = next_entry
    drivers = (_driver_label(next_truck, next_body, "next ready"),)
    if _is_body_ready_to_start(next_body):
        return WeldFeedStatus(score=0.0, level="watch", drivers=drivers)
    return WeldFeedStatus(
        score=0.0,
        level="low",
        drivers=(_driver_label(next_truck, next_body, "next blocked"),),
    )


def _compute_weld_feed(trucks: list[Truck], feed: str | None = None) -> WeldFeedStatus:
    score = 0.0
    drivers: list[str] = []
    for truck in trucks:
        for kit in truck.kits:
            if not kit.is_active:
                continue
            if not _kit_in_weld_feed(kit, feed):
                continue
            contribution, note = _weld_feed_contribution(kit)
            if contribution <= 0.0:
                continue
            score += float(contribution)
            note_text = note or "feed"
            drivers.append(_driver_label(truck, kit, note_text))

    if feed == "b":
        low_threshold = 1.5
        healthy_threshold = 2.0
    else:
        low_threshold = 2.0
        healthy_threshold = 4.0

    if score < low_threshold:
        level = "low"
    elif score < healthy_threshold:
        level = "watch"
    else:
        level = "healthy"

    return WeldFeedStatus(score=round(score, 1), level=level, drivers=tuple(drivers))


def _format_late_weeks(value: float) -> str:
    rounded_weeks = max(0, int(float(value) + 0.5))
    unit = "week" if rounded_weeks == 1 else "weeks"
    return f"{rounded_weeks} {unit} late"


def _build_attention_items(
    bend_buffer: BendBufferHealth,
    schedule_insights: ScheduleInsights,
) -> list[AttentionItem]:
    items: list[AttentionItem] = []

    if schedule_insights.release_hold_items:
        oldest = schedule_insights.release_hold_items[0]
        items.append(
            AttentionItem(
                priority=96,
                title="Engineering release is holding work start",
                detail=(
                    f"{len(schedule_insights.release_hold_items)} kit(s) past planned start; "
                    f"oldest {_format_late_weeks(oldest.hold_weeks)} "
                    f"({oldest.truck_number} {oldest.kit_name})."
                ),
            )
        )

    laser_label = stage_label(Stage.LASER).lower()
    bend_label = stage_label(Stage.BEND).lower()
    if bend_buffer.level == "dry":
        items.append(
            AttentionItem(
                priority=85,
                title="Bend buffer dry",
                detail=f"No released kits are in {laser_label}/{bend_label}.",
            )
        )
    elif bend_buffer.level == "low":
        items.append(
            AttentionItem(
                priority=80,
                title="Bend buffer low",
                detail=f"Less than 3 released kit(s) are in {laser_label}/{bend_label}.",
            )
        )

    if not items:
        items.append(
            AttentionItem(
                priority=0,
                title="No urgent flow risks",
                detail="Flow signals are healthy right now.",
            )
        )

    return sorted(items, key=lambda item: item.priority, reverse=True)
