# Fabrication Flow Dashboard

A PySide6 application for tracking fabrication flow and schedule signals.

## Quick start

1. Open a terminal in this folder:
   ```powershell
   cd c:\Tools\fabrication_flow_dashboard
   ```
2. Run the app:
   ```powershell
   .\.venv\Scripts\python.exe app.py
   ```
3. For live reload (preferred):
   ```powershell
   .\dev_run.bat
   ```
   During hot reload, a top banner appears. Click `Cancel Reload` on the banner within 10 seconds to keep the current session; otherwise the app auto-reloads.
4. Compatibility command (same launcher behavior):
   ```powershell
   .\.venv\Scripts\python.exe watch_and_run.py
   ```

## Truck input (V1)

- Trucks come from `truck_registry.csv` (external input).
- Required CSV columns:
  - `truck_number`
  - `day_zero`
  - `is_active`
  - `notes`
- On startup, CSV rows are synced into the local SQLite database.
- Missing CSV trucks are created with the default kit set:
  - Pumphouse
  - Console
  - Body
  - Interior
  - Exterior
- Sync is one-way and simple:
  - Existing trucks are updated for `day_zero`, `notes`, and `is_active`.
  - Trucks are not deleted from the database when removed from CSV.

## Repository structure

- `app.py` - app entrypoint and startup CSV sync
- `truck_registry.py` - CSV parsing and sync orchestration
- `main_window.py` - main dashboard UI and interactions
- `board_widget.py` - stage board rendering and drag/drop
- `iso_board_widget.py` - fixed-isometric 3D Flow board rendering and hit-testing
- `database.py` - SQLite schema and persistence
- `stages.py` - canonical `Stage` enum and metadata
- `schedule.py` - schedule insights and release/concurrency calculations
- `metrics.py` - dashboard metrics and attention signals
- `teams_card.py` - compact Microsoft Teams Adaptive Card payload builders and shared gantt rendering helpers
- `publish_artifacts.py` - published artifact generation and link resolution for Teams actions
- `export_ops_snapshot_teams_card.py` - export/post Teams webhook JSON payload
- `truck_registry.csv` - truck registry input
- `fabrication_flow.db` - local operational state

## 3D Flow Board

- The `3D Flow` tab is a fixed-camera isometric board on purpose. Keeping the camera fixed preserves a stable stage-by-row skyline for operational scanning and avoids introducing angle/orbit controls that would hide or distort schedule comparisons.
- LASER, BEND, and WELD tower base heights come from the planned operation duration already defined in `schedule_config.json`: clamp raw planned weeks to `0.25..4.0`, then map that linearly into `28..120 px`.
- The 3D board mirrors the gantt overlay row set rather than inventing a second filter. Only productive fabrication stages are shown, and rows/stages whose planned start is two weeks out or later are cropped from the isometric view to keep the near-term skyline readable.
- Active fabrication progress is shown as a fill level inside the full planned-height tower instead of shortening the tower itself.

## Technical specification

- See [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md) for:
  - architecture and module responsibilities
  - data model and workflow definitions
  - gantt rendering rules and exceptions
  - Teams payload sizing/degradation behavior

## Teams Adaptive Card payload (Fabrication Flow Dashboard)

The app includes a `Publish to Teams` button for a compact Fabrication Flow Dashboard snapshot card.

- Open the Fabrication Flow Dashboard.
- The webhook URL is pre-filled with the project default.
- Click `Publish to Teams`.
- Payload is also written to `_runtime\teams_dashboard_card.json`.

Publish order for `Publish to Teams`:
- Generate/update published artifacts first:
  - `_runtime\published\summary.html`
  - `_runtime\published\gantt.png` (when image generation succeeds)
  - `_runtime\published\status.json`
- Build compact Adaptive Card payload that links to the published artifacts.
- POST payload to the configured Teams webhook.

Optional link configuration:
- Create `_runtime\published_artifact_links.json` with:
  - `summary_html_url`
  - `gantt_png_url`
  - `status_json_url`
- Or set environment variables:
  - `FABRICATION_FLOW_SUMMARY_HTML_URL`
  - `FABRICATION_FLOW_GANTT_PNG_URL`
  - `FABRICATION_FLOW_STATUS_JSON_URL`
- If no published URLs are configured, local file URIs are used as fallback action targets.

Generate the webhook JSON payload from the same publish flow used by the UI:

```powershell
.\.venv\Scripts\python.exe export_ops_snapshot_teams_card.py --output _runtime\teams_dashboard_card.json
```

Generate and post directly to a webhook:

```powershell
.\.venv\Scripts\python.exe export_ops_snapshot_teams_card.py --webhook-url "<YOUR_WEBHOOK_URL>"
```

Provide explicit artifact URLs from CLI when needed:

```powershell
.\.venv\Scripts\python.exe export_ops_snapshot_teams_card.py `
  --summary-url "https://contoso.sharepoint.com/sites/fab/summary.html" `
  --gantt-url "https://contoso.sharepoint.com/sites/fab/gantt.png" `
  --status-url "https://contoso.sharepoint.com/sites/fab/status.json"
```

