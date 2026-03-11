from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from metrics import DashboardMetrics
from models import Truck, TruckKit
from schedule import ScheduleInsights
from stages import STAGE_SEQUENCE, Stage, stage_from_id, stage_label


def _tone_to_adaptive_color(tone: str) -> str:
    normalized = str(tone or "").strip().lower()
    if normalized in {"ok", "healthy"}:
        return "Good"
    if normalized in {"watch", "caution"}:
        return "Warning"
    if normalized in {"low", "empty", "warning", "problem"}:
        return "Attention"
    return "Default"


def _tile_column(label: str, value: str, detail: str, tone: str) -> dict[str, Any]:
    return {
        "type": "Column",
        "width": "stretch",
        "items": [
            {
                "type": "TextBlock",
                "text": label,
                "size": "Small",
                "isSubtle": True,
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": value,
                "weight": "Bolder",
                "size": "Medium",
                "color": _tone_to_adaptive_color(tone),
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": detail,
                "size": "Small",
                "wrap": True,
                "spacing": "None",
            },
        ],
    }


def _find_main_body_kit(truck: Truck) -> TruckKit | None:
    for kit in truck.kits:
        if not kit.is_active:
            continue
        if kit.is_main_kit or str(kit.kit_name).strip().lower() == "body":
            return kit
    return None


def _count_active_kits(trucks: list[Truck]) -> int:
    return sum(1 for truck in trucks for kit in truck.kits if kit.is_active)


def _count_blocked_kits(trucks: list[Truck]) -> int:
    return sum(1 for truck in trucks for kit in truck.kits if kit.is_active and str(kit.blocker).strip())


def _build_holds_by_truck(schedule_insights: ScheduleInsights) -> dict[str, int]:
    by_truck: dict[str, int] = {}
    for item in schedule_insights.release_hold_items:
        key = str(item.truck_number or "").strip()
        if not key:
            continue
        by_truck[key] = by_truck.get(key, 0) + 1
    return by_truck


def _build_stage_load_text(truck: Truck) -> str:
    counts = {stage: 0 for stage in STAGE_SEQUENCE}
    for kit in truck.kits:
        if not kit.is_active:
            continue
        counts[stage_from_id(kit.front_stage_id)] += 1
    parts = [f"{stage_label(stage)} {counts[stage]}" for stage in STAGE_SEQUENCE]
    return " | ".join(parts)


def _build_truck_risk_text(truck: Truck, hold_count: int) -> tuple[str, str]:
    blocked_count = sum(1 for kit in truck.kits if kit.is_active and str(kit.blocker).strip())
    if hold_count > 0:
        return (f"Late Release: {hold_count} kit(s) late release.", "problem")
    if blocked_count > 0:
        return (f"Blocked: {blocked_count} kit(s) blocked.", "problem")
    return ("No immediate issue.", "ok")


def build_dashboard_adaptive_card(
    *,
    trucks: list[Truck],
    dashboard_metrics: DashboardMetrics,
    schedule_insights: ScheduleInsights,
    max_trucks: int = 20,
    max_attention: int = 8,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(timezone.utc)
    generated_text = generated.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    active_kits = _count_active_kits(trucks)
    blocked_kits = _count_blocked_kits(trucks)
    late_releases = len(schedule_insights.release_hold_items)
    next_main_ready = not dashboard_metrics.next_main_kit_risk.is_warning

    summary_tiles = [
        (
            "Active Trucks",
            str(len(trucks)),
            "Visible active trucks on the board.",
            "ok" if trucks else "caution",
        ),
        (
            "Active Kits",
            str(active_kits),
            "Active kits across all displayed trucks.",
            "ok" if active_kits else "caution",
        ),
        (
            "Engineering Holds",
            str(late_releases),
            "Not released past planned start.",
            "problem" if late_releases > 0 else "ok",
        ),
        (
            "Blocked Kits",
            str(blocked_kits),
            "Kits with blocker text.",
            "problem" if blocked_kits > 0 else "ok",
        ),
        (
            "Next Main Kit",
            "Ready" if next_main_ready else "At Risk",
            dashboard_metrics.next_main_kit_risk.message,
            "ok" if next_main_ready else "problem",
        ),
        (
            "Bend Buffer",
            dashboard_metrics.bend_buffer.level.upper(),
            f"{dashboard_metrics.bend_buffer.kit_count} kit(s) in laser/bend.",
            dashboard_metrics.bend_buffer.level,
        ),
        (
            "Weld Feed",
            dashboard_metrics.weld_feed.level.upper(),
            f"Score {dashboard_metrics.weld_feed.score:.1f}.",
            dashboard_metrics.weld_feed.level,
        ),
    ]

    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Fabrication Flow Dashboard - Operations Snapshot",
            "size": "Large",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": f"Generated: {generated_text}",
            "isSubtle": True,
            "spacing": "None",
            "wrap": True,
        },
    ]

    for start in range(0, len(summary_tiles), 4):
        chunk = summary_tiles[start : start + 4]
        body.append(
            {
                "type": "ColumnSet",
                "columns": [
                    _tile_column(
                        label=tile[0],
                        value=tile[1],
                        detail=tile[2],
                        tone=tile[3],
                    )
                    for tile in chunk
                ],
                "spacing": "Medium",
            }
        )

    body.append(
        {
            "type": "TextBlock",
            "text": "Attention",
            "weight": "Bolder",
            "spacing": "Medium",
            "wrap": True,
        }
    )
    attention_items = dashboard_metrics.attention_items[: max(1, int(max_attention))]
    for item in attention_items:
        tone = "problem" if item.priority >= 90 else ("caution" if item.priority >= 70 else "ok")
        body.append(
            {
                "type": "TextBlock",
                "text": f"- {item.title}: {item.detail}",
                "color": _tone_to_adaptive_color(tone),
                "wrap": True,
                "spacing": "Small",
            }
        )

    holds_by_truck = _build_holds_by_truck(schedule_insights)
    body.append(
        {
            "type": "TextBlock",
            "text": "Per-Truck Board Snapshot",
            "weight": "Bolder",
            "spacing": "Medium",
            "wrap": True,
        }
    )

    visible_trucks = trucks[: max(1, int(max_trucks))]
    for truck in visible_trucks:
        main_kit = _find_main_body_kit(truck)
        if main_kit:
            main_stage = stage_label(main_kit.front_stage_id)
            main_released = (
                main_kit.release_state == "released"
                or stage_from_id(main_kit.front_stage_id) > Stage.RELEASE
            )
            main_text = f"{main_stage} ({'released' if main_released else 'not released'})"
        else:
            main_text = "N/A"

        risk_text, risk_tone = _build_truck_risk_text(
            truck,
            hold_count=holds_by_truck.get(truck.truck_number, 0),
        )

        body.append(
            {
                "type": "Container",
                "separator": True,
                "spacing": "Small",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": f"{truck.truck_number} | Main Body: {main_text}",
                        "weight": "Bolder",
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": "Stage Load: " + _build_stage_load_text(truck),
                        "wrap": True,
                        "spacing": "None",
                    },
                    {
                        "type": "TextBlock",
                        "text": "Risk: " + risk_text,
                        "color": _tone_to_adaptive_color(risk_tone),
                        "wrap": True,
                        "spacing": "None",
                    },
                ],
            }
        )

    remaining = len(trucks) - len(visible_trucks)
    if remaining > 0:
        body.append(
            {
                "type": "TextBlock",
                "text": f"+{remaining} more truck(s) not shown.",
                "isSubtle": True,
                "wrap": True,
            }
        )

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
    }


def build_teams_webhook_payload(
    *,
    trucks: list[Truck],
    dashboard_metrics: DashboardMetrics,
    schedule_insights: ScheduleInsights,
    max_trucks: int = 20,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    card = build_dashboard_adaptive_card(
        trucks=trucks,
        dashboard_metrics=dashboard_metrics,
        schedule_insights=schedule_insights,
        max_trucks=max_trucks,
        generated_at=generated_at,
    )
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }

