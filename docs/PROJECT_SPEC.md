# Fabrication Flow Dashboard - Project Specification

## 1. Purpose
This project is the Fabrication Flow Dashboard for tracking fabrication status by truck and kit, with schedule-aware visualization and Teams publishing.

Primary goals:
- Keep a live board of active truck kits by stage.
- Surface release/flow risk signals (late release, blockers, laser/brake/weld health).
- Publish concise snapshot cards to Microsoft Teams under payload limits.

## 2. Scope
In scope:
- Desktop UI (PySide6) for daily fabrication execution and schedule review.
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
- `board_widget.py`: stage board rendering, drag/drop interactions, and card gesture handling.
- `database.py`: SQLite schema lifecycle and CRUD operations.
- `schedule.py`: schedule insights generation from config + live truck state.
- `metrics.py`: health/risk metrics and attention signal computation.
- `teams_card.py`: Teams Adaptive Card payload builders plus compact/published gantt rendering helpers used by Teams publishing.
- `publish_artifacts.py`: published artifact generation (`summary.html`, `gantt.png`, `status.json`), link resolution, and SharePoint-synced gantt output.
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
  - release state, front/back stage span (`front_stage_id`, `back_stage_id`), fabrication positions (`front_position`, `back_position`), blocker, single PDF link, active flag.
- `KitTemplate`
  - default kit definitions used when creating/syncing trucks.

## 5. Core Workflows

## 5.1 Startup
1. Initialize DB schema.
2. Sync CSV truck registry into local DB.
3. Load active/visible trucks and kit state.
4. Build schedule insights + metrics.
5. Render board, top traffic signals, attention panel, and gantt.

Desktop publish controls:
- `Publish to Teams`: generate/update published artifacts, build the Adaptive Card payload, and POST to the configured webhook.
- `Update Published Gantt`: generate/update the published artifacts only, without posting a Teams card.

## 5.2 Kit Movement
- Drag/drop updates front stage.
- Stage span is normalized so `back_stage_id` never exceeds `front_stage_id`.
- Moving fabrication-forward can auto-normalize release state to `released`.
- Card single-click opens the kit edit dialog.
- Card double-click opens the linked PDF directly, if present.

## 5.3 Kit Editing
- Head/tail fabrication positions are edited in the popup dialog, not inline on the board.
- Position display uses the five-step stage scale rendered as `0%`, `10%`, `50%`, `90%`, `100%`.
- Kit PDF handling is intentionally single-file only.

## 6. Gantt Specification

## 6.1 Windowing
- `ALL` is the master gantt and defines the shared viewport.
- Per-truck gantt tabs reuse the same week range, label framing, and chart canvas size as `ALL`.
- The visible range is anchored to the current week start and extends forward 8 weeks, with small side padding.
- Tick labels display in `mm/dd/yy` format.

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
- Released kit: marker follows actual front-stage position within the stage window.
- Unreleased kit:
  - marker sits at laser entry until release.
  - >1 week before due = black
  - within 1 week of due = yellow
  - overdue = red
- Released kit color rules:
  - within `+/-1 week` of the master timeline = green
  - >1 week late = yellow
  - >1 week ahead = blue
  - special case: late released final-stage `WELD` never renders green; it stays yellow
- Late rows can still show a catch-up arrow to `TODAY` even when the dot remains green inside the tolerance band.

## 6.5 Tail Arrow Rules
- If `tail_stage < actual_stage`, a gray tail arrow is drawn from tail position toward actual marker.
- Special case: when tail is `RELEASE`, arrow starts at laser start.

## 6.6 Visual Aids
- Red dashed vertical line = `TODAY`, clipped to the active row area only.
- Faint weekly vertical guides, clipped to the active row area only.
- Horizontal separators between rows for readability.
- One row of clean whitespace is reserved above the gantt rows.

## 6.7 Pane and Tab Sizing
- The gantt pane is locked and auto-sized from the `ALL` gantt only.
- Per-truck tabs are rendered inside the same locked gantt frame and should not change splitter sizing.
- Signal-detail changes or attention-panel text should not resize the gantt pane.

## 6.8 Row Label Framing
- Per-truck gantts use the same truck/kit label padding widths as the `ALL` gantt.
- This keeps the left label gutter stable so the time axis does not shift between tabs.

## 6.9 Color Source of Truth
- Gantt front-dot coloring is the source of truth.
- Board card coloring follows the same status classification when a schedule anchor exists.
- If no usable schedule anchor exists, board cards fall back to neutral gray.

## 7. Teams Adaptive Card Publishing

## 7.1 Payload Constraints
- Project target cap: `28,000` bytes per webhook payload.
- Gantt image compression cap: `18,000` bytes (PNG) before fallback.
- Dashboard card is intentionally compact and avoids large embedded detail blocks.

## 7.2 Compact Dashboard Card Shape
The dashboard webhook payload is a compact live board summary:
- Top signal row:
  - traffic-light style indicators only
  - `LASER`
  - `BRAKE`
  - `WELD A`
  - `WELD B`
- Attention section:
  - red attention items only
  - wording should match the desktop attention panel for those rows
  - if empty, show `No red attention items.`
- Embedded gantt section:
  - compact image embedded in-card when it fits payload constraints
  - cue text above image: `Click to open full-size schedule`
  - image click opens the published gantt link
  - published/card gantt looks back toward unfinished work but only shows a short future horizon
- Board lanes section:
  - always visible
  - 2-column layout for better mobile wrapping
  - each truck column shows `truck | Body: <stage>` plus per-kit fact rows
- Footer behavior:
  - no bottom action buttons
  - no separate `Open Full Dashboard`, `Open Gantt Snapshot`, or `Open Published JSON` actions

## 7.3 Publish Order
Dashboard publish follows this sequence:
1. Generate/update published artifacts.
2. Resolve/confirm artifact links (SharePoint URLs preferred).
3. Build compact Adaptive Card payload.
4. POST payload to the Teams Incoming Webhook.

Quiet gantt refresh follows the same artifact-generation path, but stops before card payload build/post.

## 7.4 Degradation Strategy
For compact dashboard publish:
- Reduce per-truck row count progressively until payload fits.
- Embedded gantt attempts a compact PNG render and falls back to the published gantt link when inline image generation cannot fit constraints.
- Published/high-resolution gantt uses a shorter forward horizon than the desktop gantt and drops rows that do not intersect the visible viewport.

## 7.5 Output Artifacts
Generated payloads are written to:
- `_runtime/teams_dashboard_card.json`
- `_runtime/published/summary.html`
- `_runtime/published/gantt.png` (if image extraction succeeds)
- `_runtime/published/status.json`

## 8. Configuration
`schedule_config.json` controls:
- Day-zero anchor and truck start lag.
- Kit lag/duration defaults.
- Per-kit operation windows.
- Operation standards and cycle settings.

Hardcoded operational routing currently includes:
- `WELD B` feed kits: `Console`, `Interior`, `Exterior`

Teams artifact link resolution supports:
- `_runtime/published_artifact_links.json` keys:
  - `summary_html_url`
  - `gantt_png_url`
  - `status_json_url`
- Environment variable overrides:
  - `FABRICATION_FLOW_SUMMARY_HTML_URL`
  - `FABRICATION_FLOW_GANTT_PNG_URL`
  - `FABRICATION_FLOW_STATUS_JSON_URL`

## 9. Operational Notes
- This project currently uses a local-first architecture; runtime files under `_runtime` are operational artifacts and may change frequently.
- The main dashboard view is the only in-app page; older "tab" terminology is obsolete.
- `main_window.py`, `gantt_overlay.py`, and `teams_card.py` include inline comments documenting gantt behavior and payload fallback behavior.

## 10. Recommended Next Improvements
- Add automated payload-size tests for Teams builders.
- Add unit tests for gantt row eligibility, viewport locking, and status-color rules.
- Add a dedicated docs page for publish/degradation decision paths.
