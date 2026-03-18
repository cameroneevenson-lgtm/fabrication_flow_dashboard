from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dashboard_helpers import is_truck_complete, sort_trucks_natural
from database import FabricationDatabase
from metrics import DashboardMetrics, SnapshotMetrics, compute_dashboard_metrics, compute_snapshot_metrics
from models import Truck
from publish_artifacts import ArtifactPublishResult, publish_compact_artifacts
from schedule import ScheduleInsights, build_schedule_insights
from teams_card import build_teams_webhook_payload

DEFAULT_TEAMS_WEBHOOK_URL = (
    "https://default97009fec357647f39ce0fc3d1496b7.b8.environment.api.powerplatform.com:443/"
    "powerautomate/automations/direct/workflows/98b3a4e7ea8c439090e2d40232163817/triggers/manual/"
    "paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=ggEqWDyQT6T3GEouJCsp0jiZPF8mgQI5j5bl4T8T4CQ"
)


@dataclass(frozen=True)
class DashboardPublishSnapshot:
    trucks: list[Truck]
    schedule_insights: ScheduleInsights
    dashboard_metrics: DashboardMetrics
    snapshot_metrics: SnapshotMetrics
    artifacts: ArtifactPublishResult


def load_active_dashboard_trucks(database: FabricationDatabase) -> list[Truck]:
    loaded_trucks = sort_trucks_natural(database.load_trucks_with_kits(active_only=True))
    return [
        truck for truck in loaded_trucks if truck.is_visible and not is_truck_complete(truck)
    ]


def build_dashboard_publish_snapshot(
    *,
    project_root: Path,
    trucks: list[Truck],
    schedule_insights: ScheduleInsights | None = None,
    dashboard_metrics: DashboardMetrics | None = None,
    generated_at: datetime | None = None,
    configured_links: dict[str, str] | None = None,
) -> DashboardPublishSnapshot:
    ordered_trucks = sort_trucks_natural(list(trucks))
    insights = schedule_insights or build_schedule_insights(ordered_trucks)
    resolved_dashboard_metrics = dashboard_metrics or compute_dashboard_metrics(
        ordered_trucks,
        schedule_insights=insights,
    )
    snapshot_metrics = compute_snapshot_metrics(
        ordered_trucks,
        schedule_insights=insights,
        dashboard_metrics=resolved_dashboard_metrics,
    )
    artifacts = publish_compact_artifacts(
        project_root=project_root,
        trucks=ordered_trucks,
        dashboard_metrics=resolved_dashboard_metrics,
        schedule_insights=insights,
        snapshot_metrics=snapshot_metrics,
        generated_at=generated_at or datetime.now(timezone.utc),
        configured_links=configured_links,
    )
    return DashboardPublishSnapshot(
        trucks=ordered_trucks,
        schedule_insights=insights,
        dashboard_metrics=resolved_dashboard_metrics,
        snapshot_metrics=snapshot_metrics,
        artifacts=artifacts,
    )


def build_dashboard_publish_payload(
    *,
    snapshot: DashboardPublishSnapshot,
    max_trucks: int,
    max_attention: int = 3,
) -> dict[str, object]:
    return build_teams_webhook_payload(
        trucks=snapshot.trucks,
        dashboard_metrics=snapshot.dashboard_metrics,
        schedule_insights=snapshot.schedule_insights,
        max_trucks=max(1, int(max_trucks)),
        max_attention=max(1, int(max_attention)),
        artifact_links=snapshot.artifacts.action_links,
        generated_at=snapshot.artifacts.generated_at,
    )


def build_sized_dashboard_publish_payload(
    *,
    snapshot: DashboardPublishSnapshot,
    max_payload_bytes: int,
    max_attention: int = 3,
    candidate_rows: tuple[int, ...] = (8, 6, 5, 4, 3),
) -> tuple[dict[str, object], int, int]:
    best_payload: dict[str, object] | None = None
    best_size: int | None = None
    best_rows = 0

    for max_rows in candidate_rows:
        payload = build_dashboard_publish_payload(
            snapshot=snapshot,
            max_trucks=max_rows,
            max_attention=max_attention,
        )
        payload_size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        if best_size is None or payload_size < best_size:
            best_payload = payload
            best_size = payload_size
            best_rows = max_rows
        if payload_size <= max_payload_bytes:
            return (payload, payload_size, max_rows)

    if best_payload is not None and best_size is not None:
        return (best_payload, best_size, best_rows)
    return ({}, 0, 0)


def write_dashboard_payload(output_path: Path, payload: dict[str, object]) -> Path:
    resolved_path = output_path.resolve() if output_path.is_absolute() else output_path
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return resolved_path


def post_json_webhook(webhook_url: str, payload: dict[str, object]) -> tuple[int, str]:
    raw = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        status = int(getattr(response, "status", response.getcode()))
        body = response.read().decode("utf-8", errors="replace")
    return (status, body)
