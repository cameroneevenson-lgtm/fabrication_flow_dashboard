# Fabrication Flow Dashboard - Project Specification

## 1. Purpose
This project is an operations dashboard for tracking fabrication status by truck and kit, with schedule-aware visualization and Teams publishing.

Primary goals:
- Keep a live board of active truck kits by stage.
- Surface release/flow risk signals (late release, blockers, bend/weld health).
- Publish concise snapshot cards to Microsoft Teams under payload limits.

## 2. Scope
In scope:
- Desktop UI (PySide6) for operations planning and daily execution.
- Local SQLite persistence for truck/kit state.
- CSV truck registry sync on startup.
- Gantt render for scheduled vs actual progression.
- Teams Adaptive Card payload generation and webhook posting.

Out of scope:
- Multi-user sync/conflict resolution.
- Cloud-hosted API/service backend.
- Historical analytics warehouse.

## 3. Runtime Architecture

## 3.1 Main Modules
- `app.py`: application entrypoint and startup sequence.
- `main_window.py`: main UI shell, interaction wiring, Teams publishing, gantt panel.
- `board_widget.py`: stage board rendering and drag/drop interactions.
- `database.py`: SQLite schema lifecycle and CRUD operations.
- `schedule.py`: schedule insights generation from config + live truck state.
- `metrics.py`: health/risk metrics and attention signal computation.
- `teams_card.py`: Teams Adaptive Card payload builders (dashboard + gantt-only).
- `truck_registry.py`: CSV parsing and sync orchestration.

## 3.2 Data Sources
- SQLite database: `fabrication_flow.db`
- External CSV feed: `truck_registry.csv`
- Schedule config: `schedule_config.json`

## 4. Domain Model

## 4.1 Stages
Canonical stage sequence:
- `RELEASE`
- `LASER`
- `BEND`
- `WELD`
- `COMPLETE`

## 4.2 Core Entities
- `Truck`
  - identity, display/order metadata, planned start date, visibility.
- `TruckKit`
  - release state, front/back stage span (`front_stage_id`, `back_stage_id`), blocker, PDF links, active flag.
- `KitTemplate`
  - default kit definitions used when creating/syncing trucks.

## 5. Core Workflows

## 5.1 Startup
1. Initialize DB schema.
2. Sync CSV truck registry into local DB.
3. Load active/visible trucks and kit state.
4. Build schedule insights + metrics.
5. Render board, gantt, and summary panels.

## 5.2 Kit Movement
- Drag/drop updates front stage.
- Stage span is normalized so `back_stage_id` never exceeds `front_stage_id`.
- Moving fabrication-forward can auto-normalize release state to `released`.

## 5.3 Tail Collapse
- Tail collapse action advances `back_stage_id` toward `front_stage_id`.
- Used to indicate upstream work completion and reduce trailing span.

## 6. Gantt Specification

## 6.1 Windowing
- Current gantt view is intentionally clamped to current week +/- 8 weeks for legibility and payload stability.
- Weekly tick labels are displayed across this fixed window.

## 6.2 Row Eligibility
A kit row is rendered only when all are true:
- Truck has a schedule anchor (`truck_planned_start_week`).
- Kit is active.
- Kit front stage is not `COMPLETE`.
- Kit name exists in configured operation windows.
- At least one stage remains after stage filtering.

## 6.3 Stage Rendering
- Scheduled bars are drawn for `LASER`, `BEND`, `WELD` using configured kit windows + truck anchor.
- Upstream completed stages are hidden when `stage < tail_stage`.

## 6.4 Actual Marker Rules
- Released kit: marker follows actual front-stage window center.
- Unreleased kit:
  - Not late: marker sits on laser trailing edge.
  - Late: marker sits at laser start.
  - Late adds a red backward arrow from current-week line to marker.

## 6.5 Tail Arrow Rules
- If `tail_stage < actual_stage`, a gray tail arrow is drawn from tail position toward actual marker.
- Special case: when tail is `RELEASE`, arrow starts at laser start.

## 6.6 Visual Aids
- Red dashed vertical line = current week.
- Faint weekly vertical grid lines.
- Horizontal separators between rows for readability.

## 7. Teams Adaptive Card Publishing

## 7.1 Payload Constraints
- Project target cap: `28,000` bytes per webhook payload.
- Gantt image compression cap: `18,000` bytes (PNG) before fallback.

## 7.2 Degradation Strategy
For gantt-only publish:
- Try higher row counts first with image.
- Then reduce row count and/or disable image until size fits.
- Keep smallest viable fallback payload if no candidate fits.

For dashboard publish:
- Reduce truck rows progressively until payload fits.

## 7.3 Output Artifacts
Generated payloads are written to:
- `_runtime/teams_dashboard_card.json`
- `_runtime/teams_gantt_only_card.json`

## 8. Configuration
`schedule_config.json` controls:
- Day-zero anchor and truck start lag.
- Kit lag/duration defaults.
- Per-kit operation windows.
- Operation standards and cycle settings.

## 9. Operational Notes
- This project currently uses a local-first architecture; runtime files under `_runtime` are operational artifacts and may change frequently.
- `main_window.py` and `teams_card.py` include inline comments documenting key gantt exception behavior and payload fallback behavior.

## 10. Recommended Next Improvements
- Add automated payload-size tests for Teams builders.
- Add unit tests for gantt row eligibility and marker placement rules.
- Add a dedicated docs page for publish/degradation decision paths.
