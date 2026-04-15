from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable

from dashboard_helpers import normalize_blocked_state_from_kit
from models import SECONDARY_FLOW_KIT_NAMES, Truck, TruckKit, canonicalize_kit_name
from schedule import ScheduleInsights
from stages import FABRICATION_ALLOWED_POSITIONS, FABRICATION_STAGE_POSITION_SCALE, Stage, stage_from_id

LASER_START_POSITION = 10
LASER_MID_POSITION = 14
LASER_NEAR_POSITION = 18
BEND_START_POSITION = 20
BEND_MID_POSITION = 24
BEND_NEAR_POSITION = 28
WELD_START_POSITION = 30
WELD_MID_POSITION = 34
WELD_NEAR_POSITION = 38

OVERLAY_ALLOWED_POSITIONS: tuple[int, ...] = FABRICATION_ALLOWED_POSITIONS

FABRICATION_STAGES: tuple[Stage, ...] = (Stage.LASER, Stage.BEND, Stage.WELD)
STAGE_POSITION_ANCHORS: dict[Stage, tuple[int, int, int]] = {
    Stage.LASER: (LASER_START_POSITION, LASER_MID_POSITION, LASER_NEAR_POSITION),
    Stage.BEND: (BEND_START_POSITION, BEND_MID_POSITION, BEND_NEAR_POSITION),
    Stage.WELD: (WELD_START_POSITION, WELD_MID_POSITION, WELD_NEAR_POSITION),
}
POSITION_TO_STAGE: dict[int, Stage] = {
    LASER_START_POSITION: Stage.LASER,
    12: Stage.LASER,
    LASER_MID_POSITION: Stage.LASER,
    16: Stage.LASER,
    LASER_NEAR_POSITION: Stage.LASER,
    BEND_START_POSITION: Stage.BEND,
    22: Stage.BEND,
    BEND_MID_POSITION: Stage.BEND,
    26: Stage.BEND,
    BEND_NEAR_POSITION: Stage.BEND,
    WELD_START_POSITION: Stage.WELD,
    32: Stage.WELD,
    WELD_MID_POSITION: Stage.WELD,
    36: Stage.WELD,
    WELD_NEAR_POSITION: Stage.WELD,
}
POSITION_TO_RATIO: dict[int, float] = {
    LASER_START_POSITION: 0.0,
    12: 0.25,
    LASER_MID_POSITION: 0.5,
    16: 0.75,
    LASER_NEAR_POSITION: 0.85,
    BEND_START_POSITION: 0.0,
    22: 0.25,
    BEND_MID_POSITION: 0.5,
    26: 0.75,
    BEND_NEAR_POSITION: 0.85,
    WELD_START_POSITION: 0.0,
    32: 0.25,
    WELD_MID_POSITION: 0.5,
    36: 0.75,
    WELD_NEAR_POSITION: 0.85,
}

STATUS_COLORS: dict[str, str] = {
    "black": "#111827",
    "red": "#DC2626",
    "yellow": "#F59E0B",
    "green": "#16A34A",
    "blue": "#2563EB",
}
NEUTRAL_BACK_COLOR = "#6B7280"
STAGE_BAR_COLORS: dict[Stage, str] = {
    Stage.LASER: "#F97316",
    Stage.BEND: "#2563EB",
    Stage.WELD: "#7C3AED",
}
STAGE_BAR_ALPHA = 0.5
SECONDARY_FLOW_KIT_KEYS = {
    canonicalize_kit_name(name).casefold()
    for name in SECONDARY_FLOW_KIT_NAMES
}


@dataclass(frozen=True)
class OverlayRow:
    row_label: str
    windows: dict[Stage, tuple[float, float]]
    baseline_windows: dict[Stage, tuple[float, float]]
    front_position: int
    back_position: int
    expected_position: int
    front_week: float
    back_week: float
    expected_week: float | None
    latest_due_week: float
    released: bool
    blocked: bool
    blocked_reason: str
    status_key: str
    status_color: str
    is_behind: bool
    is_not_due: bool


def _normalize_week_around_current(week_value: float, current_week: float) -> float:
    value = float(week_value)
    current = float(current_week)
    cycle = 52.0
    while (value - current) > 26.0:
        value -= cycle
    while (current - value) > 26.0:
        value += cycle
    return value
def _normalize_release_state(kit: TruckKit) -> bool:
    release_state = str(getattr(kit, "release_state", "") or "").strip().lower()
    if release_state == "released":
        return True
    return stage_from_id(getattr(kit, "front_stage_id", int(Stage.RELEASE))) > Stage.RELEASE


def _default_front_position_for_stage(stage_id: int | Stage | None) -> int:
    stage = stage_from_id(stage_id)
    if stage >= Stage.WELD:
        return WELD_MID_POSITION
    if stage == Stage.BEND:
        return BEND_MID_POSITION
    if stage == Stage.LASER:
        return LASER_MID_POSITION
    return LASER_START_POSITION


def _default_back_position_for_stage(stage_id: int | Stage | None) -> int:
    stage = stage_from_id(stage_id)
    if stage >= Stage.WELD:
        return WELD_START_POSITION
    if stage == Stage.BEND:
        return BEND_START_POSITION
    return LASER_START_POSITION


def _normalize_position_value(value: object) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    if normalized not in OVERLAY_ALLOWED_POSITIONS:
        return None
    return normalized


def _position_matches_stage(position: int, stage: Stage) -> bool:
    if stage == Stage.RELEASE:
        return int(position) == LASER_START_POSITION
    if stage >= Stage.WELD:
        return int(position) in FABRICATION_STAGE_POSITION_SCALE[Stage.WELD]
    stage_positions = FABRICATION_STAGE_POSITION_SCALE.get(stage)
    if stage_positions is None:
        return False
    return int(position) in stage_positions


def normalize_position_span(
    front_position: object,
    back_position: object,
    *,
    front_stage_id: int | Stage | None,
    back_stage_id: int | Stage | None,
) -> tuple[int, int]:
    normalized_front = _normalize_position_value(front_position)
    normalized_back = _normalize_position_value(back_position)
    front_stage = stage_from_id(front_stage_id)
    back_stage = stage_from_id(back_stage_id)

    if normalized_front is None or not _position_matches_stage(normalized_front, front_stage):
        normalized_front = _default_front_position_for_stage(front_stage_id)
    if normalized_back is None or not _position_matches_stage(normalized_back, back_stage):
        if back_stage == front_stage:
            normalized_back = normalized_front
        else:
            normalized_back = _default_back_position_for_stage(back_stage)
    if normalized_front < normalized_back:
        normalized_front = normalized_back
    return (normalized_front, normalized_back)


def expected_position_for_week(
    *,
    current_week: float,
    baseline_windows: dict[Stage, tuple[float, float]],
) -> int:
    laser_bounds = baseline_windows.get(Stage.LASER)
    if laser_bounds is None:
        return 0
    if float(current_week) < float(laser_bounds[0]):
        return 0

    expected_stage = Stage.LASER
    for stage in (Stage.BEND, Stage.WELD):
        bounds = baseline_windows.get(stage)
        if bounds is None:
            continue
        if float(current_week) >= float(bounds[0]):
            expected_stage = stage

    start_week, end_week = baseline_windows.get(expected_stage, laser_bounds)
    if float(end_week) <= float(start_week):
        return STAGE_POSITION_ANCHORS[expected_stage][2]

    ratio = max(0.0, min(1.0, (float(current_week) - float(start_week)) / (float(end_week) - float(start_week))))
    anchors = STAGE_POSITION_ANCHORS[expected_stage]
    if ratio < 0.34:
        return anchors[0]
    if ratio < 0.67:
        return anchors[1]
    return anchors[2]


def _fallback_position_week(
    *,
    stage: Stage,
    windows: dict[Stage, tuple[float, float]],
    fallback_week: float,
) -> float:
    if not windows:
        return float(fallback_week)
    if stage == Stage.LASER:
        return min(float(start) for start, _end in windows.values())
    if stage == Stage.BEND:
        laser_bounds = windows.get(Stage.LASER)
        if laser_bounds is not None:
            return float(laser_bounds[1])
        weld_bounds = windows.get(Stage.WELD)
        if weld_bounds is not None:
            return float(weld_bounds[0])
    return max(float(end) for _start, end in windows.values())


def overlay_position_to_week(
    *,
    position: int,
    windows: dict[Stage, tuple[float, float]],
    fallback_week: float,
) -> float:
    if position < LASER_START_POSITION:
        laser_bounds = windows.get(Stage.LASER)
        if laser_bounds is None:
            return float(fallback_week)
        return float(laser_bounds[0])

    stage = POSITION_TO_STAGE.get(int(position), Stage.LASER)
    bounds = windows.get(stage)
    if bounds is None:
        return _fallback_position_week(stage=stage, windows=windows, fallback_week=fallback_week)

    start_week, end_week = bounds
    if float(end_week) <= float(start_week):
        return float(start_week)
    ratio = POSITION_TO_RATIO.get(int(position), 0.0)
    return float(start_week) + ((float(end_week) - float(start_week)) * float(ratio))


def classify_front_status(
    *,
    released: bool,
    blocked: bool,
    front_stage: Stage,
    expected_position: int,
    front_position: int,
    expected_week: float | None = None,
    front_week: float | None = None,
    schedule_tolerance_weeks: float = 1.0,
    current_week: float | None = None,
) -> tuple[str, str]:
    # Reduced decision rules:
    # - blocked => red
    # - unreleased more than 1 week overdue => red
    # - unreleased up to 1 week overdue => yellow
    # - unreleased not due yet => black
    # - released late in final weld => yellow
    # - released within +/- tolerance of master timeline => green
    # - released more than tolerance late => yellow
    # - released more than tolerance ahead => blue
    # - no position-based fallback: if week context is missing, default released to green
    if blocked:
        return ("red", STATUS_COLORS["red"])
    if not released:
        if expected_week is not None and current_week is not None:
            due_delta = float(expected_week) - float(current_week)
            if due_delta < -float(schedule_tolerance_weeks):
                return ("red", STATUS_COLORS["red"])
            if due_delta <= 0.0:
                return ("yellow", STATUS_COLORS["yellow"])
            return ("black", STATUS_COLORS["black"])
        if expected_position >= LASER_START_POSITION:
            return ("red", STATUS_COLORS["red"])
        return ("black", STATUS_COLORS["black"])
    if released and front_week is not None and current_week is not None:
        week_delta = float(front_week) - float(current_week)
        if front_stage >= Stage.WELD and current_week is not None and float(front_week) < float(current_week):
            return ("yellow", STATUS_COLORS["yellow"])
        if abs(week_delta) <= float(schedule_tolerance_weeks):
            return ("green", STATUS_COLORS["green"])
        if week_delta < 0:
            return ("yellow", STATUS_COLORS["yellow"])
        return ("blue", STATUS_COLORS["blue"])
    return ("green", STATUS_COLORS["green"])


def _is_small_kit(kit_name: str) -> bool:
    return canonicalize_kit_name(str(kit_name or "")).casefold() in SECONDARY_FLOW_KIT_KEYS


def build_overlay_rows(
    *,
    trucks: list[Truck],
    schedule_insights: ScheduleInsights,
    max_rows: int,
    include_small_kits: bool = True,
) -> list[OverlayRow]:
    kit_windows_by_name: dict[str, dict[Stage, tuple[float, float]]] = {}
    for window in schedule_insights.kit_operation_windows:
        kit_key = str(window.kit_name or "").strip().lower()
        if not kit_key:
            continue
        stage = stage_from_id(window.stage_id)
        if stage not in FABRICATION_STAGES:
            continue
        kit_windows_by_name.setdefault(kit_key, {})[stage] = (
            float(window.start_week),
            float(window.end_week),
        )

    rows: list[OverlayRow] = []
    for truck in trucks:
        if truck.id is None:
            continue
        truck_start_week = schedule_insights.truck_planned_start_week_by_id.get(int(truck.id))
        if truck_start_week is None:
            # Rows without a plan anchor cannot be projected onto the shared timeline.
            continue

        for kit in truck.kits:
            if not kit.is_active:
                continue
            if not include_small_kits and _is_small_kit(kit.kit_name):
                continue

            front_stage = stage_from_id(kit.front_stage_id)
            back_stage = stage_from_id(kit.back_stage_id)
            if front_stage == Stage.COMPLETE:
                # Completed kits are intentionally hidden from the active operational overlay.
                continue

            kit_key = str(kit.kit_name or "").strip().lower()
            base_windows = kit_windows_by_name.get(kit_key)
            if not base_windows:
                continue

            baseline_windows: dict[Stage, tuple[float, float]] = {}
            for stage, (start_week, end_week) in base_windows.items():
                normalized_start = _normalize_week_around_current(
                    float(truck_start_week) + float(start_week),
                    schedule_insights.current_week,
                )
                normalized_end = _normalize_week_around_current(
                    float(truck_start_week) + float(end_week),
                    schedule_insights.current_week,
                )
                if normalized_end < normalized_start:
                    normalized_end = normalized_start
                baseline_windows[stage] = (normalized_start, normalized_end)
            if not baseline_windows:
                continue

            windows: dict[Stage, tuple[float, float]] = {}
            for stage, bounds in baseline_windows.items():
                if stage < back_stage:
                    # Hide stages that are already fully behind the active tail.
                    continue
                windows[stage] = bounds
            if not windows:
                continue

            released = _normalize_release_state(kit)
            blocked, blocked_reason = normalize_blocked_state_from_kit(kit)
            front_position, back_position = normalize_position_span(
                getattr(kit, "front_position", None),
                getattr(kit, "back_position", None),
                front_stage_id=front_stage,
                back_stage_id=back_stage,
            )

            expected_position = expected_position_for_week(
                current_week=float(schedule_insights.current_week),
                baseline_windows=baseline_windows,
            )
            if not released:
                # Unreleased work stays fixed at laser entry until it is released.
                unreleased_anchor = LASER_START_POSITION
                display_front_position = unreleased_anchor
                display_back_position = unreleased_anchor
            else:
                display_front_position = front_position
                display_back_position = back_position

            front_week = overlay_position_to_week(
                position=display_front_position,
                windows=baseline_windows,
                fallback_week=float(schedule_insights.current_week),
            )
            back_week = overlay_position_to_week(
                position=display_back_position,
                windows=baseline_windows,
                fallback_week=float(schedule_insights.current_week),
            )
            scheduled_front_week: float | None = None
            if expected_position >= LASER_START_POSITION:
                scheduled_front_week = overlay_position_to_week(
                    position=expected_position,
                    windows=baseline_windows,
                    fallback_week=float(schedule_insights.current_week),
                )
            release_due_week = min(float(start) for start, _end in baseline_windows.values())
            comparison_due_week = release_due_week if not released else scheduled_front_week
            status_key, status_color = classify_front_status(
                released=released,
                blocked=blocked,
                front_stage=front_stage,
                expected_position=expected_position,
                front_position=display_front_position,
                expected_week=comparison_due_week,
                front_week=front_week,
                current_week=float(schedule_insights.current_week),
            )
            is_not_due = (not released) and (float(release_due_week) > float(schedule_insights.current_week))
            unreleased_overdue = bool(
                (not released)
                and float(release_due_week) < float(schedule_insights.current_week)
            )
            is_behind = bool(
                (released and float(front_week) < float(schedule_insights.current_week))
                or (released and scheduled_front_week is not None and (float(front_week) < (float(scheduled_front_week) - 1.0)))
                or unreleased_overdue
            )
            expected_week: float | None = comparison_due_week if is_behind else None

            latest_due_week = max(float(end) for _start, end in baseline_windows.values())
            truck_label = str(truck.truck_number or "Truck?").strip() or "Truck?"
            kit_label = str(kit.kit_name or "Kit?").strip() or "Kit?"
            row_label = f"{truck_label} | {kit_label}"

            rows.append(
                OverlayRow(
                    row_label=row_label,
                    windows=windows,
                    baseline_windows=baseline_windows,
                    front_position=display_front_position,
                    back_position=display_back_position,
                    expected_position=expected_position,
                    front_week=front_week,
                    back_week=back_week,
                    expected_week=expected_week,
                    latest_due_week=latest_due_week,
                    released=released,
                    blocked=blocked,
                    blocked_reason=blocked_reason,
                    status_key=status_key,
                    status_color=status_color,
                    is_behind=is_behind,
                    is_not_due=is_not_due,
                )
            )

    rows.sort(
        key=lambda row: (
            min(float(start) for start, _end in row.baseline_windows.values()),
            row.row_label.lower(),
        )
    )
    if rows:
        parsed_labels = [str(row.row_label or "").split(" | ", 1) for row in rows]
        truck_width = max(len(parts[0].rstrip()) for parts in parsed_labels if parts)
        kit_width = max(len(parts[1].rstrip()) for parts in parsed_labels if len(parts) > 1)
        rows = [
            replace(
                row,
                row_label=(
                    f"{parts[0].rstrip():<{truck_width}} | {parts[1].rstrip():<{kit_width}}"
                    if len(parts) > 1
                    else str(row.row_label or "")
                ),
            )
            for row, parts in zip(rows, parsed_labels)
        ]
    return rows[: max(1, int(max_rows))]


def normalize_overlay_row_labels(
    rows: list[OverlayRow],
    *,
    truck_width: int | None = None,
    kit_width: int | None = None,
) -> list[OverlayRow]:
    if not rows:
        return []

    parsed_labels = [str(row.row_label or "").split(" | ", 1) for row in rows]
    normalized_truck_width = max(
        int(truck_width or 0),
        max(len(parts[0].rstrip()) for parts in parsed_labels if parts),
    )
    normalized_kit_width = max(
        int(kit_width or 0),
        max(len(parts[1].rstrip()) for parts in parsed_labels if len(parts) > 1),
    )
    return [
        replace(
            row,
            row_label=(
                f"{parts[0].rstrip():<{normalized_truck_width}} | {parts[1].rstrip():<{normalized_kit_width}}"
                if len(parts) > 1
                else str(row.row_label or "")
            ),
        )
        for row, parts in zip(rows, parsed_labels)
    ]


def compute_overlay_viewport(
    *,
    rows: list[OverlayRow],
    current_week: float,
    forward_horizon_weeks: float = 8.0,
    side_padding_weeks: float = 0.35,
    extend_to_latest_due_week: bool = True,
) -> tuple[float, float]:
    week_start_anchor = math.floor(float(current_week))
    if not rows:
        return (
            float(week_start_anchor) - float(side_padding_weeks),
            float(week_start_anchor) + float(forward_horizon_weeks) + float(side_padding_weeks),
        )

    behind_left_edges = [
        min(float(row.front_week), float(row.back_week))
        for row in rows
        if row.is_behind
    ]
    left_anchor = min(behind_left_edges) if behind_left_edges else float(week_start_anchor)

    right_anchor = float(week_start_anchor) + float(forward_horizon_weeks)
    if extend_to_latest_due_week:
        latest_due_week = max(float(row.latest_due_week) for row in rows)
        right_anchor = max(right_anchor, latest_due_week)

    min_week = min(left_anchor, float(week_start_anchor)) - float(side_padding_weeks)
    max_week = right_anchor + float(side_padding_weeks)
    if max_week <= min_week:
        max_week = min_week + 1.0
    return (min_week, max_week)


def build_week_ticks(*, current_week: float, min_week: float, max_week: float) -> list[float]:
    start_offset = int(math.ceil(float(min_week) - float(current_week)))
    end_offset = int(math.ceil(float(max_week) - float(current_week))) - 1
    if end_offset < start_offset:
        end_offset = start_offset
    return [float(current_week) + float(offset) for offset in range(start_offset, end_offset + 1)]


def render_overlay_png(
    *,
    rows: list[OverlayRow],
    current_week: float,
    min_week: float,
    max_week: float,
    week_label: Callable[[float, float], str],
    fig_width: float = 9.4,
    dpi: int = 110,
    bar_height: float = 0.38,
    fig_min_height: float = 1.1,
    fig_height_per_row: float = 0.13,
    y_label_size: float = 5.5,
    x_label_size: float = 6.0,
    x_label_text: str = "Week of",
    legend_size: float = 6.0,
    dark_mode: bool = False,
) -> bytes | None:
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

    row_step = bar_height
    fig_height = max(float(fig_min_height), (float(fig_height_per_row) * len(ordered_rows)) + 0.45)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=dpi)

    if dark_mode:
        plot_bg = "#061321"
        figure_bg = "#061321"
        row_band_even = "#071827"
        row_band_odd = "#0B2033"
        text_color = "#C6D8E6"
        line_color = "#92ABC1"
        back_fill = "#0D2133"
        separator_color = "#274157"
        grid_color = "#7AA7C4"
        legend_face = "#081B2B"
        legend_edge = "#4D6A84"
        today_color = "#FF6B6B"
    else:
        plot_bg = "#FFFFFF"
        figure_bg = "#FFFFFF"
        row_band_even = "#FFFFFF"
        row_band_odd = "#F3F4F6"
        text_color = "#0F172A"
        line_color = "#4B5563"
        back_fill = "#F8FAFC"
        separator_color = "#D9E2EC"
        grid_color = "#94A3B8"
        legend_face = "#FFFFFF"
        legend_edge = "#CBD5E1"
        today_color = "#DC2626"

    y_positions: list[float] = []
    labels: list[str] = []
    for row_index, row in enumerate(ordered_rows):
        y = float(row_index) * float(row_step)
        y_positions.append(y)
        labels.append(str(row.row_label))

        ax.axhspan(
            y - (bar_height / 2.0),
            y + (bar_height / 2.0),
            color=row_band_even if row_index % 2 == 0 else row_band_odd,
            zorder=0,
        )

        for stage in FABRICATION_STAGES:
            bounds = row.windows.get(stage)
            if bounds is None:
                continue
            start_week, end_week = bounds
            width = max(0.08, float(end_week) - float(start_week))
            ax.barh(
                y,
                width,
                left=float(start_week),
                height=bar_height,
                color=STAGE_BAR_COLORS[stage],
                alpha=STAGE_BAR_ALPHA,
                zorder=2,
            )

        clipped_back = max(float(min_week), min(float(max_week), float(row.back_week)))
        clipped_front = max(float(min_week), min(float(max_week), float(row.front_week)))
        line_start = min(clipped_back, clipped_front)
        line_end = max(clipped_back, clipped_front)
        ax.hlines(
            y,
            line_start,
            line_end,
            color=line_color,
            linewidth=1.1,
            alpha=0.9,
            zorder=5,
        )
        ax.scatter(
            [clipped_back],
            [y],
            s=28,
            facecolors=back_fill,
            edgecolors=NEUTRAL_BACK_COLOR,
            linewidths=1.1,
            marker="o",
            zorder=6,
        )
        ax.scatter(
            [clipped_front],
            [y],
            s=32,
            c=row.status_color,
            marker="o",
            zorder=7,
        )

        if row.is_behind and row.released and not row.blocked:
            target_week_value = float(current_week)
            if row.status_key not in {"red", "yellow"} and float(row.front_week) >= float(current_week):
                target_week_value = float(row.expected_week) if row.expected_week is not None else float(current_week)
            target_week = max(float(min_week), min(float(max_week), target_week_value))
            if target_week > (clipped_front + 0.01):
                ax.annotate(
                    "",
                    xy=(target_week, y),
                    xytext=(clipped_front, y),
                    arrowprops={
                        "arrowstyle": "->",
                        "color": row.status_color,
                        "lw": 1.25,
                        "shrinkA": 0,
                        "shrinkB": 0,
                    },
                    zorder=6.5,
                )

    if y_positions:
        boundary_lines = [y - (bar_height / 2.0) for y in y_positions]
        boundary_lines.append(y_positions[-1] + (bar_height / 2.0))
        for separator_y in boundary_lines:
            ax.hlines(
                separator_y,
                float(min_week),
                float(max_week),
                color=separator_color,
                linewidth=0.6,
                alpha=0.75,
                zorder=1.5,
            )

    ax.set_xlim(float(min_week), float(max_week))
    ax.set_yticks(y_positions)
    ax.set_yticklabels(labels, fontsize=y_label_size, fontfamily="DejaVu Sans Mono", color=text_color)
    ax.tick_params(axis="y", pad=0, colors=text_color)
    if y_positions:
        ax.set_ylim(-bar_height / 2.0, y_positions[-1] + (bar_height / 2.0) + row_step)

    week_start_anchor = math.floor(float(current_week))
    ticks = build_week_ticks(current_week=float(week_start_anchor), min_week=float(min_week), max_week=float(max_week))
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [week_label(value, float(week_start_anchor)) for value in ticks],
        fontsize=x_label_size,
        rotation=45,
        ha="right",
        color=text_color,
    )
    ax.tick_params(axis="x", colors=text_color)
    if y_positions:
        grid_ymin = -bar_height / 2.0
        grid_ymax = y_positions[-1] + (bar_height / 2.0)
        for tick in ticks:
            ax.vlines(
                float(tick),
                grid_ymin,
                grid_ymax,
                color=grid_color,
                linewidth=0.45,
                alpha=0.28,
                zorder=1,
            )
        ax.vlines(
            float(current_week),
            grid_ymin,
            grid_ymax,
            color=today_color,
            linestyle="--",
            linewidth=1.2,
            zorder=4,
        )
    ax.margins(y=0.0)
    if x_label_text:
        ax.set_xlabel(x_label_text, fontsize=max(6.0, float(x_label_size) + 1.0), color=text_color)

    legend_handles = [
        Patch(facecolor=STAGE_BAR_COLORS[Stage.LASER], alpha=STAGE_BAR_ALPHA, label="LASER"),
        Patch(facecolor=STAGE_BAR_COLORS[Stage.BEND], alpha=STAGE_BAR_ALPHA, label="BEND"),
        Patch(facecolor=STAGE_BAR_COLORS[Stage.WELD], alpha=STAGE_BAR_ALPHA, label="WELD"),
        Line2D([0], [0], color=today_color, linestyle="--", linewidth=1.2, label="TODAY"),
    ]
    legend = ax.legend(
        handles=legend_handles,
        loc="upper right",
        fontsize=legend_size,
        frameon=True,
        framealpha=0.95,
        edgecolor=legend_edge,
    )
    if legend is not None:
        legend.get_frame().set_facecolor(legend_face)
        legend.get_frame().set_edgecolor(legend_edge)
        for text in legend.get_texts():
            text.set_color(text_color)
    ax.set_facecolor(plot_bg)
    fig.patch.set_facecolor(figure_bg)
    ax.spines["left"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["top"].set_visible(False)
    ax.spines["bottom"].set_color(separator_color)
    fig.tight_layout(pad=0.2)

    try:
        from io import BytesIO

        buffer = BytesIO()
        fig.savefig(buffer, format="png")
        return buffer.getvalue()
    finally:
        plt.close(fig)
