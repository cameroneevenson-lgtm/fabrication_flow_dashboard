from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
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


def _kit_stage_text(kit: TruckKit) -> str:
    release_text = "released" if kit.release_state == "released" else "not released"
    front = stage_label(kit.front_stage_id)
    back = stage_label(kit.back_stage_id)
    blocked = " | blocked" if str(kit.blocker or "").strip() else ""
    return f"{release_text} | {front}->{back}{blocked}"


def _current_week_of_label() -> str:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday.strftime("%b %d, %Y")


def _week_value_to_date_label(week_value: float, current_week: float) -> str:
    today = date.today()
    current_monday = today - timedelta(days=today.weekday())
    delta_days = (float(week_value) - float(current_week)) * 7.0
    target_date = current_monday + timedelta(days=delta_days)
    return target_date.strftime("%b %d, %Y")


def _normalize_week_around_current(week_value: float, current_week: float) -> float:
    value = float(week_value)
    current = float(current_week)
    cycle = 52.0
    while (value - current) > 26.0:
        value -= cycle
    while (current - value) > 26.0:
        value += cycle
    return value


def _safe_toggle_id(value: str, fallback_index: int) -> str:
    clean = "".join(ch if ch.isalnum() else "_" for ch in str(value or "").strip())
    clean = clean.strip("_")
    if not clean:
        clean = f"truck_{fallback_index}"
    return f"truck_details_{clean.lower()}"


def _week_to_index(week_value: float, min_week: float, max_week: float, width: int) -> int:
    if width <= 1:
        return 0
    span = max(0.0001, float(max_week - min_week))
    ratio = (float(week_value) - min_week) / span
    idx = int(round(ratio * float(width - 1)))
    return max(0, min(width - 1, idx))


def _render_gantt_png_data_uri(
    timeline_rows: list[tuple[Truck, dict[Stage, tuple[float, float]]]],
    *,
    current_week: float,
    min_week: float,
    max_week: float,
) -> str | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import pyplot as plt
    except Exception:
        return None

    if not timeline_rows:
        return None

    ordered_rows = list(reversed(timeline_rows))
    fig_width = 10.0
    fig_height = max(2.4, 0.45 * len(ordered_rows) + 1.2)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=120)

    colors = {
        Stage.RELEASE: "#9CA3AF",
        Stage.LASER: "#93C5FD",
        Stage.BEND: "#FDE68A",
        Stage.WELD: "#FCA5A5",
        Stage.COMPLETE: "#86EFAC",
    }
    stage_marker = {
        Stage.RELEASE: "R",
        Stage.LASER: "L",
        Stage.BEND: "B",
        Stage.WELD: "W",
        Stage.COMPLETE: "C",
    }

    y_positions = list(range(len(ordered_rows)))
    labels: list[str] = []
    for y, (truck, windows) in enumerate(ordered_rows):
        labels.append(str(truck.truck_number))
        if not windows:
            continue

        first_start = min(start for start, _end in windows.values())
        last_end = max(end for _start, end in windows.values())

        release_left = max(min_week, first_start - 0.35)
        release_width = max(0.08, first_start - release_left)
        ax.barh(y, release_width, left=release_left, height=0.34, color=colors[Stage.RELEASE], alpha=0.8)

        for stage in (Stage.LASER, Stage.BEND, Stage.WELD):
            bounds = windows.get(stage)
            if bounds is None:
                continue
            start_week, end_week = bounds
            width = max(0.08, end_week - start_week)
            ax.barh(y, width, left=start_week, height=0.34, color=colors[stage], alpha=0.95)

        complete_right = min(max_week, last_end + 0.35)
        complete_width = max(0.08, complete_right - last_end)
        ax.barh(y, complete_width, left=last_end, height=0.34, color=colors[Stage.COMPLETE], alpha=0.9)

        body_kit = _find_main_body_kit(truck)
        if body_kit is not None:
            actual_stage = stage_from_id(body_kit.front_stage_id)
            is_released = str(body_kit.release_state or "").strip().lower() == "released"
            has_blocker = bool(str(body_kit.blocker or "").strip())
            marker_week: float | None = None
            stage_bounds = windows.get(actual_stage)
            if stage_bounds is not None:
                marker_week = (stage_bounds[0] + stage_bounds[1]) / 2.0
            elif actual_stage == Stage.RELEASE:
                marker_week = first_start
            elif actual_stage == Stage.COMPLETE:
                marker_week = last_end

            if marker_week is not None:
                marker_week = max(min_week, min(max_week, marker_week))
                if has_blocker:
                    marker_color = "#F59E0B"
                elif is_released:
                    marker_color = "#16A34A"
                else:
                    marker_color = "#DC2626"
                ax.scatter([marker_week], [y], s=28, c=marker_color, marker="o", zorder=6)

    ax.axvline(float(current_week), color="#DC2626", linestyle="--", linewidth=1.2, zorder=2)
    ax.set_xlim(float(min_week), float(max_week))
    ax.set_yticks(y_positions)
    ax.set_yticklabels(labels, fontsize=8)
    tick_count = 6
    ticks = [
        float(min_week + ((max_week - min_week) * i / max(1, tick_count - 1)))
        for i in range(tick_count)
    ]
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [_week_value_to_date_label(value, current_week) for value in ticks],
        fontsize=8,
    )
    ax.grid(axis="x", color="#CBD5E1", linewidth=0.7, alpha=0.7)
    ax.set_xlabel("Week of", fontsize=8)
    ax.set_facecolor("#FFFFFF")
    fig.patch.set_facecolor("#FFFFFF")
    fig.tight_layout(pad=0.9)

    try:
        buffer = BytesIO()
        fig.savefig(buffer, format="png")
        raw = buffer.getvalue()
    finally:
        plt.close(fig)

    if not raw:
        return None
    if len(raw) > 350_000:
        return None

    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _build_scheduled_vs_actual_gantt_items(
    trucks: list[Truck],
    schedule_insights: ScheduleInsights,
    *,
    max_rows: int,
    chart_width: int = 26,
) -> list[dict[str, Any]]:
    body_windows: dict[Stage, tuple[float, float]] = {}
    for window in schedule_insights.kit_operation_windows:
        if str(window.kit_name or "").strip().lower() != "body":
            continue
        stage = stage_from_id(window.stage_id)
        body_windows[stage] = (float(window.start_week), float(window.end_week))

    if not body_windows:
        return [
            {
                "type": "TextBlock",
                "text": "Scheduled vs Actual Gantt is unavailable (no body schedule windows configured).",
                "isSubtle": True,
                "wrap": True,
                "spacing": "Small",
            }
        ]

    timeline_rows: list[tuple[Truck, dict[Stage, tuple[float, float]]]] = []
    for truck in trucks[: max(1, int(max_rows))]:
        if truck.id is None:
            continue
        truck_start_week = schedule_insights.truck_planned_start_week_by_id.get(int(truck.id))
        if truck_start_week is None:
            continue
        absolute_windows: dict[Stage, tuple[float, float]] = {}
        for stage, (start_week, end_week) in body_windows.items():
            start_value = _normalize_week_around_current(
                truck_start_week + start_week,
                schedule_insights.current_week,
            )
            end_value = _normalize_week_around_current(
                truck_start_week + end_week,
                schedule_insights.current_week,
            )
            if end_value < start_value:
                end_value = start_value
            absolute_windows[stage] = (start_value, end_value)
        timeline_rows.append((truck, absolute_windows))

    if not timeline_rows:
        return [
            {
                "type": "TextBlock",
                "text": "Scheduled vs Actual Gantt is unavailable (no truck schedule anchors).",
                "isSubtle": True,
                "wrap": True,
                "spacing": "Small",
            }
        ]

    min_week = min(start for _truck, windows in timeline_rows for start, _end in windows.values())
    max_week = max(
        float(schedule_insights.current_week),
        max(end for _truck, windows in timeline_rows for _start, end in windows.values()),
    )
    if max_week <= min_week:
        max_week = min_week + 1.0

    image_url = _render_gantt_png_data_uri(
        timeline_rows,
        current_week=float(schedule_insights.current_week),
        min_week=float(min_week),
        max_week=float(max_week),
    )
    if image_url:
        return [
            {
                "type": "TextBlock",
                "text": "Master Schedule vs Actual",
                "weight": "Bolder",
                "spacing": "Medium",
                "wrap": True,
            },
            {
                "type": "Image",
                "url": image_url,
                "size": "Stretch",
                "altText": "Body scheduled vs actual gantt",
                "spacing": "Small",
            },
            {
                "type": "TextBlock",
                "text": (
                    "Red dashed line = current week. "
                    "Black marker = current stage."
                ),
                "isSubtle": True,
                "spacing": "Small",
                "wrap": True,
            },
        ]

    now_idx = _week_to_index(float(schedule_insights.current_week), min_week, max_week, chart_width)

    items: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Master Schedule vs Actual",
            "weight": "Bolder",
            "spacing": "Medium",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": "Legend: R release, L laser, B bend, W weld, C complete, | current week",
            "isSubtle": True,
            "spacing": "None",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": (
                f"Range: Week of {_week_value_to_date_label(min_week, schedule_insights.current_week)}"
                f" to Week of {_week_value_to_date_label(max_week, schedule_insights.current_week)}"
            ),
            "isSubtle": True,
            "spacing": "None",
            "wrap": True,
        },
    ]

    scheduled_fill_map: list[tuple[Stage, str]] = [
        (Stage.LASER, "L"),
        (Stage.BEND, "B"),
        (Stage.WELD, "W"),
    ]
    actual_marker_map: dict[Stage, str] = {
        Stage.RELEASE: "R",
        Stage.LASER: "L",
        Stage.BEND: "B",
        Stage.WELD: "W",
        Stage.COMPLETE: "C",
    }

    for truck, windows in timeline_rows:
        scheduled = ["."] * chart_width
        actual = ["."] * chart_width

        first_start = min(start for start, _end in windows.values())
        last_end = max(end for _start, end in windows.values())
        scheduled[_week_to_index(first_start, min_week, max_week, chart_width)] = "R"
        scheduled[_week_to_index(last_end, min_week, max_week, chart_width)] = "C"

        for stage, char in scheduled_fill_map:
            bounds = windows.get(stage)
            if bounds is None:
                continue
            start_week, end_week = bounds
            start_idx = _week_to_index(start_week, min_week, max_week, chart_width)
            end_idx = _week_to_index(end_week, min_week, max_week, chart_width)
            if end_idx < start_idx:
                end_idx = start_idx
            for idx in range(start_idx, end_idx + 1):
                scheduled[idx] = char

        body_kit = _find_main_body_kit(truck)
        if body_kit is not None:
            actual_stage = stage_from_id(body_kit.front_stage_id)
            marker = actual_marker_map.get(actual_stage)
            marker_week: float | None = None
            stage_bounds = windows.get(actual_stage)
            if stage_bounds is not None:
                marker_week = (stage_bounds[0] + stage_bounds[1]) / 2.0
            elif actual_stage == Stage.RELEASE:
                marker_week = first_start
            elif actual_stage == Stage.COMPLETE:
                marker_week = last_end
            if marker_week is not None and marker:
                actual[_week_to_index(marker_week, min_week, max_week, chart_width)] = marker

        if scheduled[now_idx] == ".":
            scheduled[now_idx] = "|"
        if actual[now_idx] == ".":
            actual[now_idx] = "|"

        items.append(
            {
                "type": "TextBlock",
                "text": str(truck.truck_number),
                "weight": "Bolder",
                "spacing": "Small",
                "wrap": True,
            }
        )
        items.append(
            {
                "type": "TextBlock",
                "text": f"Sched : {''.join(scheduled)}",
                "fontType": "Monospace",
                "spacing": "None",
                "wrap": True,
            }
        )
        items.append(
            {
                "type": "TextBlock",
                "text": f"Actual: {''.join(actual)}",
                "fontType": "Monospace",
                "spacing": "None",
                "wrap": True,
            }
        )

    return items


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
    next_body_ready = not dashboard_metrics.next_main_kit_risk.is_warning

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
            "Next Body",
            "Ready" if next_body_ready else "At Risk",
            dashboard_metrics.next_main_kit_risk.message,
            "ok" if next_body_ready else "problem",
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
            "text": "Fabrication Flow Dashboard - Live Board View",
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
        {
            "type": "TextBlock",
            "text": (
                f"Week of {_current_week_of_label()} | "
                f"Engineering Holds {late_releases} | "
                f"Blocked Kits {blocked_kits}"
            ),
            "wrap": True,
            "spacing": "Small",
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

    body.extend(
        _build_scheduled_vs_actual_gantt_items(
            trucks=trucks,
            schedule_insights=schedule_insights,
            max_rows=max(1, int(max_trucks)),
        )
    )

    attention_items_block: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Live Attention Queue",
            "weight": "Bolder",
            "spacing": "Medium",
            "wrap": True,
        }
    ]
    attention_items = dashboard_metrics.attention_items[: max(1, int(max_attention))]
    for item in attention_items:
        tone = "problem" if item.priority >= 90 else ("caution" if item.priority >= 70 else "ok")
        attention_items_block.append(
            {
                "type": "TextBlock",
                "text": f"- {item.title}: {item.detail}",
                "color": _tone_to_adaptive_color(tone),
                "wrap": True,
                "spacing": "Small",
            }
        )
    body.append(
        {
            "type": "Container",
            "id": "attention_section",
            "isVisible": True,
            "items": attention_items_block,
        }
    )

    holds_by_truck = _build_holds_by_truck(schedule_insights)
    truck_lane_items: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Truck Board Lanes",
            "weight": "Bolder",
            "spacing": "Medium",
            "wrap": True,
        }
    ]

    visible_trucks = trucks[: max(1, int(max_trucks))]
    for index, truck in enumerate(visible_trucks, start=1):
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
        details_id = _safe_toggle_id(truck.truck_number, index)

        truck_lane_items.append(
            {
                "type": "Container",
                "separator": True,
                "spacing": "Small",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": f"{truck.truck_number} | Body: {main_text}",
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
                    {
                        "type": "ActionSet",
                        "actions": [
                            {
                                "type": "Action.ToggleVisibility",
                                "title": "Show/Hide Kit Details",
                                "targetElements": [details_id],
                            }
                        ],
                        "spacing": "Small",
                    },
                    {
                        "type": "Container",
                        "id": details_id,
                        "isVisible": False,
                        "items": [
                            {
                                "type": "FactSet",
                                "facts": [
                                    {
                                        "title": str(kit.kit_name),
                                        "value": _kit_stage_text(kit),
                                    }
                                    for kit in truck.kits
                                    if kit.is_active
                                ],
                                "spacing": "Small",
                            }
                        ],
                    },
                ],
            }
        )
    body.append(
        {
            "type": "Container",
            "id": "truck_lanes_section",
            "isVisible": True,
            "items": truck_lane_items,
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


def build_teams_gantt_only_webhook_payload(
    *,
    trucks: list[Truck],
    schedule_insights: ScheduleInsights,
    max_trucks: int = 20,
    mention_name: str = "cevenson",
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(timezone.utc)
    generated_text = generated.astimezone().strftime("%Y-%m-%d %H:%M %Z")

    mention = str(mention_name or "").strip()
    mention_token = f"<at>{mention}</at>" if mention else ""

    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Master Schedule vs Actual",
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
    if mention_token:
        body.append(
            {
                "type": "TextBlock",
                "text": mention_token,
                "wrap": True,
                "spacing": "Small",
            }
        )

    body.extend(
        _build_scheduled_vs_actual_gantt_items(
            trucks=trucks,
            schedule_insights=schedule_insights,
            max_rows=max(1, int(max_trucks)),
        )
    )

    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
    }
    if mention_token:
        card["msteams"] = {
            "width": "Full",
            "entities": [
                {
                    "type": "mention",
                    "text": mention_token,
                    "mentioned": {
                        "id": mention,
                        "name": mention,
                    },
                }
            ],
        }

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

