from __future__ import annotations

import re

from models import Truck, TruckKit
from stages import Stage, stage_from_id


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


def is_truck_complete(truck: Truck) -> bool:
    active_kits = [kit for kit in truck.kits if kit.is_active]
    if not active_kits:
        return False
    return all(stage_from_id(kit.front_stage_id) == Stage.COMPLETE for kit in active_kits)


def filter_dashboard_trucks(
    trucks: list[Truck],
    *,
    include_completed: bool = False,
) -> list[Truck]:
    return [
        truck
        for truck in sort_trucks_natural(list(trucks))
        if truck.is_visible and (include_completed or not is_truck_complete(truck))
    ]


def completing_kit_would_finish_truck(
    truck: Truck,
    *,
    kit_id: int | None,
    target_stage_id: int | Stage | None,
) -> bool:
    if stage_from_id(target_stage_id) != Stage.COMPLETE:
        return False

    active_kits = [kit for kit in truck.kits if kit.is_active]
    if not active_kits or kit_id is None:
        return False

    target_kit: TruckKit | None = None
    for kit in active_kits:
        if kit.id is not None and int(kit.id) == int(kit_id):
            target_kit = kit
            break

    if target_kit is None:
        return False
    if stage_from_id(target_kit.front_stage_id) == Stage.COMPLETE:
        return False

    return all(
        kit is target_kit or stage_from_id(kit.front_stage_id) == Stage.COMPLETE
        for kit in active_kits
    )


def normalize_blocked_state(
    *,
    blocked: bool | None = None,
    blocked_reason: str | None = None,
    blocker: str | None = None,
) -> tuple[bool, str]:
    blocker_text = str(blocker or "").strip()
    reason_text = str(blocked_reason or "").strip()
    normalized_blocked = bool(blocked) if blocked is not None else bool(reason_text or blocker_text)
    if not normalized_blocked:
        return (False, "")
    return (True, reason_text or blocker_text or "Blocked")


def normalize_blocked_state_from_kit(kit: TruckKit) -> tuple[bool, str]:
    return normalize_blocked_state(
        blocked=bool(getattr(kit, "blocked", False)),
        blocked_reason=str(getattr(kit, "blocked_reason", "") or ""),
        blocker=str(getattr(kit, "blocker", "") or ""),
    )


def signal_state_for_level(level: str, *, family: str) -> str:
    normalized = str(level or "").strip().lower()
    if family in {"laser", "brake"}:
        if normalized == "healthy":
            return "green"
        if normalized == "low":
            return "yellow"
        return "red"
    if normalized == "healthy":
        return "green"
    if normalized == "watch":
        return "yellow"
    return "red"
