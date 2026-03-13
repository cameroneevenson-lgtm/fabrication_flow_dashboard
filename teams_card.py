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
from metrics import DashboardMetrics, SnapshotMetrics, compute_snapshot_metrics
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


def _render_gantt_png_data_uri(
    rows: list[OverlayRow],
    *,
    current_week: float,
    min_week: float,
    max_week: float,
) -> str | None:
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

    encoded = base64.b64encode(compressed).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _build_scheduled_vs_actual_gantt_items(
    trucks: list[Truck],
    schedule_insights: ScheduleInsights,
    *,
    max_rows: int,
    chart_width: int = 26,
    allow_image: bool = True,
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
        forward_horizon_weeks=8.0,
        side_padding_weeks=0.35,
    )

    image_url: str | None = None
    if allow_image:
        image_url = _render_gantt_png_data_uri(
            rows,
            current_week=current_week,
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
                "altText": "Master schedule vs actual gantt",
                "spacing": "Small",
            },
            {
                "type": "TextBlock",
                "text": (
                    "Front marker colors: black not due, red blocked/late release, yellow behind, "
                    "green on schedule, blue ahead. Back marker stays neutral."
                ),
                "isSubtle": True,
                "spacing": "Small",
                "wrap": True,
            },
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
        if row.is_behind:
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
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(timezone.utc)
    generated_text = generated.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    active_kits = _count_active_kits(trucks)
    blocked_kits = _count_blocked_kits(trucks)
    late_releases = len(schedule_insights.release_hold_items)
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
            "Laser",
            dashboard_metrics.laser_buffer.level.upper(),
            "Released work currently feeding laser.",
            dashboard_metrics.laser_buffer.level,
        ),
        (
            "Brake",
            dashboard_metrics.bend_buffer.level.upper(),
            "3+ released kits in laser/bend is healthy.",
            dashboard_metrics.bend_buffer.level,
        ),
        (
            "Weld A",
            dashboard_metrics.weld_feed_a.level.upper(),
            "Body-line continuity into weld A.",
            dashboard_metrics.weld_feed_a.level,
        ),
        (
            "Weld B",
            dashboard_metrics.weld_feed_b.level.upper(),
            "Console / interior / exterior weld feed.",
            dashboard_metrics.weld_feed_b.level,
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
    max_trucks: int = 5,
    max_attention: int = 3,
    artifact_links: dict[str, str] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(timezone.utc)
    generated_text = generated.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    blocked_kits = _count_blocked_kits(trucks)

    snapshot_metrics: SnapshotMetrics = compute_snapshot_metrics(
        trucks=trucks,
        schedule_insights=schedule_insights,
        dashboard_metrics=dashboard_metrics,
    )

    summary_facts = [
        {"title": "Active Trucks", "value": str(len(trucks))},
        {"title": "Laser", "value": dashboard_metrics.laser_buffer.level.capitalize()},
        {"title": "Brake", "value": dashboard_metrics.bend_buffer.level.capitalize()},
        {"title": "Weld A", "value": dashboard_metrics.weld_feed_a.level.capitalize()},
        {"title": "Weld B", "value": dashboard_metrics.weld_feed_b.level.capitalize()},
        {"title": "Kits Behind Schedule", "value": str(snapshot_metrics.sync_summary.behind_kits)},
        {"title": "Blocked Kits", "value": str(blocked_kits)},
    ]

    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Fabrication Status",
            "size": "Large",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": f"Published: {generated_text}",
            "isSubtle": True,
            "spacing": "None",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": "Confirmed published snapshot",
            "isSubtle": True,
            "spacing": "None",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": "Top Summary",
            "weight": "Bolder",
            "spacing": "Medium",
            "wrap": True,
        },
        {
            "type": "FactSet",
            "facts": summary_facts,
            "spacing": "Small",
        },
        {
            "type": "TextBlock",
            "text": "Risk Summary",
            "weight": "Bolder",
            "spacing": "Medium",
            "wrap": True,
        },
    ]

    attention_items = dashboard_metrics.attention_items[: max(1, int(max_attention))]
    for item in attention_items:
        tone = "problem" if item.priority >= 90 else ("caution" if item.priority >= 70 else "ok")
        body.append(
            {
                "type": "TextBlock",
                "text": f"- {item.title}: {item.detail}",
                "color": _tone_to_adaptive_color(tone),
                "spacing": "Small",
                "wrap": True,
            }
        )
    if not attention_items:
        body.append(
            {
                "type": "TextBlock",
                "text": "- No urgent flow risks.",
                "isSubtle": True,
                "spacing": "Small",
                "wrap": True,
            }
        )

    body.append(
        {
            "type": "TextBlock",
            "text": "Per-Truck Summary",
            "weight": "Bolder",
            "spacing": "Medium",
            "wrap": True,
        }
    )

    tone_order = {"problem": 0, "caution": 1, "ok": 2}
    sorted_truck_rows = sorted(
        snapshot_metrics.truck_rows,
        key=lambda row: (
            tone_order.get(str(row.tone or "").lower(), 3),
            0 if str(row.risk_category or "").strip().lower() != "in sync" else 1,
            str(row.truck_number or "").lower(),
        ),
    )
    visible_rows = sorted_truck_rows[: max(1, int(max_trucks))]
    for row in visible_rows:
        body.append(
            {
                "type": "TextBlock",
                "text": f"- {row.truck_number} - {row.main_stage} - {row.sync_status} - {row.issue_summary}",
                "color": _tone_to_adaptive_color(row.tone),
                "spacing": "Small",
                "wrap": True,
            }
        )
    remaining_rows = max(0, len(sorted_truck_rows) - len(visible_rows))
    if remaining_rows > 0:
        body.append(
            {
                "type": "TextBlock",
                "text": f"+{remaining_rows} more truck(s) not shown.",
                "isSubtle": True,
                "spacing": "Small",
                "wrap": True,
            }
        )

    links = {
        "summary_html_url": "",
        "gantt_png_url": "",
        "status_json_url": "",
    }
    if artifact_links:
        for key in links:
            links[key] = str(artifact_links.get(key, "")).strip()

    actions: list[dict[str, Any]] = []
    action_order = [
        ("Open Full Dashboard", "summary_html_url"),
        ("Open Gantt Snapshot", "gantt_png_url"),
        ("Open Published JSON", "status_json_url"),
    ]
    for title, key in action_order:
        url = links.get(key, "")
        if not url:
            continue
        actions.append(
            {
                "type": "Action.OpenUrl",
                "title": title,
                "url": url,
            }
        )

    if not actions:
        body.append(
            {
                "type": "TextBlock",
                "text": (
                    "Artifact links are not configured. "
                    "Set published URLs in _runtime/published_artifact_links.json."
                ),
                "isSubtle": True,
                "spacing": "Medium",
                "wrap": True,
            }
        )

    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
    }
    if actions:
        card["actions"] = actions

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
    allow_image: bool = True,
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
            allow_image=bool(allow_image),
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

