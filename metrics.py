from __future__ import annotations

import re
from dataclasses import dataclass

from models import Truck, TruckKit
from schedule import ScheduleInsights, build_schedule_insights
from stages import Stage, stage_from_id, stage_label


@dataclass
class NextMainKitRisk:
    is_warning: bool
    message: str


@dataclass
class BendBufferHealth:
    kit_count: int
    level: str


@dataclass
class WeldFeedStatus:
    score: float
    level: str


@dataclass
class AttentionItem:
    priority: int
    title: str
    detail: str


@dataclass
class DashboardMetrics:
    next_main_kit_risk: NextMainKitRisk
    bend_buffer: BendBufferHealth
    weld_feed: WeldFeedStatus
    attention_items: list[AttentionItem]


@dataclass
class BossTile:
    key: str
    label: str
    value: str
    detail: str
    tone: str


@dataclass
class BossSyncSummary:
    ahead_kits: int
    in_sync_kits: int
    behind_kits: int


@dataclass
class BossReleaseSummary:
    summary: str
    late_releases: int
    next_main_released: bool


@dataclass
class BossTruckRow:
    truck_number: str
    main_stage: str
    sync_status: str
    main_released: str
    risk_category: str
    issue_summary: str
    tone: str


@dataclass
class BossLensMetrics:
    tiles: list[BossTile]
    sync_summary: BossSyncSummary
    release_summary: BossReleaseSummary
    flow_summary: str
    truck_rows: list[BossTruckRow]


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

    next_main_kit_risk = _compute_next_main_kit_risk(ordered_trucks)
    bend_buffer = _compute_bend_buffer(ordered_trucks)
    weld_feed = _compute_weld_feed(ordered_trucks)
    attention_items = _build_attention_items(
        next_main_kit_risk=next_main_kit_risk,
        bend_buffer=bend_buffer,
        weld_feed=weld_feed,
        schedule_insights=insights,
    )

    return DashboardMetrics(
        next_main_kit_risk=next_main_kit_risk,
        bend_buffer=bend_buffer,
        weld_feed=weld_feed,
        attention_items=attention_items,
    )


def compute_boss_lens_metrics(
    trucks: list[Truck],
    schedule_insights: ScheduleInsights | None = None,
    dashboard_metrics: DashboardMetrics | None = None,
) -> BossLensMetrics:
    ordered_trucks = sort_trucks_natural(trucks)
    insights = schedule_insights or build_schedule_insights(ordered_trucks)
    metrics = dashboard_metrics or compute_dashboard_metrics(ordered_trucks, schedule_insights=insights)

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
    blocked_kits = 0
    truck_rows: list[BossTruckRow] = []

    for truck in ordered_trucks:
        truck_id = truck.id
        truck_start_week = insights.truck_planned_start_week_by_id.get(int(truck_id or -1))

        truck_blocked_count = 0
        truck_behind_count = 0
        main_sync_status = "In Sync"
        main_kit = _find_main_body_kit(truck)
        main_stage = "-"
        main_released = "No"

        for kit in truck.kits:
            if not kit.is_active:
                continue
            if str(kit.blocker or "").strip():
                blocked_kits += 1
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
            released = (
                main_kit.release_state == "released"
                or stage_from_id(main_kit.front_stage_id) > Stage.RELEASE
            )
            main_released = "Yes" if released else "No"

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
            BossTruckRow(
                truck_number=truck.truck_number,
                main_stage=main_stage,
                sync_status=main_sync_status,
                main_released=main_released,
                risk_category=risk_category,
                issue_summary=issue_summary,
                tone=tone,
            )
        )

    late_releases = len(insights.release_hold_items)
    next_main_released = not metrics.next_main_kit_risk.is_warning

    release_bits: list[str] = []
    if late_releases > 0:
        release_bits.append(f"{late_releases} kit(s) late release")
    if not next_main_released:
        release_bits.append("next truck body not released")
    release_summary_text = "Main kit release on time." if not release_bits else "; ".join(release_bits) + "."

    bend_health = metrics.bend_buffer.level.upper()
    weld_health = metrics.weld_feed.level.upper()
    active_trucks = len(ordered_trucks)

    tiles = [
        BossTile(
            key="active_trucks",
            label="Active Trucks",
            value=str(active_trucks),
            detail="Current trucks in active flow.",
            tone="ok" if active_trucks > 0 else "caution",
        ),
        BossTile(
            key="next_main_released",
            label="Next Main Kit Released",
            value="Yes" if next_main_released else "No",
            detail=metrics.next_main_kit_risk.message,
            tone="ok" if next_main_released else "problem",
        ),
        BossTile(
            key="bend_buffer",
            label="Bend Buffer Health",
            value=bend_health,
            detail="3+ released kits in laser/bend is healthy.",
            tone=_tone_for_buffer(metrics.bend_buffer.level),
        ),
        BossTile(
            key="weld_feed",
            label="Weld Feed Health",
            value=weld_health,
            detail="Flow readiness from bend into weld.",
            tone=_tone_for_weld(metrics.weld_feed.level),
        ),
        BossTile(
            key="behind_kits",
            label="Kits Behind Master Schedule",
            value=str(behind_kits),
            detail="Compared to fixed schedule baseline.",
            tone=_tone_for_count(behind_kits),
        ),
        BossTile(
            key="late_releases",
            label="Late Releases",
            value=str(late_releases),
            detail="Kits still not released past planned start.",
            tone=_tone_for_count(late_releases),
        ),
        BossTile(
            key="blocked_kits",
            label="Blocked Kits",
            value=str(blocked_kits),
            detail="Kits with blocker text present.",
            tone=_tone_for_count(blocked_kits),
        ),
    ]

    return BossLensMetrics(
        tiles=tiles,
        sync_summary=BossSyncSummary(
            ahead_kits=ahead_kits,
            in_sync_kits=in_sync_kits,
            behind_kits=behind_kits,
        ),
        release_summary=BossReleaseSummary(
            summary=release_summary_text,
            late_releases=late_releases,
            next_main_released=next_main_released,
        ),
        flow_summary=(
            f"Bend Buffer={bend_health} | Weld Feed={weld_health} | "
            f"Active Trucks={active_trucks} | Blocked Kits={blocked_kits}"
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


def _tone_for_count(value: int) -> str:
    if value <= 0:
        return "ok"
    if value <= 2:
        return "caution"
    return "problem"


def _tone_for_buffer(level: str) -> str:
    if level == "healthy":
        return "ok"
    if level == "low":
        return "caution"
    return "problem"


def _tone_for_weld(level: str) -> str:
    if level == "healthy":
        return "ok"
    if level == "watch":
        return "caution"
    return "problem"


def _compute_next_main_kit_risk(trucks: list[Truck]) -> NextMainKitRisk:
    if len(trucks) < 2:
        return NextMainKitRisk(is_warning=False, message="Not enough trucks in flow.")

    for index in range(len(trucks) - 1):
        current_truck = trucks[index]
        next_truck = trucks[index + 1]
        current_main_kit = _find_main_body_kit(current_truck)
        next_main_kit = _find_main_body_kit(next_truck)

        if not current_main_kit or not next_main_kit:
            continue

        current_front = stage_from_id(current_main_kit.front_stage_id)
        if current_front == Stage.WELD and next_main_kit.release_state != "released":
            return NextMainKitRisk(
                is_warning=True,
                message=f"{next_truck.truck_number} Body is {next_main_kit.release_state.replace('_', ' ')}.",
            )

    return NextMainKitRisk(is_warning=False, message="Next Body release is aligned.")


def _compute_bend_buffer(trucks: list[Truck]) -> BendBufferHealth:
    front_buffer_count = 0
    has_body_tail_in_buffer = False

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

            if is_body:
                back_stage = stage_from_id(kit.back_stage_id)
                if back_stage in {Stage.LASER, Stage.BEND}:
                    has_body_tail_in_buffer = True

    if front_buffer_count >= 3:
        count = front_buffer_count
        level = "healthy"
    elif front_buffer_count > 0:
        count = front_buffer_count
        level = "low"
    elif has_body_tail_in_buffer:
        # Prevent a hard "dry" signal when the body tail is still feeding laser/bend.
        count = 1
        level = "low"
    else:
        count = 0
        level = "dry"

    return BendBufferHealth(kit_count=count, level=level)


def _compute_weld_feed(trucks: list[Truck]) -> WeldFeedStatus:
    score = 0.0
    for truck in trucks:
        for kit in truck.kits:
            if not kit.is_active:
                continue
            front_stage = stage_from_id(kit.front_stage_id)
            if front_stage in {Stage.BEND, Stage.WELD}:
                score += 1.0

    if score < 2.0:
        level = "low"
    elif score < 4.0:
        level = "watch"
    else:
        level = "healthy"

    return WeldFeedStatus(score=round(score, 1), level=level)


def _build_attention_items(
    next_main_kit_risk: NextMainKitRisk,
    bend_buffer: BendBufferHealth,
    weld_feed: WeldFeedStatus,
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
                    f"oldest {oldest.hold_weeks:.1f} week(s) "
                    f"({oldest.truck_number} {oldest.kit_name})."
                ),
            )
        )

    if next_main_kit_risk.is_warning:
        items.append(
            AttentionItem(
                priority=100,
                title="Next Body not released",
                detail=next_main_kit_risk.message,
            )
        )

    if weld_feed.level == "low":
        items.append(
            AttentionItem(
                priority=90,
                title="Weld feed low",
                detail="Insufficient active kits are feeding weld from bend.",
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

    operation_standards = schedule_insights.operation_standards
    overloaded_stages = [stage_label(op.stage_id).upper() for op in operation_standards if op.spare_days < 0.0]
    low_spare_stages = [
        stage_label(op.stage_id).upper() for op in operation_standards if 0.0 <= op.spare_days < 1.0
    ]

    if overloaded_stages:
        items.append(
            AttentionItem(
                priority=88,
                title="Operation standard is overbooked",
                detail=("Planned work exceeds available duration in: " + ", ".join(overloaded_stages) + "."),
            )
        )
    elif low_spare_stages:
        items.append(
            AttentionItem(
                priority=72,
                title="Spare capacity is tight",
                detail=("Less than 1 spare day in: " + ", ".join(low_spare_stages) + "."),
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
