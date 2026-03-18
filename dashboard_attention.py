from __future__ import annotations

from dataclasses import dataclass

from gantt_overlay import build_overlay_rows
from metrics import DashboardMetrics
from models import Truck
from schedule import ScheduleInsights

DUPLICATED_SIGNAL_TITLES = {
    "Next Body not released",
    "Weld feed low",
    "Bend buffer dry",
    "Bend buffer low",
    "No urgent flow risks",
}


@dataclass(frozen=True)
class DashboardAttentionLine:
    text: str
    tone: str


def _format_late_weeks(value: float) -> str:
    rounded_weeks = max(0, int(float(value) + 0.5))
    unit = "week" if rounded_weeks == 1 else "weeks"
    return f"{rounded_weeks} {unit} late"


def _tone_from_priority(priority: int) -> str:
    if int(priority) >= 90:
        return "problem"
    if int(priority) >= 70:
        return "caution"
    return "default"


def build_dashboard_attention_lines(
    *,
    trucks: list[Truck],
    dashboard_metrics: DashboardMetrics,
    schedule_insights: ScheduleInsights,
    min_priority: int | None = None,
    include_late_release: bool = True,
    include_late_fabrication: bool = True,
    include_empty_message: bool = False,
) -> list[DashboardAttentionLine]:
    # This merges dashboard attention, late releases, and behind-schedule rows into one de-duplicated display list.
    hold_items = list(schedule_insights.release_hold_items) if include_late_release else []
    behind_rows = []
    if include_late_fabrication:
        behind_rows = [
            row
            for row in build_overlay_rows(
                trucks=list(trucks),
                schedule_insights=schedule_insights,
                max_rows=max(1, len(trucks) * 8),
            )
            if row.is_behind
        ]

    lines: list[DashboardAttentionLine] = []
    shown_count = 0
    seen_texts: set[str] = set()
    for item in dashboard_metrics.attention_items:
        if hold_items and item.title == "Engineering release is holding work start":
            continue
        if item.title in DUPLICATED_SIGNAL_TITLES:
            continue
        if min_priority is not None and int(item.priority) < int(min_priority):
            continue

        shown_count += 1
        text = f"{shown_count}. {item.title}: {item.detail}"
        if text in seen_texts:
            continue
        seen_texts.add(text)
        lines.append(DashboardAttentionLine(text=text, tone=_tone_from_priority(item.priority)))

    if shown_count == 0 and not hold_items:
        if include_empty_message:
            return [DashboardAttentionLine(text="No additional attention items.", tone="muted")]
        return []
    if not hold_items and not behind_rows:
        return lines

    late_release_keys = {
        (str(hold.truck_number or "").strip().lower(), str(hold.kit_name or "").strip().lower())
        for hold in hold_items
    }

    next_index = shown_count + 1
    for row_offset, hold in enumerate(hold_items):
        rank = next_index + row_offset
        lines.append(
            DashboardAttentionLine(
                text=(
                    f"{rank}. Late Release: {hold.truck_number} {hold.kit_name} "
                    f"({_format_late_weeks(hold.hold_weeks)})"
                ),
                tone="problem",
            )
        )

    late_fabrication_rows: list[tuple[str, str]] = []
    for row in behind_rows:
        parts = [part.strip() for part in str(row.row_label or "").split("|", 1)]
        if len(parts) != 2:
            continue
        key = (parts[0].lower(), parts[1].lower())
        # Avoid listing the same kit twice when a late release row already explains why it is behind.
        if key in late_release_keys:
            continue
        late_fabrication_rows.append((parts[0], parts[1]))

    late_start_index = next_index + len(hold_items)
    for row_offset, (truck_number, kit_name) in enumerate(late_fabrication_rows):
        rank = late_start_index + row_offset
        lines.append(
            DashboardAttentionLine(
                text=f"{rank}. Behind Schedule: {truck_number} {kit_name}",
                tone="caution",
            )
        )

    return lines
