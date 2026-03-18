from __future__ import annotations

import argparse
import urllib.error
from pathlib import Path

from dashboard_publish import (
    DEFAULT_TEAMS_WEBHOOK_URL,
    build_dashboard_publish_payload,
    build_dashboard_publish_snapshot,
    load_active_dashboard_trucks,
    post_json_webhook,
    write_dashboard_payload,
)
from database import FabricationDatabase


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

    active_trucks = load_active_dashboard_trucks(db)

    configured_links = {
        "summary_html_url": str(args.summary_url or "").strip(),
        "gantt_png_url": str(args.gantt_url or "").strip(),
        "status_json_url": str(args.status_url or "").strip(),
    }

    snapshot = build_dashboard_publish_snapshot(
        project_root=root,
        trucks=active_trucks,
        configured_links=configured_links,
    )

    payload = build_dashboard_publish_payload(
        snapshot=snapshot,
        max_trucks=max(1, int(args.max_trucks)),
    )

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = (root / output_path).resolve()
    output_path = write_dashboard_payload(output_path, payload)
    print(f"Wrote Adaptive Card payload: {output_path}")
    print(f"Wrote published summary: {snapshot.artifacts.summary_html_path}")
    print(f"Wrote published status: {snapshot.artifacts.status_json_path}")
    print(f"Wrote published gantt: {snapshot.artifacts.gantt_png_path or 'not generated'}")
    print(f"Action link (dashboard): {snapshot.artifacts.action_links.get('summary_html_url', '')}")
    print(f"Action link (gantt): {snapshot.artifacts.action_links.get('gantt_png_url', '')}")
    print(f"Action link (json): {snapshot.artifacts.action_links.get('status_json_url', '')}")

    webhook_url = str(args.webhook_url or "").strip()
    if webhook_url:
        try:
            status, body = post_json_webhook(webhook_url, payload)
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

