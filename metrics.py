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


def _find_main_body_kit(truck: Truck) -> TruckKit | None:
    for kit in truck.kits:
        if not kit.is_active:
            continue
        if kit.is_main_kit or kit.kit_name.lower() == "body":
            return kit
    return None


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
    count = 0
    has_body_in_buffer = False
    for truck in trucks:
        for kit in truck.kits:
            if not kit.is_active:
                continue
            if kit.release_state == "not_released":
                continue
            front_stage = stage_from_id(kit.front_stage_id)
            if front_stage in {Stage.LASER, Stage.BEND}:
                count += 1
                if kit.is_main_kit or kit.kit_name.strip().lower() == "body":
                    has_body_in_buffer = True

    if count == 0:
        level = "empty"
    elif count <= 2 and not has_body_in_buffer:
        level = "low"
    elif count <= 2:
        level = "watch"
    else:
        level = "healthy"

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
                detail=f"Estimated weld feed score is {weld_feed.score}.",
            )
        )

    laser_label = stage_label(Stage.LASER).lower()
    bend_label = stage_label(Stage.BEND).lower()
    if bend_buffer.level == "empty":
        items.append(
            AttentionItem(
                priority=85,
                title="Bend buffer empty",
                detail=f"No released kits are in {laser_label}/{bend_label}.",
            )
        )
    elif bend_buffer.level == "low":
        items.append(
            AttentionItem(
                priority=80,
                title="Bend buffer low",
                detail=f"Only {bend_buffer.kit_count} kit(s) are approaching {bend_label}.",
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
