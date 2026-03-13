from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from database import FabricationDatabase
from metrics import compute_dashboard_metrics, compute_snapshot_metrics, sort_trucks_natural
from publish_artifacts import publish_compact_artifacts
from schedule import build_schedule_insights
from stages import Stage, stage_from_id
from teams_card import build_teams_webhook_payload

DEFAULT_TEAMS_WEBHOOK_URL = (
    "https://default97009fec357647f39ce0fc3d1496b7.b8.environment.api.powerplatform.com:443/"
    "powerautomate/automations/direct/workflows/98b3a4e7ea8c439090e2d40232163817/triggers/manual/"
    "paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=ggEqWDyQT6T3GEouJCsp0jiZPF8mgQI5j5bl4T8T4CQ"
)


def _is_truck_complete(truck) -> bool:
    active_kits = [kit for kit in truck.kits if kit.is_active]
    if not active_kits:
        return False
    return all(stage_from_id(kit.front_stage_id) == Stage.COMPLETE for kit in active_kits)


def _post_payload(webhook_url: str, payload: dict[str, object]) -> tuple[int, str]:
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export the Fabrication Flow Dashboard snapshot as a Microsoft Teams Adaptive Card webhook JSON payload."
    )
    parser.add_argument(
        "--output",
        default="_runtime/teams_dashboard_card.json",
        help="Output path for the JSON payload.",
    )
    parser.add_argument(
        "--max-trucks",
        type=int,
        default=5,
        help="Maximum truck rows to include in the card.",
    )
    parser.add_argument(
        "--summary-url",
        default="",
        help="Published URL for summary.html (for Action.OpenUrl).",
    )
    parser.add_argument(
        "--gantt-url",
        default="",
        help="Published URL for gantt.png (for Action.OpenUrl).",
    )
    parser.add_argument(
        "--status-url",
        default="",
        help="Published URL for status.json (for Action.OpenUrl).",
    )
    parser.add_argument(
        "--webhook-url",
        default=DEFAULT_TEAMS_WEBHOOK_URL,
        help="Teams/Power Automate webhook URL. Defaults to the project hardcoded endpoint.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    db = FabricationDatabase(root / "fabrication_flow.db")
    db.initialize()

    loaded_trucks = sort_trucks_natural(db.load_trucks_with_kits(active_only=True))
    active_trucks = [
        truck for truck in loaded_trucks if truck.is_visible and not _is_truck_complete(truck)
    ]

    schedule_insights = build_schedule_insights(active_trucks)
    dashboard_metrics = compute_dashboard_metrics(active_trucks, schedule_insights=schedule_insights)
    snapshot_metrics = compute_snapshot_metrics(
        active_trucks,
        schedule_insights=schedule_insights,
        dashboard_metrics=dashboard_metrics,
    )

    generated_at = datetime.now(timezone.utc)
    configured_links = {
        "summary_html_url": str(args.summary_url or "").strip(),
        "gantt_png_url": str(args.gantt_url or "").strip(),
        "status_json_url": str(args.status_url or "").strip(),
    }

    artifacts = publish_compact_artifacts(
        project_root=root,
        trucks=active_trucks,
        dashboard_metrics=dashboard_metrics,
        schedule_insights=schedule_insights,
        snapshot_metrics=snapshot_metrics,
        generated_at=generated_at,
        configured_links=configured_links,
    )

    payload = build_teams_webhook_payload(
        trucks=active_trucks,
        dashboard_metrics=dashboard_metrics,
        schedule_insights=schedule_insights,
        max_trucks=max(1, int(args.max_trucks)),
        max_attention=3,
        artifact_links=artifacts.action_links,
        generated_at=artifacts.generated_at,
    )

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = (root / output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote Adaptive Card payload: {output_path}")
    print(f"Wrote published summary: {artifacts.summary_html_path}")
    print(f"Wrote published status: {artifacts.status_json_path}")
    print(f"Wrote published gantt: {artifacts.gantt_png_path or 'not generated'}")
    print(f"Action link (dashboard): {artifacts.action_links.get('summary_html_url', '')}")
    print(f"Action link (gantt): {artifacts.action_links.get('gantt_png_url', '')}")
    print(f"Action link (json): {artifacts.action_links.get('status_json_url', '')}")

    webhook_url = str(args.webhook_url or "").strip()
    if webhook_url:
        try:
            status, body = _post_payload(webhook_url, payload)
            print(f"Webhook POST status: {status}")
            if body.strip():
                trimmed = body.strip()
                if len(trimmed) > 600:
                    trimmed = trimmed[:600] + "..."
                print(f"Webhook response: {trimmed}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
            print(f"Webhook HTTP error: {exc.code} {exc.reason}")
            if detail.strip():
                print(detail.strip())
            return 2
        except urllib.error.URLError as exc:
            print(f"Webhook URL error: {exc.reason}")
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

