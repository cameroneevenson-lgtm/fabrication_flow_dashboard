from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from database import FabricationDatabase
from metrics import compute_boss_lens_metrics, compute_dashboard_metrics, sort_trucks_natural
from schedule import build_schedule_insights
from stages import Stage, stage_from_id
from teams_card import build_teams_webhook_payload


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
        description="Export Boss Lens summary as a Microsoft Teams Adaptive Card webhook JSON payload."
    )
    parser.add_argument(
        "--output",
        default="_runtime/boss_lens_teams_card.json",
        help="Output path for the JSON payload.",
    )
    parser.add_argument(
        "--max-trucks",
        type=int,
        default=20,
        help="Maximum truck rows to include in the card.",
    )
    parser.add_argument(
        "--webhook-url",
        default="",
        help="Optional Teams/Power Automate webhook URL. If set, the payload is posted after writing.",
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
    boss_metrics = compute_boss_lens_metrics(
        active_trucks,
        schedule_insights=schedule_insights,
        dashboard_metrics=dashboard_metrics,
    )

    payload = build_teams_webhook_payload(
        boss_metrics,
        max_trucks=max(1, int(args.max_trucks)),
        generated_at=datetime.now(timezone.utc),
    )

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = (root / output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote Adaptive Card payload: {output_path}")

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

