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
  - Console Pack
  - Body
  - Interior Pack
  - Exterior Pack
- Sync is one-way and simple:
  - Existing trucks are updated for `day_zero`, `notes`, and `is_active`.
  - Trucks are not deleted from the database when removed from CSV.

## Repository structure

- `app.py` - app entrypoint and startup CSV sync
- `truck_registry.py` - CSV parsing and sync orchestration
- `main_window.py` - main dashboard UI and interactions
- `board_widget.py` - stage board rendering and drag/drop
- `database.py` - SQLite schema and persistence
- `stages.py` - canonical `Stage` enum and metadata
- `schedule.py` - schedule insights and release/concurrency calculations
- `metrics.py` - dashboard metrics and attention signals
- `teams_card.py` - Boss Lens to Microsoft Teams Adaptive Card payload builder
- `export_boss_lens_teams_card.py` - export/post Teams webhook JSON payload
- `truck_registry.csv` - truck registry input
- `fabrication_flow.db` - local operational state

## Teams Adaptive Card payload (Boss Lens)

Boss Lens now includes a `Publish to Teams` button.

- Open the `Boss Lens` tab.
- The webhook URL is pre-filled with the project default.
- Click `Test Auth` to send a minimal card and validate authorization first.
- Click `Publish to Teams`.
- Click `Publish My Version` to post `_runtime\boss_lens_teams_card.json` as-is (for custom edits).
- Payload is also written to `_runtime\boss_lens_teams_card.json`.

Generate the webhook JSON payload:

```powershell
.\.venv\Scripts\python.exe export_boss_lens_teams_card.py --output _runtime\boss_lens_teams_card.json
```

Generate and post directly to a webhook:

```powershell
.\.venv\Scripts\python.exe export_boss_lens_teams_card.py --webhook-url "<YOUR_WEBHOOK_URL>"
```
