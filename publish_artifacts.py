from __future__ import annotations
import html
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from metrics import DashboardMetrics, SnapshotMetrics
from models import Truck
from schedule import ScheduleInsights
from teams_card import render_published_gantt_png_bytes

ARTIFACT_LINK_KEYS = ("summary_html_url", "gantt_png_url", "status_json_url")
ARTIFACT_LINK_ENV = {
    "summary_html_url": "FABRICATION_FLOW_SUMMARY_HTML_URL",
    "gantt_png_url": "FABRICATION_FLOW_GANTT_PNG_URL",
    "status_json_url": "FABRICATION_FLOW_STATUS_JSON_URL",
}
SHAREPOINT_GANTT_SYNC_DIR = Path(
    r"C:\Users\athankachan\BATTLESHIELD INDUSTRIES LIMITED\Manufacturing - Fire Truck Fabrication"
)
SHAREPOINT_GANTT_FILENAME = "fabrication_gantt_hi_res.png"


@dataclass(frozen=True)
class ArtifactPublishResult:
    generated_at: datetime
    output_dir: Path
    summary_html_path: Path
    gantt_png_path: Path | None
    status_json_path: Path
    action_links: dict[str, str]


def load_configured_artifact_links(project_root: Path) -> dict[str, str]:
    configured: dict[str, str] = {key: "" for key in ARTIFACT_LINK_KEYS}
    links_file = project_root / "_runtime" / "published_artifact_links.json"

    if not links_file.exists():
        try:
            links_file.parent.mkdir(parents=True, exist_ok=True)
            links_file.write_text(json.dumps(configured, indent=2), encoding="utf-8")
        except OSError:
            pass

    if links_file.exists():
        try:
            raw = json.loads(links_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in ARTIFACT_LINK_KEYS:
                    configured[key] = str(raw.get(key, "")).strip()
        except (OSError, json.JSONDecodeError):
            pass

    for key, env_name in ARTIFACT_LINK_ENV.items():
        env_value = str(os.getenv(env_name, "")).strip()
        if env_value:
            configured[key] = env_value

    return configured


def _resolve_action_link(raw_value: str, *, project_root: Path, fallback_path: Path | None) -> str:
    value = str(raw_value or "").strip()
    if not value:
        if fallback_path is None:
            return ""
        return fallback_path.resolve().as_uri()

    if "://" in value:
        return value

    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = (project_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    return candidate.as_uri()


def _extract_gantt_png_bytes(
    *,
    trucks: list[Truck],
    schedule_insights: ScheduleInsights,
) -> bytes | None:
    return render_published_gantt_png_bytes(
        trucks=trucks,
        schedule_insights=schedule_insights,
        max_rows=max(1, len(trucks) * 8),
    )


def _truck_row_sort_key(row: Any) -> tuple[int, int, str]:
    tone_order = {"problem": 0, "caution": 1, "ok": 2}
    tone_rank = tone_order.get(str(getattr(row, "tone", "")).strip().lower(), 3)
    risk = str(getattr(row, "risk_category", "")).strip().lower()
    risk_rank = 0 if risk and risk != "in sync" else 1
    truck_number = str(getattr(row, "truck_number", "")).strip().lower()
    return (tone_rank, risk_rank, truck_number)


def _build_status_payload(
    *,
    generated_at: datetime,
    trucks: list[Truck],
    dashboard_metrics: DashboardMetrics,
    schedule_insights: ScheduleInsights,
    snapshot_metrics: SnapshotMetrics,
    action_links: dict[str, str],
) -> dict[str, Any]:
    blocked_kits = sum(
        1 for truck in trucks for kit in truck.kits if kit.is_active and str(kit.blocker or "").strip()
    )

    attention = []
    for item in dashboard_metrics.attention_items[:3]:
        attention.append(
            {
                "priority": int(item.priority),
                "title": str(item.title),
                "detail": str(item.detail),
            }
        )

    sorted_rows = sorted(snapshot_metrics.truck_rows, key=_truck_row_sort_key)
    truck_rows = []
    for row in sorted_rows:
        truck_rows.append(
            {
                "truck_number": str(row.truck_number),
                "main_stage": str(row.main_stage),
                "sync_state": str(row.sync_status),
                "risk_category": str(row.risk_category),
                "issue_summary": str(row.issue_summary),
                "tone": str(row.tone),
            }
        )

    return {
        "published_at_utc": generated_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "active_trucks": int(len(trucks)),
            "laser": str(dashboard_metrics.laser_buffer.level),
            "bend_buffer": str(dashboard_metrics.bend_buffer.level),
            "weld_feed_a": str(dashboard_metrics.weld_feed_a.level),
            "weld_feed_b": str(dashboard_metrics.weld_feed_b.level),
            "kits_behind_schedule": int(snapshot_metrics.sync_summary.behind_kits),
            "late_releases": int(len(schedule_insights.release_hold_items)),
            "blocked_kits": int(blocked_kits),
        },
        "risk_summary": attention,
        "truck_rows": truck_rows,
        "artifact_links": action_links,
    }


def _render_summary_html(
    *,
    status_payload: dict[str, Any],
    generated_at: datetime,
) -> str:
    summary = status_payload.get("summary", {})
    risk_items = status_payload.get("risk_summary", [])
    truck_rows = status_payload.get("truck_rows", [])

    summary_rows = [
        ("Active Trucks", str(summary.get("active_trucks", 0))),
        ("Laser", str(summary.get("laser", ""))),
        ("Bend Buffer", str(summary.get("bend_buffer", ""))),
        ("Weld A", str(summary.get("weld_feed_a", ""))),
        ("Weld B", str(summary.get("weld_feed_b", ""))),
        ("Kits Behind Schedule", str(summary.get("kits_behind_schedule", 0))),
        ("Blocked Kits", str(summary.get("blocked_kits", 0))),
    ]

    summary_table = "".join(
        "<tr><th>{}</th><td>{}</td></tr>".format(html.escape(label), html.escape(value))
        for label, value in summary_rows
    )

    risk_html = "".join(
        "<li><strong>{}</strong>: {}</li>".format(
            html.escape(str(item.get("title", ""))),
            html.escape(str(item.get("detail", ""))),
        )
        for item in risk_items[:5]
    )
    if not risk_html:
        risk_html = "<li>No urgent flow risks.</li>"

    truck_html = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(row.get("truck_number", ""))),
            html.escape(str(row.get("main_stage", ""))),
            html.escape(str(row.get("sync_state", ""))),
            html.escape(str(row.get("issue_summary", ""))),
        )
        for row in truck_rows[:20]
    )
    if not truck_html:
        truck_html = "<tr><td colspan='4'>No active truck rows.</td></tr>"

    generated_text = generated_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Fabrication Status</title>
  <style>
    body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 24px; color: #0F172A; }}
    h1, h2 {{ margin: 0 0 10px 0; }}
    .meta {{ color: #475569; margin-bottom: 16px; }}
    .card {{ border: 1px solid #CBD5E1; border-radius: 8px; padding: 14px; margin-bottom: 16px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #E2E8F0; text-align: left; padding: 8px; vertical-align: top; }}
    th {{ width: 280px; background: #F8FAFC; }}
    ul {{ margin: 8px 0 0 20px; padding: 0; }}
  </style>
</head>
<body>
  <h1>Fabrication Status</h1>
  <div class="meta">Published: {html.escape(generated_text)}<br/>Confirmed published snapshot.</div>
  <section class="card">
    <h2>Top Summary</h2>
    <table>{summary_table}</table>
  </section>
  <section class="card">
    <h2>Risk Summary</h2>
    <ul>{risk_html}</ul>
  </section>
  <section class="card">
    <h2>Per-Truck Summary</h2>
    <table>
      <tr><th>Truck</th><th>Main Kit Stage</th><th>Sync State</th><th>Issue</th></tr>
      {truck_html}
    </table>
  </section>
</body>
</html>
"""


def publish_compact_artifacts(
    *,
    project_root: Path,
    trucks: list[Truck],
    dashboard_metrics: DashboardMetrics,
    schedule_insights: ScheduleInsights,
    snapshot_metrics: SnapshotMetrics,
    generated_at: datetime | None = None,
    configured_links: dict[str, str] | None = None,
) -> ArtifactPublishResult:
    generated = generated_at or datetime.now(timezone.utc)
    output_dir = (project_root / "_runtime" / "published").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    summary_html_path = output_dir / "summary.html"
    status_json_path = output_dir / "status.json"
    gantt_png_path = output_dir / "gantt.png"

    gantt_png_bytes = _extract_gantt_png_bytes(
        trucks=trucks,
        schedule_insights=schedule_insights,
    )
    if gantt_png_bytes:
        gantt_png_path.write_bytes(gantt_png_bytes)
        try:
            SHAREPOINT_GANTT_SYNC_DIR.mkdir(parents=True, exist_ok=True)
            (SHAREPOINT_GANTT_SYNC_DIR / SHAREPOINT_GANTT_FILENAME).write_bytes(gantt_png_bytes)
        except OSError:
            pass
        resolved_gantt_path: Path | None = gantt_png_path
    else:
        resolved_gantt_path = gantt_png_path if gantt_png_path.exists() else None

    loaded_links = load_configured_artifact_links(project_root)
    if configured_links:
        for key in ARTIFACT_LINK_KEYS:
            value = str(configured_links.get(key, "")).strip()
            if value:
                loaded_links[key] = value

    action_links = {
        "summary_html_url": _resolve_action_link(
            loaded_links.get("summary_html_url", ""),
            project_root=project_root,
            fallback_path=summary_html_path,
        ),
        "gantt_png_url": _resolve_action_link(
            loaded_links.get("gantt_png_url", ""),
            project_root=project_root,
            fallback_path=resolved_gantt_path,
        ),
        "status_json_url": _resolve_action_link(
            loaded_links.get("status_json_url", ""),
            project_root=project_root,
            fallback_path=status_json_path,
        ),
    }

    status_payload = _build_status_payload(
        generated_at=generated,
        trucks=trucks,
        dashboard_metrics=dashboard_metrics,
        schedule_insights=schedule_insights,
        snapshot_metrics=snapshot_metrics,
        action_links=action_links,
    )
    status_json_path.write_text(json.dumps(status_payload, indent=2), encoding="utf-8")

    summary_html = _render_summary_html(
        status_payload=status_payload,
        generated_at=generated,
    )
    summary_html_path.write_text(summary_html, encoding="utf-8")

    return ArtifactPublishResult(
        generated_at=generated,
        output_dir=output_dir,
        summary_html_path=summary_html_path,
        gantt_png_path=resolved_gantt_path,
        status_json_path=status_json_path,
        action_links=action_links,
    )
