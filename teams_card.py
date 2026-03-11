from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Any

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


def _safe_row_token(value: object, fallback: str) -> str:
    tokens = str(value or "").strip().split()
    if tokens:
        return tokens[0]
    return fallback


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
    rows: list[tuple[str, dict[Stage, tuple[float, float]], Stage, Stage, bool, bool]],
    *,
    current_week: float,
    min_week: float,
    max_week: float,
) -> str | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import pyplot as plt
        from matplotlib.lines import Line2D
        from matplotlib.patches import Patch
    except Exception:
        return None

    ordered_rows = list(reversed(rows))
    if not ordered_rows:
        return None

    fig_width = 9.4
    bar_height = 0.38
    row_step = bar_height
    fig_height = max(1.1, 0.13 * len(ordered_rows) + 0.45)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=110)

    colors = {
        Stage.LASER: "#F97316",
        Stage.BEND: "#2563EB",
        Stage.WELD: "#7C3AED",
        Stage.COMPLETE: "#16A34A",
    }

    y_positions: list[float] = []
    labels: list[str] = []
    for row_index, (row_label, windows, actual_stage, tail_stage, is_released, has_blocker) in enumerate(ordered_rows):
        y = row_index * row_step
        y_positions.append(y)
        labels.append(row_label)
        if not windows:
            continue

        last_end = max(end for _start, end in windows.values())

        for stage in (Stage.LASER, Stage.BEND, Stage.WELD):
            bounds = windows.get(stage)
            if bounds is None:
                continue
            start_week, end_week = bounds
            width = max(0.08, end_week - start_week)
            ax.barh(y, width, left=start_week, height=bar_height, color=colors[stage], alpha=0.95)

        complete_right = min(max_week, last_end + 0.35)
        complete_width = max(0.08, complete_right - last_end)
        ax.barh(y, complete_width, left=last_end, height=bar_height, color=colors[Stage.COMPLETE], alpha=0.9)

        marker_week: float | None = None
        marker_color = "#16A34A"
        if not is_released:
            laser_bounds = windows.get(Stage.LASER)
            laser_trailing_week = laser_bounds[1] if laser_bounds is not None else float(current_week)
            is_late_release = float(current_week) > float(laser_trailing_week)
            marker_week = float(current_week) if is_late_release else float(laser_trailing_week)
            marker_color = "#DC2626"
        else:
            stage_bounds = windows.get(actual_stage)
            if stage_bounds is not None:
                marker_week = (stage_bounds[0] + stage_bounds[1]) / 2.0
            elif actual_stage == Stage.COMPLETE:
                marker_week = last_end
            if has_blocker:
                marker_color = "#F59E0B"

        if marker_week is not None:
            marker_week = max(min_week, min(max_week, marker_week))
            ax.scatter([marker_week], [y], s=30, c=marker_color, marker="o", zorder=6)

        if tail_stage < actual_stage and marker_week is not None:
            tail_week: float | None = None
            tail_bounds = windows.get(tail_stage)
            if tail_bounds is not None:
                tail_week = (tail_bounds[0] + tail_bounds[1]) / 2.0
            elif tail_stage == Stage.COMPLETE:
                tail_week = last_end

            if tail_week is not None:
                tail_week = max(min_week, min(max_week, tail_week))
                ax.annotate(
                    "",
                    xy=(marker_week, y),
                    xytext=(tail_week, y),
                    arrowprops={
                        "arrowstyle": "->",
                        "color": "#374151",
                        "lw": 1.0,
                        "shrinkA": 0,
                        "shrinkB": 0,
                    },
                    zorder=5,
                )

    ax.axvline(float(current_week), color="#DC2626", linestyle="--", linewidth=1.2, zorder=2)
    ax.set_xlim(float(min_week), float(max_week))
    ax.set_yticks(y_positions)
    ax.set_yticklabels(labels, fontsize=5.5)
    ax.tick_params(axis="y", pad=0)
    if y_positions:
        ax.set_ylim(-bar_height / 2.0, y_positions[-1] + (bar_height / 2.0))
    ticks = [float(current_week) + float(offset) for offset in range(-8, 9)]
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [_week_value_to_date_label(value, current_week) for value in ticks],
        fontsize=6,
        rotation=45,
        ha="right",
    )
    ax.grid(axis="x", color="#CBD5E1", linewidth=0.7, alpha=0.7)
    ax.margins(y=0.0)
    ax.set_xlabel("Week of (8 weeks back / 8 weeks forward)", fontsize=7)
    legend_handles = [
        Patch(facecolor=colors[Stage.LASER], label="Laser"),
        Patch(facecolor=colors[Stage.BEND], label="Bend"),
        Patch(facecolor=colors[Stage.WELD], label="Weld"),
        Patch(facecolor=colors[Stage.COMPLETE], label="Complete"),
        Line2D([0], [0], color="#DC2626", linestyle="--", linewidth=1.2, label="Current week"),
        Line2D(
            [0],
            [0],
            marker="o",
            color="#111827",
            markerfacecolor="#111827",
            markersize=4.0,
            linewidth=0,
            label="Actual stage (state-colored)",
        ),
    ]
    ax.legend(
        handles=legend_handles,
        loc="upper right",
        fontsize=6,
        frameon=True,
        framealpha=0.95,
        edgecolor="#CBD5E1",
    )
    ax.set_facecolor("#FFFFFF")
    fig.patch.set_facecolor("#FFFFFF")
    fig.tight_layout(pad=0.2)

    try:
        buffer = BytesIO()
        fig.savefig(buffer, format="png")
        raw = buffer.getvalue()
    finally:
        plt.close(fig)

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
    kit_windows_by_name: dict[str, dict[Stage, tuple[float, float]]] = {}
    for window in schedule_insights.kit_operation_windows:
        kit_key = str(window.kit_name or "").strip().lower()
        if not kit_key:
            continue
        stage = stage_from_id(window.stage_id)
        kit_windows_by_name.setdefault(kit_key, {})[stage] = (
            float(window.start_week),
            float(window.end_week),
        )

    if not kit_windows_by_name:
        return [
            {
                "type": "TextBlock",
                "text": "Scheduled vs Actual Gantt is unavailable (no kit schedule windows configured).",
                "isSubtle": True,
                "wrap": True,
                "spacing": "Small",
            }
        ]

    rows: list[tuple[str, dict[Stage, tuple[float, float]], Stage, Stage, bool, bool]] = []
    for truck in trucks:
        if truck.id is None:
            continue
        truck_start_week = schedule_insights.truck_planned_start_week_by_id.get(int(truck.id))
        if truck_start_week is None:
            continue
        for kit in truck.kits:
            if not kit.is_active:
                continue
            actual_stage = stage_from_id(kit.front_stage_id)
            tail_stage = stage_from_id(kit.back_stage_id)
            if actual_stage == Stage.COMPLETE:
                continue
            kit_key = str(kit.kit_name or "").strip().lower()
            base_windows = kit_windows_by_name.get(kit_key)
            if not base_windows:
                continue
            absolute_windows: dict[Stage, tuple[float, float]] = {}
            for stage, (start_week, end_week) in base_windows.items():
                if stage < tail_stage:
                    # Hide upstream stages that are already complete unless they are still in tail.
                    continue
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
            if not absolute_windows:
                continue
            truck_token = _safe_row_token(truck.truck_number, "Truck?")
            kit_token = _safe_row_token(kit.kit_name, "Kit?")
            row_label = f"{truck_token} | {kit_token}"
            is_released = str(kit.release_state or "").strip().lower() == "released"
            has_blocker = bool(str(kit.blocker or "").strip())
            rows.append((row_label, absolute_windows, actual_stage, tail_stage, is_released, has_blocker))

    if not rows:
        return [
            {
                "type": "TextBlock",
                "text": "Scheduled vs Actual Gantt is unavailable (no truck schedule anchors).",
                "isSubtle": True,
                "wrap": True,
                "spacing": "Small",
            }
        ]

    rows.sort(
        key=lambda row: (
            min(start for start, _end in row[1].values()),
            row[0].lower(),
        )
    )
    rows = rows[: max(1, int(max_rows))]

    current_week = float(schedule_insights.current_week)
    min_week = current_week - 8.0
    max_week = current_week + 8.0

    image_url: str | None = None
    if allow_image:
        image_url = _render_gantt_png_data_uri(
            rows,
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
                "altText": "Master schedule vs actual gantt",
                "spacing": "Small",
            },
            {
                "type": "TextBlock",
                "text": (
                    "Red dashed line = current week. "
                    "Unreleased marker sits on current week if late, otherwise on laser trailing edge."
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
            "text": "Legend: L laser, B bend, W weld, C complete, ! unreleased, | current week",
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
        Stage.LASER: "L",
        Stage.BEND: "B",
        Stage.WELD: "W",
        Stage.COMPLETE: "C",
    }

    for row_label, windows, actual_stage, _tail_stage, is_released, _has_blocker in rows:
        scheduled = ["."] * chart_width
        actual = ["."] * chart_width

        last_end = max(end for _start, end in windows.values())
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

        marker = actual_marker_map.get(actual_stage)
        marker_week: float | None = None
        if not is_released:
            laser_bounds = windows.get(Stage.LASER)
            laser_trailing_week = laser_bounds[1] if laser_bounds is not None else float(schedule_insights.current_week)
            is_late_release = float(schedule_insights.current_week) > float(laser_trailing_week)
            marker_week = float(schedule_insights.current_week) if is_late_release else float(laser_trailing_week)
            marker = "!"
        else:
            stage_bounds = windows.get(actual_stage)
            if stage_bounds is not None:
                marker_week = (stage_bounds[0] + stage_bounds[1]) / 2.0
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
            "3+ released kits in laser/bend is healthy.",
            dashboard_metrics.bend_buffer.level,
        ),
        (
            "Weld Feed",
            dashboard_metrics.weld_feed.level.upper(),
            "Flow readiness from bend into weld.",
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

