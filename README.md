# Fabrication Flow Dashboard

A PySide6 application for tracking fabrication progress (trucks/kits/stages) and visualizing scheduling insights.

## Quick start

1. Open a terminal in this folder:
   ```powershell
   cd c:\Tools\fabrication_flow_dashboard
   ```

2. Run the app (requires the bundled `.venv`):
   ```powershell
   .\.venv\Scripts\python.exe app.py
   ```

3. For live reload while developing, use:
   ```powershell
   .\.venv\Scripts\python.exe watch_and_run.py
   ```

## Repository structure

- `app.py` — entrypoint; initializes the database and launches the UI
- `main_window.py` — main dashboard UI and widget wiring
- `database.py` — SQLite schema and persistence layer
- `schedule.py` — schedule calculation and insights logic
- `metrics.py` — metric computations for dashboard views
- `fabrication_flow.db` — local SQLite database (local state)

## Notes

- The app expects `fabrication_flow.db` in the same directory.
- The `.venv` is included for a consistent Python runtime/environment.
