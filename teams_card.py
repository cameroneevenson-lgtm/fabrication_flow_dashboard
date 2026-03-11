from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from metrics import BossLensMetrics, BossTile, BossTruckRow


def _tone_to_adaptive_color(tone: str) -> str:
    normalized = str(tone or "").strip().lower()
    if normalized == "ok":
        return "Good"
    if normalized == "caution":
        return "Warning"
    if normalized == "problem":
        return "Attention"
    return "Default"


def _tile_column(tile: BossTile) -> dict[str, Any]:
    return {
        "type": "Column",
        "width": "stretch",
        "items": [
            {
                "type": "TextBlock",
                "text": tile.label,
                "size": "Small",
                "isSubtle": True,
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": tile.value,
                "weight": "Bolder",
                "size": "Medium",
                "color": _tone_to_adaptive_color(tile.tone),
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": tile.detail,
                "size": "Small",
                "wrap": True,
                "spacing": "None",
            },
        ],
    }


def _truck_row_container(row: BossTruckRow) -> dict[str, Any]:
    return {
        "type": "Container",
        "separator": True,
        "spacing": "Small",
        "items": [
            {
                "type": "TextBlock",
                "text": (
                    f"{row.truck_number} | Main Kit: {row.main_stage} | "
                    f"Sync: {row.sync_status} | Main Released: {row.main_released}"
                ),
                "weight": "Bolder",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": f"Risk: {row.risk_category}. {row.issue_summary}",
                "color": _tone_to_adaptive_color(row.tone),
                "wrap": True,
                "spacing": "None",
            },
        ],
    }


def build_boss_lens_adaptive_card(
    metrics: BossLensMetrics,
    *,
    max_trucks: int = 20,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now(timezone.utc)
    generated_text = generated.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    visible_trucks = metrics.truck_rows[: max(1, int(max_trucks))]

    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": "Fabrication Flow Dashboard - Boss Lens",
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
            "text": "Top-Level Summary",
            "weight": "Bolder",
            "spacing": "Medium",
            "wrap": True,
        },
    ]

    for start in range(0, len(metrics.tiles), 4):
        chunk = metrics.tiles[start : start + 4]
        body.append(
            {
                "type": "ColumnSet",
                "columns": [_tile_column(tile) for tile in chunk],
                "spacing": "Small",
            }
        )

    body.extend(
        [
            {
                "type": "TextBlock",
                "text": "Master Schedule Sync",
                "weight": "Bolder",
                "spacing": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": (
                    f"{metrics.sync_summary.in_sync_kits} kits in sync, "
                    f"{metrics.sync_summary.behind_kits} kits behind, "
                    f"{metrics.sync_summary.ahead_kits} kits ahead."
                ),
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": "Release Alignment",
                "weight": "Bolder",
                "spacing": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": metrics.release_summary.summary,
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": "Flow Health",
                "weight": "Bolder",
                "spacing": "Medium",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": metrics.flow_summary,
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": "Per-Truck Summary",
                "weight": "Bolder",
                "spacing": "Medium",
                "wrap": True,
            },
        ]
    )

    for row in visible_trucks:
        body.append(_truck_row_container(row))

    remaining = len(metrics.truck_rows) - len(visible_trucks)
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
    metrics: BossLensMetrics,
    *,
    max_trucks: int = 20,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    card = build_boss_lens_adaptive_card(
        metrics,
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

