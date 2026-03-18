from __future__ import annotations

import base64
import struct
import zlib
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from io import BytesIO
from typing import Any

from dashboard_attention import build_dashboard_attention_lines
from dashboard_helpers import signal_state_for_level
from gantt_overlay import (
    OverlayRow,
    build_overlay_rows,
    compute_overlay_viewport,
    normalize_overlay_row_labels,
    render_overlay_png,
)
from metrics import DashboardMetrics
from models import Truck
from schedule import ScheduleInsights
from stages import Stage

TEAMS_GANTT_MAX_PNG_BYTES = 18_000
# Teams/sharepoint gantt favors "what still needs attention" over long-range lookahead.
PUBLISHED_GANTT_FORWARD_HORIZON_WEEKS = 2.0


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


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


@lru_cache(maxsize=4)
def _signal_light_data_url(state: str) -> str:
    normalized_state = str(state or "").strip().lower()
    width = 224
    height = 96
    off_fill_by_light = {
        "red": (245, 194, 201),
        "yellow": (244, 228, 188),
        "green": (194, 231, 203),
    }
    on_fill_by_light = {
        "red": (255, 110, 128),
        "yellow": (245, 214, 120),
        "green": (124, 219, 148),
    }
    rows = [bytearray(width * 4) for _ in range(height)]
    center_y = height / 2.0
    for light, center_x in (("red", 40), ("yellow", 112), ("green", 184)):
        is_active = light == normalized_state
        fill_rgb = on_fill_by_light[light] if is_active else off_fill_by_light[light]
        fill_alpha = 255 if light == normalized_state else 64
        fill_radius = 30.0 if is_active else 26.0
        outline_radius = (fill_radius + 2.5) if is_active else 0.0
        fill_radius_sq = fill_radius * fill_radius
        outline_radius_sq = outline_radius * outline_radius
        max_radius = max(fill_radius, outline_radius)
        min_x = max(0, int(center_x - max_radius - 1))
        max_x = min(width - 1, int(center_x + max_radius + 1))
        min_y = max(0, int(center_y - max_radius - 1))
        max_y = min(height - 1, int(center_y + max_radius + 1))
        for y in range(min_y, max_y + 1):
            for x in range(min_x, max_x + 1):
                dx = (x + 0.5) - center_x
                dy = (y + 0.5) - float(center_y)
                distance_sq = (dx * dx) + (dy * dy)
                if distance_sq <= fill_radius_sq:
                    rgba = (*fill_rgb, fill_alpha)
                elif is_active and distance_sq <= outline_radius_sq:
                    rgba = (0, 0, 0, 255)
                else:
                    continue
                offset = x * 4
                rows[y][offset : offset + 4] = bytes(rgba)

    raw = bytearray()
    for row in rows:
        raw.append(0)
        raw.extend(row)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), level=9))
        + _png_chunk(b"IEND", b"")
    )
    encoded = base64.b64encode(png).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _signal_light_column(label: str, state: str) -> dict[str, Any]:
    return {
        "type": "Column",
        "width": "stretch",
        "items": [
            {
                "type": "TextBlock",
                "text": label,
                "size": "Medium",
                "weight": "Bolder",
                "wrap": True,
                "horizontalAlignment": "Center",
            },
            {
                "type": "Image",
                "url": _signal_light_data_url(state),
                "altText": f"{label} signal is {state}",
                "horizontalAlignment": "Center",
                "width": "144px",
                "spacing": "Small",
            },
        ],
    }
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
                "size": "ExtraLarge",
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
                "size": "Medium",
                "weight": "Bolder",
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


def _overlay_status_to_adaptive_color(status_key: str) -> str:
    normalized = str(status_key or "").strip().lower()
    if normalized == "red":
        return "Attention"
    if normalized == "yellow":
        return "Warning"
    if normalized == "green":
        return "Good"
    if normalized == "blue":
        return "Accent"
    return "Default"


def _signal_driver_identity(label: str) -> tuple[str, str] | None:
    base = str(label or "").strip()
    if not base:
        return None
    note_start = base.find(" (")
    if note_start >= 0:
        base = base[:note_start].strip()
    parts = base.split(maxsplit=1)
    if len(parts) != 2:
        return None
    truck_number, kit_name = parts
    return (truck_number.strip().lower(), kit_name.strip().lower())


def _build_signal_driver_status_map(
    trucks: list[Truck],
    schedule_insights: ScheduleInsights,
) -> dict[tuple[str, str], str]:
    max_rows = max(
        1,
        sum(1 for truck in trucks for kit in truck.kits if getattr(kit, "is_active", False)),
    )
    rows = build_overlay_rows(
        trucks=trucks,
        schedule_insights=schedule_insights,
        max_rows=max_rows,
    )
    status_by_driver: dict[tuple[str, str], str] = {}
    for row in rows:
        truck_label, separator, kit_label = row.row_label.partition("|")
        if not separator:
            continue
        key = (truck_label.strip().lower(), kit_label.strip().lower())
        status_by_driver[key] = row.status_key
    return status_by_driver


def _normalize_signal_feed_driver(signal_label: str, driver: str) -> str | None:
    text = str(driver or "").strip()
    if not text:
        return None
    note_start = text.find(" (")
    if note_start < 0:
        return text

    base = text[:note_start].strip()
    note = text[note_start + 2 :]
    if note.endswith(")"):
        note = note[:-1]
    normalized_note = note.strip().lower()
    normalized_signal = str(signal_label or "").strip().upper()

    if normalized_signal.startswith("WELD") and normalized_note.startswith("weld ") and normalized_note.endswith("%"):
        return base
    if normalized_signal == "WELD A" and normalized_note == "next blocked":
        return None
    return text


def _build_signal_feed_items(
    dashboard_metrics: DashboardMetrics,
    *,
    trucks: list[Truck],
    schedule_insights: ScheduleInsights,
) -> list[dict[str, Any]]:
    signal_feeds = [
        ("LASER", dashboard_metrics.laser_buffer.drivers),
        ("BRAKE", dashboard_metrics.bend_buffer.drivers),
        ("WELD A", dashboard_metrics.weld_feed_a.drivers),
        ("WELD B", dashboard_metrics.weld_feed_b.drivers),
    ]
    status_by_driver = _build_signal_driver_status_map(trucks, schedule_insights)
    items: list[dict[str, Any]] = []
    for label, drivers in signal_feeds:
        visible = [
            normalized
            for driver in drivers
            for normalized in [_normalize_signal_feed_driver(label, str(driver))]
            if normalized
        ]
        inlines: list[dict[str, Any]] = [
            {
                "type": "TextRun",
                "text": f"{label}: ",
                "weight": "Bolder",
            }
        ]
        if visible:
            for index, driver in enumerate(visible):
                if index > 0:
                    inlines.append({"type": "TextRun", "text": ", "})
                driver_key = _signal_driver_identity(driver)
                driver_status = status_by_driver.get(driver_key or ("", ""), "")
                inlines.append(
                    {
                        "type": "TextRun",
                        "text": driver,
                        "color": _overlay_status_to_adaptive_color(driver_status),
                    }
                )
        else:
            inlines.append({"type": "TextRun", "text": "None", "isSubtle": True})
        items.append(
            {
                "type": "RichTextBlock",
                "inlines": inlines,
                "spacing": "Small",
            }
        )
    return items


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


def _build_card_gantt_render_context(
    *,
    trucks: list[Truck],
    schedule_insights: ScheduleInsights,
    max_rows: int,
) -> tuple[list[OverlayRow], float, float, float] | None:
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
        forward_horizon_weeks=PUBLISHED_GANTT_FORWARD_HORIZON_WEEKS,
        side_padding_weeks=0.35,
        extend_to_latest_due_week=False,
    )
    # Drop rows whose bars/markers never intersect the published viewport so the image stays focused and compact.
    visible_rows = [
        row for row in rows
        if _row_intersects_viewport(
            row=row,
            current_week=current_week,
            min_week=min_week,
            max_week=max_week,
        )
    ]
    if not visible_rows:
        return None
    return (
        normalize_overlay_row_labels(visible_rows),
        current_week,
        min_week,
        max_week,
    )


def _interval_intersects_viewport(
    *,
    start_week: float,
    end_week: float,
    min_week: float,
    max_week: float,
) -> bool:
    left = min(float(start_week), float(end_week))
    right = max(float(start_week), float(end_week))
    return not (right < float(min_week) or left > float(max_week))


def _row_intersects_viewport(
    *,
    row: OverlayRow,
    current_week: float,
    min_week: float,
    max_week: float,
) -> bool:
    intervals: list[tuple[float, float]] = list(row.windows.values())
    intervals.append((float(row.back_week), float(row.front_week)))

    if row.is_behind and row.released and not row.blocked:
        # Behind arrows count as visible content too, so keep rows whose catch-up arrow enters the viewport.
        target_week_value = float(current_week)
        if row.status_key not in {"red", "yellow"} and float(row.front_week) >= float(current_week):
            target_week_value = float(row.expected_week) if row.expected_week is not None else float(current_week)
        intervals.append((float(row.front_week), float(target_week_value)))

    return any(
        _interval_intersects_viewport(
            start_week=float(start_week),
            end_week=float(end_week),
            min_week=float(min_week),
            max_week=float(max_week),
        )
        for start_week, end_week in intervals
    )


def render_compact_gantt_png_bytes(
    *,
    trucks: list[Truck],
    schedule_insights: ScheduleInsights,
    max_rows: int = 12,
) -> bytes | None:
    render_context = _build_card_gantt_render_context(
        trucks=trucks,
        schedule_insights=schedule_insights,
        max_rows=max_rows,
    )
    if render_context is None:
        return None

    rows, current_week, min_week, max_week = render_context

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
    render_context = _build_card_gantt_render_context(
        trucks=trucks,
        schedule_insights=schedule_insights,
        max_rows=max_rows,
    )
    if render_context is None:
        return None

    rows, current_week, min_week, max_week = render_context

    return render_overlay_png(
        rows=rows,
        current_week=current_week,
        min_week=min_week,
        max_week=max_week,
        week_label=_week_value_to_date_label,
        fig_width=12.0,
        dpi=320,
        bar_height=0.56,
        fig_min_height=2.4,
        fig_height_per_row=0.34,
        y_label_size=9.5,
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
    title_item: dict[str, Any] = {
        "type": "TextBlock",
        "text": "Master Schedule vs Actual - Click to open",
        "weight": "Bolder",
        "spacing": "Medium",
        "wrap": True,
        "horizontalAlignment": "Center",
    }
    if gantt_link_url:
        title_item["selectAction"] = {
            "type": "Action.OpenUrl",
            "url": gantt_link_url,
        }

    render_context = _build_card_gantt_render_context(
        trucks=trucks,
        schedule_insights=schedule_insights,
        max_rows=max_rows,
    )
    if render_context is None:
        return [
            {
                "type": "TextBlock",
                "text": "Scheduled vs Actual Gantt is unavailable (no active kit rows with schedule anchors).",
                "isSubtle": True,
                "wrap": True,
                "spacing": "Small",
            }
        ]

    rows, current_week, min_week, max_week = render_context

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
            title_item,
            image_item,
        ]

    now_idx = _week_to_index(current_week, min_week, max_week, chart_width)

    items: list[dict[str, Any]] = [
        title_item,
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
        ("LASER", signal_state_for_level(dashboard_metrics.laser_buffer.level, family="laser")),
        ("BRAKE", signal_state_for_level(dashboard_metrics.bend_buffer.level, family="brake")),
        ("WELD A", signal_state_for_level(dashboard_metrics.weld_feed_a.level, family="weld")),
        ("WELD B", signal_state_for_level(dashboard_metrics.weld_feed_b.level, family="weld")),
    ]

    body: list[dict[str, Any]] = []

    body.append(
        {
            "type": "ColumnSet",
            "columns": [_signal_light_column(label, state) for label, state in signal_tiles],
            "spacing": "Small",
        }
    )
    body.append(
        {
            "type": "Container",
            "id": "signal_feed_section",
            "items": _build_signal_feed_items(
                dashboard_metrics,
                trucks=trucks,
                schedule_insights=schedule_insights,
            ),
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
    attention_lines = build_dashboard_attention_lines(
        trucks=trucks,
        dashboard_metrics=dashboard_metrics,
        schedule_insights=schedule_insights,
        min_priority=90,
        include_late_fabrication=False,
    )
    visible_attention_lines = attention_lines[: max(1, int(max_attention))]
    for item in visible_attention_lines:
        attention_items_block.append(
            {
                "type": "TextBlock",
                "text": item.text,
                "color": _tone_to_adaptive_color(item.tone),
                "wrap": True,
                "spacing": "Small",
            }
        )
    if not visible_attention_lines:
        attention_items_block.append(
            {
                "type": "TextBlock",
                "text": "No red attention items.",
                "isSubtle": True,
                "wrap": True,
                "spacing": "Small",
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
    body.append(
        {
            "type": "Container",
            "id": "attention_section",
            "isVisible": True,
            "items": attention_items_block,
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

