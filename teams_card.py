from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Any

from gantt_overlay import (
    OverlayRow,
    build_overlay_rows,
    compute_overlay_viewport,
    render_overlay_png,
)
from metrics import DashboardMetrics
from models import Truck, TruckKit
from schedule import ScheduleInsights
from stages import STAGE_SEQUENCE, Stage, stage_from_id, stage_label

TEAMS_GANTT_MAX_PNG_BYTES = 18_000


def _tone_to_adaptive_color(tone: str) -> str:
    normalized = str(tone or "").strip().lower()
    if normalized in {"ok", "healthy"}:
        return "Good"
    if normalized in {"watch", "caution"}:
        return "Warning"
    if normalized in {"low", "dry", "empty", "warning", "problem"}:
        return "Attention"
    return "Default"


def _tile_column(label: str, value: str, detail: str, tone: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = [
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
    ]
    if str(detail or "").strip():
        items.append(
            {
                "type": "TextBlock",
                "text": detail,
                "size": "Small",
                "wrap": True,
                "spacing": "None",
            }
        )
    return {
        "type": "Column",
        "width": "stretch",
        "items": items,
    }


def _signal_emoji(level: str, *, family: str) -> str:
    normalized = str(level or "").strip().lower()
    if family in {"laser", "brake"}:
        if normalized == "healthy":
            return "🟢"
        if normalized == "low":
            return "🟡"
        return "🔴"
    if normalized == "healthy":
        return "🟢"
    if normalized == "watch":
        return "🟡"
    return "🔴"


def _signal_column(label: str, emoji: str) -> dict[str, Any]:
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
                "horizontalAlignment": "Center",
            },
            {
                "type": "TextBlock",
                "text": emoji,
                "size": "ExtraLarge",
                "wrap": True,
                "horizontalAlignment": "Center",
            },
        ],
    }


def _signal_state(level: str, *, family: str) -> str:
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


def _signal_light_column(label: str, state: str) -> dict[str, Any]:
    color_by_light = {
        "red": "Attention",
        "yellow": "Warning",
        "green": "Good",
    }
    inlines: list[dict[str, Any]] = []
    for index, light in enumerate(("red", "yellow", "green")):
        inlines.append(
            {
                "type": "TextRun",
                "text": "●",
                "color": color_by_light[light],
                "isSubtle": light != state,
                "weight": "Bolder" if light == state else "Default",
                "size": "Large",
            }
        )
        if index < 2:
            inlines.append({"type": "TextRun", "text": " "})

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
                "horizontalAlignment": "Center",
            },
            {
                "type": "RichTextBlock",
                "horizontalAlignment": "Center",
                "inlines": inlines,
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
    front_stage = stage_from_id(kit.front_stage_id)
    if front_stage == Stage.RELEASE:
        return "released" if kit.release_state == "released" else "unreleased"
    if front_stage == Stage.COMPLETE:
        return "complete"
    return stage_label(front_stage)


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


def _format_late_weeks(value: float) -> str:
    rounded_weeks = max(0, int(float(value) + 0.5))
    unit = "week" if rounded_weeks == 1 else "weeks"
    return f"{rounded_weeks} {unit} late"


def _build_desktop_red_attention_lines(
    *,
    trucks: list[Truck],
    dashboard_metrics: DashboardMetrics,
    schedule_insights: ScheduleInsights,
) -> list[str]:
    hold_items = list(schedule_insights.release_hold_items)
    duplicated_signal_titles = {
        "Next Body not released",
        "Weld feed low",
        "Bend buffer dry",
        "Bend buffer low",
        "No urgent flow risks",
    }

    lines: list[str] = []
    shown_count = 0
    seen_texts: set[str] = set()
    for item in dashboard_metrics.attention_items:
        if hold_items and item.title == "Engineering release is holding work start":
            continue
        if item.title in duplicated_signal_titles:
            continue
        if int(item.priority) < 90:
            continue

        shown_count += 1
        text = f"{shown_count}. {item.title}: {item.detail}"
        if text in seen_texts:
            continue
        seen_texts.add(text)
        lines.append(text)

    next_index = shown_count + 1
    for row_offset, hold in enumerate(hold_items):
        rank = next_index + row_offset
        lines.append(
            f"{rank}. Late Release: {hold.truck_number} {hold.kit_name} "
            f"({_format_late_weeks(hold.hold_weeks)})"
        )

    return lines


def _week_to_index(week_value: float, min_week: float, max_week: float, width: int) -> int:
    if width <= 1:
        return 0
    span = max(0.0001, float(max_week - min_week))
    ratio = (float(week_value) - min_week) / span
    idx = int(round(ratio * float(width - 1)))
    return max(0, min(width - 1, idx))


def _compress_png_bytes(raw: bytes, *, max_bytes: int) -> bytes:
    if len(raw) <= max_bytes:
        return raw

    try:
        from PIL import Image
    except Exception:
        return raw

    best = raw
    try:
        with Image.open(BytesIO(raw)) as img:
            source = img.convert("RGBA")
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
            dither_none = getattr(getattr(Image, "Dither", Image), "NONE", 0)

            for colors in (128, 96, 64, 48, 32, 24, 16):
                for scale in (1.0, 0.9, 0.82, 0.74, 0.66, 0.58, 0.50):
                    if scale < 1.0:
                        width = max(460, int(source.width * scale))
                        height = max(170, int(source.height * scale))
                        working = source.resize((width, height), resampling)
                    else:
                        working = source

                    indexed = working.convert(
                        "P",
                        palette=Image.ADAPTIVE,
                        colors=colors,
                        dither=dither_none,
                    )
                    out = BytesIO()
                    indexed.save(out, format="PNG", optimize=True, compress_level=9)
                    candidate = out.getvalue()

                    if len(candidate) < len(best):
                        best = candidate
                    if len(candidate) <= max_bytes:
                        return candidate
    except Exception:
        return best

    return best
def render_compact_gantt_png_bytes(
    *,
    trucks: list[Truck],
    schedule_insights: ScheduleInsights,
    max_rows: int = 12,
) -> bytes | None:
    rows = build_overlay_rows(
        trucks=trucks,
        schedule_insights=schedule_insights,
        max_rows=max(1, int(max_rows)),
    )
    if not rows:
        return None

    current_week = float(schedule_insights.current_week)
    min_week, max_week = compute_overlay_viewport(
        rows=rows,
        current_week=current_week,
        forward_horizon_weeks=4.0,
        side_padding_weeks=0.35,
        extend_to_latest_due_week=False,
    )

    raw = render_overlay_png(
        rows=rows,
        current_week=current_week,
        min_week=min_week,
        max_week=max_week,
        week_label=_week_value_to_date_label,
        fig_width=9.4,
        dpi=110,
        bar_height=0.38,
        y_label_size=5.5,
        x_label_size=6.0,
        x_label_text="Week of",
        legend_size=6.0,
    )
    if not raw:
        return None

    compressed = _compress_png_bytes(raw, max_bytes=TEAMS_GANTT_MAX_PNG_BYTES)
    if len(compressed) > TEAMS_GANTT_MAX_PNG_BYTES:
        return None
    return compressed


def render_published_gantt_png_bytes(
    *,
    trucks: list[Truck],
    schedule_insights: ScheduleInsights,
    max_rows: int = 120,
) -> bytes | None:
    rows = build_overlay_rows(
        trucks=trucks,
        schedule_insights=schedule_insights,
        max_rows=max(1, int(max_rows)),
    )
    if not rows:
        return None

    current_week = float(schedule_insights.current_week)
    min_week, max_week = compute_overlay_viewport(
        rows=rows,
        current_week=current_week,
        forward_horizon_weeks=4.0,
        side_padding_weeks=0.35,
        extend_to_latest_due_week=False,
    )

    return render_overlay_png(
        rows=rows,
        current_week=current_week,
        min_week=min_week,
        max_week=max_week,
        week_label=_week_value_to_date_label,
        fig_width=24.0,
        dpi=320,
        bar_height=0.48,
        fig_min_height=2.0,
        fig_height_per_row=0.22,
        y_label_size=8.5,
        x_label_size=8.5,
        x_label_text="Week of",
        legend_size=9.5,
    )


def _build_scheduled_vs_actual_gantt_items(
    trucks: list[Truck],
    schedule_insights: ScheduleInsights,
    *,
    max_rows: int,
    chart_width: int = 26,
    allow_image: bool = True,
    gantt_link_url: str = "",
) -> list[dict[str, Any]]:
    rows = build_overlay_rows(
        trucks=trucks,
        schedule_insights=schedule_insights,
        max_rows=max_rows,
    )
    if not rows:
        return [
            {
                "type": "TextBlock",
                "text": "Scheduled vs Actual Gantt is unavailable (no active kit rows with schedule anchors).",
                "isSubtle": True,
                "wrap": True,
                "spacing": "Small",
            }
        ]

    current_week = float(schedule_insights.current_week)
    min_week, max_week = compute_overlay_viewport(
        rows=rows,
        current_week=current_week,
        forward_horizon_weeks=4.0,
        side_padding_weeks=0.35,
        extend_to_latest_due_week=False,
    )

    image_url: str | None = None
    if allow_image:
        image_bytes = render_compact_gantt_png_bytes(
            trucks=trucks,
            schedule_insights=schedule_insights,
            max_rows=max_rows,
        )
        if image_bytes:
            encoded = base64.b64encode(image_bytes).decode("ascii")
            image_url = f"data:image/png;base64,{encoded}"
    if not image_url and gantt_link_url:
        image_url = gantt_link_url
    if image_url:
        image_item: dict[str, Any] = {
            "type": "Image",
            "url": image_url,
            "size": "Stretch",
            "altText": "Master schedule vs actual gantt",
            "spacing": "Small",
        }
        if gantt_link_url:
            image_item["selectAction"] = {
                "type": "Action.OpenUrl",
                "url": gantt_link_url,
            }
        return [
            {
                "type": "TextBlock",
                "text": "Master Schedule vs Actual",
                "weight": "Bolder",
                "spacing": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": "Click to open full-size schedule",
                "weight": "Bolder",
                "spacing": "Small",
                "wrap": True,
            },
            image_item,
        ]

    now_idx = _week_to_index(current_week, min_week, max_week, chart_width)

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
            "text": "Legend: L laser, B bend, W weld, o back, O front, > drift, | current week",
            "isSubtle": True,
            "spacing": "None",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": f"Week of {_week_value_to_date_label(schedule_insights.current_week, schedule_insights.current_week)}",
            "isSubtle": True,
            "spacing": "None",
            "wrap": True,
        },
    ]

    for row in rows:
        row_label = row.row_label
        windows = row.windows
        scheduled = ["."] * chart_width
        actual = ["."] * chart_width

        for stage, char in ((Stage.LASER, "L"), (Stage.BEND, "B"), (Stage.WELD, "W")):
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

        back_idx = _week_to_index(row.back_week, min_week, max_week, chart_width)
        front_idx = _week_to_index(row.front_week, min_week, max_week, chart_width)
        if back_idx != front_idx:
            left_idx = min(back_idx, front_idx)
            right_idx = max(back_idx, front_idx)
            for idx in range(left_idx + 1, right_idx):
                actual[idx] = "-"
        actual[back_idx] = "o"
        actual[front_idx] = "O"
        if row.is_behind and row.released and not row.blocked:
            target_week = current_week if row.status_key in {"red", "yellow"} else (
                row.expected_week if row.expected_week is not None else current_week
            )
            target_idx = _week_to_index(target_week, min_week, max_week, chart_width)
            if target_idx > front_idx:
                for idx in range(front_idx + 1, target_idx):
                    actual[idx] = ">"

        if scheduled[now_idx] == ".":
            scheduled[now_idx] = "|"
        if actual[now_idx] == ".":
            actual[now_idx] = "|"

        items.append(
            {
                "type": "TextBlock",
                "text": row_label,
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
    artifact_links: dict[str, str] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    gantt_link_url = ""
    if artifact_links:
        gantt_link_url = str(artifact_links.get("gantt_png_url", "")).strip()
    signal_tiles = [
        ("LASER", _signal_state(dashboard_metrics.laser_buffer.level, family="laser")),
        ("BRAKE", _signal_state(dashboard_metrics.bend_buffer.level, family="brake")),
        ("WELD A", _signal_state(dashboard_metrics.weld_feed_a.level, family="weld")),
        ("WELD B", _signal_state(dashboard_metrics.weld_feed_b.level, family="weld")),
    ]

    body: list[dict[str, Any]] = []

    body.append(
        {
            "type": "ColumnSet",
            "columns": [_signal_light_column(label, state) for label, state in signal_tiles],
            "spacing": "Small",
        }
    )

    attention_items_block: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Attention",
            "weight": "Bolder",
            "spacing": "Medium",
            "wrap": True,
        }
    ]
    attention_lines = _build_desktop_red_attention_lines(
        trucks=trucks,
        dashboard_metrics=dashboard_metrics,
        schedule_insights=schedule_insights,
    )
    for text in attention_lines:
        attention_items_block.append(
            {
                "type": "TextBlock",
                "text": text,
                "color": _tone_to_adaptive_color("problem"),
                "wrap": True,
                "spacing": "Small",
            }
        )
    if not attention_lines:
        attention_items_block.append(
            {
                "type": "TextBlock",
                "text": "No red attention items.",
                "isSubtle": True,
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

    body.extend(
        _build_scheduled_vs_actual_gantt_items(
            trucks=trucks,
            schedule_insights=schedule_insights,
            max_rows=max(1, int(max_trucks)),
            gantt_link_url=gantt_link_url,
        )
    )

    board_lane_items: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Board Lanes",
            "weight": "Bolder",
            "spacing": "Medium",
            "wrap": True,
        }
    ]

    visible_trucks = trucks[: max(1, int(max_trucks))]
    board_lane_columns = 2
    for start in range(0, len(visible_trucks), board_lane_columns):
        chunk = visible_trucks[start : start + board_lane_columns]
        columns: list[dict[str, Any]] = []
        for truck in chunk:
            main_kit = _find_main_body_kit(truck)
            main_text = _kit_stage_text(main_kit) if main_kit else "N/A"
            columns.append(
                {
                    "type": "Column",
                    "width": "stretch",
                    "items": [
                        {
                            "type": "TextBlock",
                            "text": f"{truck.truck_number} | Body: {main_text}",
                            "weight": "Bolder",
                            "wrap": True,
                        },
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
                        },
                    ],
                }
            )
        while len(columns) < board_lane_columns:
            columns.append({"type": "Column", "width": "stretch", "items": []})
        board_lane_items.append(
            {
                "type": "ColumnSet",
                "columns": columns,
                "spacing": "Small",
                "separator": True,
            }
        )

    body.append(
        {
            "type": "Container",
            "id": "truck_lanes_section",
            "isVisible": True,
            "items": board_lane_items,
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
    max_trucks: int = 5,
    max_attention: int = 3,
    artifact_links: dict[str, str] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    card = build_dashboard_adaptive_card(
        trucks=trucks,
        dashboard_metrics=dashboard_metrics,
        schedule_insights=schedule_insights,
        max_trucks=max(1, int(max_trucks)),
        max_attention=max(1, int(max_attention)),
        artifact_links=artifact_links,
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

