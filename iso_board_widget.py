from __future__ import annotations

import math
import os
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QToolTip, QVBoxLayout, QWidget

from dashboard_helpers import normalize_blocked_state_from_kit
from gantt_overlay import (
    LASER_START_POSITION,
    OverlayRow,
    classify_front_status,
    expected_position_for_week,
    normalize_position_span,
    overlay_position_to_week,
)
from metrics import WELD_FEED_B_KIT_NAMES
from models import Truck, TruckKit, pdf_link
from stages import (
    FABRICATION_STAGE_POSITION_SCALE,
    FABRICATION_STAGES,
    Stage,
    stage_from_id,
)

NEUTRAL_TOWER_COLOR = "#94A3B8"
COMPLETE_TILE_COLOR = "#64748B"
OUTLINE_COLOR = "#0F172A"
LANE_GUIDE_COLORS = {
    "laser": "#38BDF8",
    "bend": "#F59E0B",
    "weld_a": "#F97316",
    "weld_b": "#FB7185",
}


@dataclass(frozen=True)
class DisplayLane:
    key: str
    label: str
    stage: Stage


DISPLAY_LANES: tuple[DisplayLane, ...] = (
    DisplayLane(key="laser", label="LASER", stage=Stage.LASER),
    DisplayLane(key="bend", label="BEND", stage=Stage.BEND),
    DisplayLane(key="weld_a", label="WELD A", stage=Stage.WELD),
    DisplayLane(key="weld_b", label="WELD B", stage=Stage.WELD),
)

TILE_WIDTH = 84.0
TILE_DEPTH = 40.0
COLUMN_DX = 190.0
COLUMN_DY = 18.0
ROW_DX = 74.0
ROW_DY = 56.0

LEFT_MARGIN = 96.0
TOP_MARGIN = 150.0
RIGHT_MARGIN = 120.0
BOTTOM_MARGIN = 140.0

RELEASE_STUB_HEIGHT = 14
COMPLETE_SLAB_HEIGHT = 18
MISSING_WINDOW_TILE_HEIGHT = 10

MIN_DURATION_WEEKS = 0.25
MAX_DURATION_WEEKS = 4.0
MIN_TOWER_HEIGHT = 28
MAX_TOWER_HEIGHT = 120
VIEWPORT_PADDING = 18.0
FUTURE_CROP_WEEKS = 2.0
ROW_LABEL_WIDTH = 170.0
ROW_LABEL_HEIGHT = 42.0
ROW_LABEL_GAP = 18.0
SCENE_PADDING_X = 38.0
SCENE_PADDING_TOP = 30.0
SCENE_PADDING_BOTTOM = 44.0
HEADER_RECT_WIDTH = 114.0
HEADER_RECT_HEIGHT = 34.0


@dataclass(frozen=True)
class IsoBoardRow:
    row_index: int
    truck_id: int
    truck_number: str
    kit: TruckKit
    current_stage: Stage
    stage_windows: dict[Stage, tuple[float, float]]
    status_key: str
    status_color: str
    status_label: str
    blocked_reason: str


@dataclass(frozen=True)
class IsoBoardCell:
    row: IsoBoardRow
    stage: Stage
    lane_key: str
    lane_label: str
    stage_index: int
    raw_duration_weeks: float | None
    tower_height: int
    progress_ratio: float
    fill_color: str
    tooltip_text: str
    pdf_target: str
    is_current_stage: bool


@dataclass
class PaintedTower:
    cell: IsoBoardCell
    row_index: int
    kit_id: int
    lane_key: str
    base_center: QPointF
    top_polygon: QPolygonF
    left_polygon: QPolygonF
    right_polygon: QPolygonF
    body_path: QPainterPath
    bounds: QRectF


def _normalize_kit_name(value: str) -> str:
    return str(value or "").strip().lower()


def _blend_colors(primary: str, secondary: str, ratio: float) -> QColor:
    mix = max(0.0, min(1.0, float(ratio)))
    a = QColor(primary)
    b = QColor(secondary)
    return QColor(
        int(round((a.red() * (1.0 - mix)) + (b.red() * mix))),
        int(round((a.green() * (1.0 - mix)) + (b.green() * mix))),
        int(round((a.blue() * (1.0 - mix)) + (b.blue() * mix))),
    )


def _lighten(color: str, ratio: float) -> QColor:
    return _blend_colors(color, "#FFFFFF", ratio)


def _darken(color: str, ratio: float) -> QColor:
    return _blend_colors(color, "#000000", ratio)


def _status_label_for_key(status_key: str, *, blocked_reason: str) -> str:
    normalized = str(status_key or "").strip().lower()
    if normalized == "red":
        if blocked_reason:
            return f"Red - blocked ({blocked_reason})"
        return "Red - overdue / blocked"
    if normalized == "yellow":
        return "Yellow - needs attention"
    if normalized == "green":
        return "Green - on schedule"
    if normalized == "blue":
        return "Blue - ahead of schedule"
    if normalized == "black":
        return "Black - unreleased / not due"
    if normalized == "complete":
        return "Complete - finished flow stage"
    return "Neutral - no schedule anchor"


def _lane_guide_color(lane_key: str) -> QColor:
    return QColor(LANE_GUIDE_COLORS.get(str(lane_key or "").strip().lower(), "#94A3B8"))


def _format_duration_weeks(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.2f} weeks"


def _planned_weeks_to_height(duration_weeks: float) -> int:
    # Keep heights deterministic and readable: clamp the raw planned weeks and scale linearly.
    bounded = max(MIN_DURATION_WEEKS, min(MAX_DURATION_WEEKS, float(duration_weeks)))
    span = MAX_DURATION_WEEKS - MIN_DURATION_WEEKS
    ratio = 0.0 if span <= 0.0 else (bounded - MIN_DURATION_WEEKS) / span
    return int(round(MIN_TOWER_HEIGHT + (ratio * (MAX_TOWER_HEIGHT - MIN_TOWER_HEIGHT))))


def _stage_progress_ratio(kit: TruckKit, stage: Stage) -> float:
    if stage not in FABRICATION_STAGES:
        return 0.0
    if stage_from_id(kit.front_stage_id) != stage:
        return 0.0

    positions = FABRICATION_STAGE_POSITION_SCALE.get(stage, ())
    if len(positions) <= 1:
        return 0.0

    front_position, _back_position = normalize_position_span(
        getattr(kit, "front_position", None),
        getattr(kit, "back_position", None),
        front_stage_id=stage_from_id(kit.front_stage_id),
        back_stage_id=stage_from_id(kit.back_stage_id),
    )
    try:
        position_index = positions.index(int(front_position))
    except ValueError:
        return 0.0
    return float(position_index) / float(len(positions) - 1)


def _status_for_kit(
    *,
    truck_id: int,
    kit: TruckKit,
    current_week: float | None,
    kit_stage_windows_by_truck: dict[tuple[int, str, int], tuple[float, float]],
) -> tuple[str, str, str, str]:
    front_stage = stage_from_id(kit.front_stage_id)
    blocked, blocked_reason = normalize_blocked_state_from_kit(kit)
    if front_stage == Stage.COMPLETE:
        return ("complete", COMPLETE_TILE_COLOR, _status_label_for_key("complete", blocked_reason=blocked_reason), blocked_reason)

    if current_week is None:
        return ("neutral", NEUTRAL_TOWER_COLOR, _status_label_for_key("neutral", blocked_reason=blocked_reason), blocked_reason)

    baseline_windows: dict[Stage, tuple[float, float]] = {}
    kit_name_key = _normalize_kit_name(kit.kit_name)
    for stage in FABRICATION_STAGES:
        bounds = kit_stage_windows_by_truck.get((truck_id, kit_name_key, int(stage)))
        if bounds is None:
            continue
        baseline_windows[stage] = (float(bounds[0]), float(bounds[1]))

    if not baseline_windows:
        return ("neutral", NEUTRAL_TOWER_COLOR, _status_label_for_key("neutral", blocked_reason=blocked_reason), blocked_reason)

    released = bool(kit.release_state == "released" or front_stage > Stage.RELEASE)
    front_position, _back_position = normalize_position_span(
        getattr(kit, "front_position", None),
        getattr(kit, "back_position", None),
        front_stage_id=front_stage,
        back_stage_id=stage_from_id(kit.back_stage_id),
    )
    expected_position = expected_position_for_week(
        current_week=float(current_week),
        baseline_windows=baseline_windows,
    )
    display_front_position = LASER_START_POSITION if not released else front_position
    front_week = overlay_position_to_week(
        position=display_front_position,
        windows=baseline_windows,
        fallback_week=float(current_week),
    )
    expected_week = None
    if expected_position >= LASER_START_POSITION:
        expected_week = overlay_position_to_week(
            position=expected_position,
            windows=baseline_windows,
            fallback_week=float(current_week),
        )

    status_key, status_color = classify_front_status(
        released=released,
        blocked=blocked,
        front_stage=front_stage,
        expected_position=expected_position,
        front_position=display_front_position,
        expected_week=expected_week,
        front_week=front_week,
        current_week=float(current_week),
    )
    return (
        status_key,
        status_color,
        _status_label_for_key(status_key, blocked_reason=blocked_reason),
        blocked_reason,
    )


def _display_stage_for_current(current_stage: Stage) -> Stage:
    if current_stage <= Stage.RELEASE:
        return Stage.LASER
    if current_stage >= Stage.COMPLETE:
        return Stage.WELD
    return current_stage


def _is_weld_feed_b_kit(kit: TruckKit) -> bool:
    normalized = _normalize_kit_name(getattr(kit, "kit_name", ""))
    aliases = {normalized}
    if normalized.endswith(" pack"):
        aliases.add(normalized[:-5].strip())
    return any(alias in WELD_FEED_B_KIT_NAMES for alias in aliases)


def _weld_lane_key_for_kit(kit: TruckKit) -> str:
    return "weld_b" if _is_weld_feed_b_kit(kit) else "weld_a"


def _current_lane_key_for_kit(kit: TruckKit) -> str:
    current_stage = _display_stage_for_current(stage_from_id(kit.front_stage_id))
    if current_stage == Stage.WELD:
        return _weld_lane_key_for_kit(kit)
    if current_stage == Stage.BEND:
        return "bend"
    return "laser"


def _is_released_for_iso(kit: TruckKit) -> bool:
    return bool(
        str(getattr(kit, "release_state", "") or "").strip().lower() == "released"
        or stage_from_id(getattr(kit, "front_stage_id", int(Stage.RELEASE))) > Stage.RELEASE
    )


def _cell_fill_color(row: IsoBoardRow, stage: Stage) -> str:
    if _display_stage_for_current(row.current_stage) == stage:
        return row.status_color
    return _blend_colors(row.status_color, NEUTRAL_TOWER_COLOR, 0.34).name()


def _stage_diagonal_offset(stage_index: int) -> float:
    # Mirror the stage axis so earlier time sits lower-left and later time rises up-right.
    return float((len(DISPLAY_LANES) - 1) - int(stage_index)) * COLUMN_DY


def _point_along_line(start: QPointF, end: QPointF, distance: float) -> QPointF:
    dx = float(end.x()) - float(start.x())
    dy = float(end.y()) - float(start.y())
    length = math.hypot(dx, dy)
    if length <= 0.001:
        return QPointF(start)
    ratio = float(distance) / length
    return QPointF(float(start.x()) + (dx * ratio), float(start.y()) + (dy * ratio))


def _quad_around_segment(start: QPointF, end: QPointF, half_width: float) -> QPolygonF:
    dx = float(end.x()) - float(start.x())
    dy = float(end.y()) - float(start.y())
    length = math.hypot(dx, dy)
    if length <= 0.001:
        return QPolygonF()
    px = -dy / length
    py = dx / length
    return QPolygonF(
        [
            QPointF(float(start.x()) + (px * half_width), float(start.y()) + (py * half_width)),
            QPointF(float(start.x()) - (px * half_width), float(start.y()) - (py * half_width)),
            QPointF(float(end.x()) - (px * half_width), float(end.y()) - (py * half_width)),
            QPointF(float(end.x()) + (px * half_width), float(end.y()) + (py * half_width)),
        ]
    )


def _row_horizontal_offset(row_index: int, row_count: int) -> float:
    # Mirror the board horizontally so the row stack fans down-left instead of down-right.
    return float(max(0, row_count - 1 - int(row_index))) * ROW_DX


class IsoBoardCanvas(QWidget):
    kit_focused = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._rows: list[IsoBoardRow] = []
        self._painted_towers: list[PaintedTower] = []
        self._content_size = QSize(900, 520)
        self._scene_rect = QRectF(0.0, 0.0, 900.0, 520.0)
        self._max_tower_height = MAX_TOWER_HEIGHT
        self._selected_kit_id = -1
        self._hovered_key: tuple[int, str] | None = None
        self._dark_mode = False

        self.setMouseTracking(True)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(1280, 820)

    def set_dark_mode(self, dark_mode: bool) -> None:
        updated = bool(dark_mode)
        if self._dark_mode == updated:
            return
        self._dark_mode = updated
        self.update()

    def set_data(
        self,
        trucks: list[Truck],
        current_week: float | None,
        kit_stage_windows_by_truck: dict[tuple[int, str, int], tuple[float, float]] | None = None,
        overlay_rows: list[OverlayRow] | None = None,
    ) -> None:
        stage_windows_map = kit_stage_windows_by_truck or {}
        rows = self._build_rows(
            trucks=trucks,
            current_week=current_week,
            stage_windows_map=stage_windows_map,
            overlay_rows=overlay_rows or [],
        )

        self._rows = rows
        if self._selected_kit_id > 0 and not any(int(row.kit.id or -1) == self._selected_kit_id for row in self._rows):
            self._selected_kit_id = -1
        self._rebuild_geometry()

    def _build_rows(
        self,
        *,
        trucks: list[Truck],
        current_week: float | None,
        stage_windows_map: dict[tuple[int, str, int], tuple[float, float]],
        overlay_rows: list[OverlayRow],
    ) -> list[IsoBoardRow]:
        if overlay_rows:
            overlay_based_rows = self._build_rows_from_overlay_rows(
                trucks=trucks,
                overlay_rows=overlay_rows,
                current_week=current_week,
            )
            if overlay_based_rows:
                return overlay_based_rows

        rows: list[IsoBoardRow] = []
        for truck in trucks:
            truck_id = int(truck.id or -1)
            if truck_id <= 0:
                continue
            active_kits = [kit for kit in sorted(truck.kits, key=lambda value: value.kit_order) if kit.is_active]
            for kit in active_kits:
                if not _is_released_for_iso(kit):
                    continue
                stage_windows: dict[Stage, tuple[float, float]] = {}
                for stage in FABRICATION_STAGES:
                    bounds = stage_windows_map.get((truck_id, _normalize_kit_name(kit.kit_name), int(stage)))
                    if bounds is None:
                        continue
                    stage_windows[stage] = (float(bounds[0]), float(bounds[1]))
                stage_windows = self._crop_stage_windows(stage_windows, current_week=current_week)
                if not stage_windows:
                    continue
                status_key, status_color, status_label, blocked_reason = _status_for_kit(
                    truck_id=truck_id,
                    kit=kit,
                    current_week=current_week,
                    kit_stage_windows_by_truck=stage_windows_map,
                )
                rows.append(
                    IsoBoardRow(
                        row_index=len(rows),
                        truck_id=truck_id,
                        truck_number=str(truck.truck_number or "").strip(),
                        kit=kit,
                        current_stage=stage_from_id(kit.front_stage_id),
                        stage_windows=stage_windows,
                        status_key=status_key,
                        status_color=status_color,
                        status_label=status_label,
                        blocked_reason=blocked_reason,
                    )
                )
        return rows

    def _build_rows_from_overlay_rows(
        self,
        *,
        trucks: list[Truck],
        overlay_rows: list[OverlayRow],
        current_week: float | None,
    ) -> list[IsoBoardRow]:
        kit_lookup: dict[tuple[str, str], tuple[int, str, TruckKit]] = {}
        for truck in trucks:
            truck_id = int(truck.id or -1)
            if truck_id <= 0:
                continue
            truck_number = str(truck.truck_number or "").strip()
            for kit in truck.kits:
                if not kit.is_active:
                    continue
                key = (truck_number, _normalize_kit_name(kit.kit_name))
                kit_lookup[key] = (truck_id, truck_number, kit)

        rows: list[IsoBoardRow] = []
        for overlay_row in overlay_rows:
            if not bool(getattr(overlay_row, "released", False)):
                continue
            truck_number, kit_name = self._parse_overlay_label(overlay_row.row_label)
            match = kit_lookup.get((truck_number, _normalize_kit_name(kit_name)))
            if match is None:
                continue
            truck_id, clean_truck_number, kit = match
            stage_windows = self._crop_stage_windows(
                {
                    stage: (float(bounds[0]), float(bounds[1]))
                    for stage, bounds in overlay_row.windows.items()
                    if stage in FABRICATION_STAGES
                },
                current_week=current_week,
            )
            if not stage_windows:
                continue
            rows.append(
                IsoBoardRow(
                    row_index=len(rows),
                    truck_id=truck_id,
                    truck_number=clean_truck_number,
                    kit=kit,
                    current_stage=stage_from_id(kit.front_stage_id),
                    stage_windows=stage_windows,
                    status_key=str(overlay_row.status_key or "").strip(),
                    status_color=str(overlay_row.status_color or NEUTRAL_TOWER_COLOR).strip() or NEUTRAL_TOWER_COLOR,
                    status_label=_status_label_for_key(
                        str(overlay_row.status_key or "").strip(),
                        blocked_reason=str(overlay_row.blocked_reason or "").strip(),
                    ),
                    blocked_reason=str(overlay_row.blocked_reason or "").strip(),
                )
            )
        return rows

    @staticmethod
    def _parse_overlay_label(value: str) -> tuple[str, str]:
        parts = str(value or "").split("|", 1)
        truck_number = parts[0].strip() if parts else ""
        kit_name = parts[1].strip() if len(parts) > 1 else ""
        return (truck_number, kit_name)

    @staticmethod
    def _crop_stage_windows(
        stage_windows: dict[Stage, tuple[float, float]],
        *,
        current_week: float | None,
    ) -> dict[Stage, tuple[float, float]]:
        if current_week is None:
            return dict(stage_windows)
        cutoff_week = float(current_week) + FUTURE_CROP_WEEKS
        return {
            stage: bounds
            for stage, bounds in stage_windows.items()
            if float(bounds[0]) < cutoff_week
        }

    def _rebuild_geometry(self) -> None:
        towers: list[PaintedTower] = []
        max_height = COMPLETE_SLAB_HEIGHT
        row_count = max(len(self._rows), 1)
        for row in self._rows:
            for stage_index, lane in enumerate(DISPLAY_LANES):
                cell = self._build_cell(row=row, lane=lane, stage_index=stage_index)
                if cell is None:
                    continue
                center = QPointF(
                    LEFT_MARGIN + (stage_index * COLUMN_DX) + _row_horizontal_offset(row.row_index, row_count),
                    TOP_MARGIN + _stage_diagonal_offset(stage_index) + (row.row_index * ROW_DY),
                )
                towers.append(self._build_painted_tower(cell=cell, center=center))
                max_height = max(max_height, cell.tower_height)

        towers.sort(key=lambda item: (item.base_center.y(), item.base_center.x()))
        scene_rect = QRectF()
        for tower in towers:
            scene_rect = tower.bounds if scene_rect.isNull() else scene_rect.united(tower.bounds)
        for stage_index, _lane in enumerate(DISPLAY_LANES):
            header_rect = self._stage_header_scene_rect(
                stage_index=stage_index,
                row_count=row_count,
                max_tower_height=max_height,
            )
            scene_rect = header_rect if scene_rect.isNull() else scene_rect.united(header_rect)
        for row in self._rows:
            label_rect = self._row_label_scene_rect(row_index=row.row_index, row_count=row_count)
            scene_rect = label_rect if scene_rect.isNull() else scene_rect.united(label_rect)
        if scene_rect.isNull():
            width = LEFT_MARGIN + ((len(DISPLAY_LANES) - 1) * COLUMN_DX) + TILE_WIDTH + RIGHT_MARGIN
            height = TOP_MARGIN + max_height + ((len(DISPLAY_LANES) - 1) * COLUMN_DY) + TILE_DEPTH + BOTTOM_MARGIN
            scene_rect = QRectF(0.0, 0.0, float(width), float(height))
        scene_rect = scene_rect.adjusted(
            -SCENE_PADDING_X,
            -SCENE_PADDING_TOP,
            SCENE_PADDING_X,
            SCENE_PADDING_BOTTOM,
        )

        self._painted_towers = towers
        self._max_tower_height = max_height
        self._scene_rect = scene_rect
        self._content_size = QSize(
            max(1, int(round(scene_rect.width()))),
            max(1, int(round(scene_rect.height()))),
        )
        self.update()

    def resizeEvent(self, event):  # type: ignore[override]
        self.update()
        super().resizeEvent(event)

    def _build_cell(
        self,
        *,
        row: IsoBoardRow,
        lane: DisplayLane,
        stage_index: int,
    ) -> IsoBoardCell | None:
        stage = lane.stage
        if stage == Stage.WELD and lane.key != _weld_lane_key_for_kit(row.kit):
            return None

        raw_duration_weeks: float | None = None
        tower_height = 0
        progress_ratio = 0.0
        current_lane_key = _current_lane_key_for_kit(row.kit)

        bounds = row.stage_windows.get(stage)
        if bounds is None:
            if current_lane_key != lane.key:
                return None
            tower_height = MISSING_WINDOW_TILE_HEIGHT
        else:
            raw_duration_weeks = max(0.0, float(bounds[1]) - float(bounds[0]))
            tower_height = _planned_weeks_to_height(raw_duration_weeks)
            progress_ratio = _stage_progress_ratio(row.kit, stage)

        status_label = row.status_label
        if stage in FABRICATION_STAGES and raw_duration_weeks is None:
            status_label = "Neutral - current stage has no planned window"

        tooltip_lines = [
            f"Truck: {row.truck_number}",
            f"Kit: {row.kit.kit_name}",
            f"Stage: {lane.label}",
            f"Planned duration: {_format_duration_weeks(raw_duration_weeks)}",
            f"Status: {status_label}",
        ]
        if row.blocked_reason:
            tooltip_lines.append(f"Blocker: {row.blocked_reason}")

        return IsoBoardCell(
            row=row,
            stage=stage,
            lane_key=lane.key,
            lane_label=lane.label,
            stage_index=stage_index,
            raw_duration_weeks=raw_duration_weeks,
            tower_height=tower_height,
            progress_ratio=progress_ratio,
            fill_color=_cell_fill_color(row, stage),
            tooltip_text="\n".join(tooltip_lines),
            pdf_target=pdf_link(getattr(row.kit, "pdf_links", "")),
            is_current_stage=current_lane_key == lane.key,
        )

    def _build_painted_tower(self, *, cell: IsoBoardCell, center: QPointF) -> PaintedTower:
        top_polygon, left_polygon, right_polygon, path, bounds = self._build_tower_geometry(
            center=center,
            height=float(cell.tower_height),
        )
        return PaintedTower(
            cell=cell,
            row_index=cell.row.row_index,
            kit_id=int(cell.row.kit.id or -1),
            lane_key=cell.lane_key,
            base_center=center,
            top_polygon=top_polygon,
            left_polygon=left_polygon,
            right_polygon=right_polygon,
            body_path=path,
            bounds=bounds,
        )

    def _build_tower_geometry(
        self,
        *,
        center: QPointF,
        height: float,
        width: float = TILE_WIDTH,
        depth: float = TILE_DEPTH,
    ) -> tuple[QPolygonF, QPolygonF, QPolygonF, QPainterPath, QRectF]:
        half_width = width / 2.0
        half_depth = depth / 2.0
        x = float(center.x())
        y = float(center.y())

        top_polygon = QPolygonF(
            [
                QPointF(x, y - height - half_depth),
                QPointF(x + half_width, y - height),
                QPointF(x, y - height + half_depth),
                QPointF(x - half_width, y - height),
            ]
        )
        left_polygon = QPolygonF(
            [
                QPointF(x - half_width, y - height),
                QPointF(x, y - height + half_depth),
                QPointF(x, y + half_depth),
                QPointF(x - half_width, y),
            ]
        )
        right_polygon = QPolygonF(
            [
                QPointF(x, y - height + half_depth),
                QPointF(x + half_width, y - height),
                QPointF(x + half_width, y),
                QPointF(x, y + half_depth),
            ]
        )

        path = QPainterPath()
        path.addPolygon(top_polygon)
        path.addPolygon(left_polygon)
        path.addPolygon(right_polygon)
        bounds = path.boundingRect()
        return (top_polygon, left_polygon, right_polygon, path, bounds)

    def paintEvent(self, event):  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self._paint_background(painter)
        if not self._rows:
            self._paint_empty_state(painter)
            return
        scale, offset_x, offset_y = self._render_transform()
        painter.save()
        painter.translate(offset_x, offset_y)
        painter.scale(scale, scale)
        self._paint_board_guides(painter)
        for tower in self._painted_towers:
            self._paint_tower(painter, tower)
        painter.restore()
        self._paint_row_labels(painter, scale=scale, offset_x=offset_x, offset_y=offset_y)
        self._paint_stage_headers(painter, scale=scale, offset_x=offset_x, offset_y=offset_y)
        self._paint_focus_card(painter)

    def _render_transform(self) -> tuple[float, float, float]:
        scene_rect = QRectF(self._scene_rect)
        scene_width = max(1.0, float(scene_rect.width()))
        scene_height = max(1.0, float(scene_rect.height()))
        available_rect = QRectF(self.rect()).adjusted(
            VIEWPORT_PADDING,
            VIEWPORT_PADDING,
            -VIEWPORT_PADDING,
            -VIEWPORT_PADDING,
        )
        if available_rect.width() <= 1.0 or available_rect.height() <= 1.0:
            return (1.0, 0.0, 0.0)

        scale_x = available_rect.width() / scene_width
        scale_y = available_rect.height() / scene_height
        scale = max(0.1, min(scale_x, scale_y))
        content_width = scene_width * scale
        content_height = scene_height * scale
        offset_x = available_rect.left() + ((available_rect.width() - content_width) / 2.0) - (scene_rect.left() * scale)
        offset_y = available_rect.top() + ((available_rect.height() - content_height) / 2.0) - (scene_rect.top() * scale)
        return (scale, offset_x, offset_y)

    def _map_to_scene(self, position: QPointF) -> QPointF:
        scale, offset_x, offset_y = self._render_transform()
        if scale <= 0.0:
            return QPointF(position)
        return QPointF(
            (float(position.x()) - offset_x) / scale,
            (float(position.y()) - offset_y) / scale,
        )

    @staticmethod
    def _map_from_scene(position: QPointF, *, scale: float, offset_x: float, offset_y: float) -> QPointF:
        return QPointF(
            offset_x + (float(position.x()) * scale),
            offset_y + (float(position.y()) * scale),
        )

    def _paint_background(self, painter: QPainter) -> None:
        rect = self.rect()
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        if self._dark_mode:
            gradient.setColorAt(0.0, QColor("#07111B"))
            gradient.setColorAt(1.0, QColor("#0B1B2A"))
        else:
            gradient.setColorAt(0.0, QColor("#F8FBFF"))
            gradient.setColorAt(1.0, QColor("#EAF0F6"))
        painter.fillRect(rect, gradient)
        glow = QLinearGradient(rect.topLeft(), rect.topRight())
        if self._dark_mode:
            glow.setColorAt(0.0, QColor(18, 52, 78, 56))
            glow.setColorAt(0.45, QColor(36, 112, 158, 22))
            glow.setColorAt(1.0, QColor(7, 17, 27, 0))
        else:
            glow.setColorAt(0.0, QColor(255, 255, 255, 180))
            glow.setColorAt(0.45, QColor(215, 232, 247, 72))
            glow.setColorAt(1.0, QColor(248, 251, 255, 0))
        painter.fillRect(rect, glow)

    def _paint_empty_state(self, painter: QPainter) -> None:
        painter.save()
        painter.setPen(QColor("#88A5BA") if self._dark_mode else QColor("#64748B"))
        painter.drawText(self.rect(), Qt.AlignCenter, "No active truck/kit rows available for the 3D Flow board.")
        painter.restore()

    def _paint_stage_headers(self, painter: QPainter, *, scale: float, offset_x: float, offset_y: float) -> None:
        painter.save()
        row_count = max(len(self._rows), 1)
        for stage_index, lane in enumerate(DISPLAY_LANES):
            scene_rect = self._stage_header_scene_rect(
                stage_index=stage_index,
                row_count=row_count,
                max_tower_height=self._max_tower_height,
            )
            scene_center = scene_rect.center()
            center = self._map_from_scene(scene_center, scale=scale, offset_x=offset_x, offset_y=offset_y)
            rect = QRectF(center.x() - (HEADER_RECT_WIDTH / 2.0), center.y() - (HEADER_RECT_HEIGHT / 2.0), HEADER_RECT_WIDTH, HEADER_RECT_HEIGHT)
            fill = QColor("#163147") if self._dark_mode else QColor("#FFFFFF")
            border = QColor("#5D7C96") if self._dark_mode else QColor("#CBD5E1")
            text_color = QColor("#CBEAFF") if self._dark_mode else QColor("#334155")
            accent = _lane_guide_color(lane.key)
            accent.setAlpha(255 if self._dark_mode else 224)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 22 if self._dark_mode else 12))
            painter.drawRoundedRect(rect.adjusted(0.0, 3.0, 0.0, 3.0), 9.0, 9.0)
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 9.0, 9.0)
            painter.fillRect(QRectF(rect.left() + 10.0, rect.bottom() - 5.0, rect.width() - 20.0, 3.0), accent)
            painter.setPen(text_color)
            font = painter.font()
            font.setBold(True)
            font.setPointSize(10)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignCenter, lane.label)
        painter.restore()

    def _paint_board_guides(self, painter: QPainter) -> None:
        row_count = len(self._rows)
        if row_count <= 0:
            return

        neutral_line_color = QColor("#88A5BA") if self._dark_mode else QColor("#94A3B8")
        neutral_line_color.setAlpha(72 if self._dark_mode else 92)
        floor_outline = QColor("#4E7493") if self._dark_mode else QColor("#CBD5E1")
        floor_outline.setAlpha(86 if self._dark_mode else 120)

        painter.save()
        for stage_index, lane in enumerate(DISPLAY_LANES):
            lane_centers = [
                QPointF(
                    LEFT_MARGIN + (stage_index * COLUMN_DX) + _row_horizontal_offset(row_index, row_count),
                    TOP_MARGIN + _stage_diagonal_offset(stage_index) + (row_index * ROW_DY),
                )
                for row_index in range(row_count)
            ]
            if not lane_centers:
                continue

            lane_color = _lane_guide_color(lane.key)
            lane_fill = QColor(lane_color)
            lane_fill.setAlpha(34 if self._dark_mode else 28)
            lane_border = QColor(lane_color)
            lane_border.setAlpha(82 if self._dark_mode else 74)
            lane_centerline = QColor(lane_color)
            lane_centerline.setAlpha(118 if self._dark_mode else 106)
            floor_fill = QColor(lane_color)
            floor_fill.setAlpha(22 if self._dark_mode else 18)

            band_start = lane_centers[0]
            band_end = lane_centers[-1]
            if row_count == 1:
                band_start = QPointF(float(band_start.x()) - 46.0, float(band_start.y()) + 20.0)
                band_end = QPointF(float(band_end.x()) + 46.0, float(band_end.y()) - 20.0)
            else:
                band_start = _point_along_line(lane_centers[0], lane_centers[-1], -40.0)
                band_end = _point_along_line(lane_centers[-1], lane_centers[0], -40.0)

            lane_band = _quad_around_segment(band_start, band_end, 28.0)
            if not lane_band.isEmpty():
                painter.setPen(QPen(lane_border, 1.2))
                painter.setBrush(lane_fill)
                painter.drawPolygon(lane_band)

            painter.setPen(QPen(lane_centerline, 1.8, Qt.SolidLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(band_start, band_end)

            painter.setPen(QPen(neutral_line_color, 1.0, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(
                QPointF(lane_centers[0].x() - (TILE_WIDTH * 0.35), lane_centers[0].y() + (TILE_DEPTH * 0.55)),
                QPointF(lane_centers[-1].x() - (TILE_WIDTH * 0.35), lane_centers[-1].y() + (TILE_DEPTH * 0.55)),
            )
            painter.drawLine(
                QPointF(lane_centers[0].x() + (TILE_WIDTH * 0.35), lane_centers[0].y()),
                QPointF(lane_centers[-1].x() + (TILE_WIDTH * 0.35), lane_centers[-1].y()),
            )

            painter.setPen(QPen(floor_outline, 1.0))
            painter.setBrush(floor_fill)
            for center in lane_centers:
                floor_top, _left, _right, _path, _bounds = self._build_tower_geometry(
                    center=center,
                    height=0.0,
                    width=TILE_WIDTH - 8.0,
                    depth=TILE_DEPTH - 8.0,
                )
                painter.drawPolygon(floor_top)

        towers_by_row: dict[int, list[PaintedTower]] = {}
        for tower in self._painted_towers:
            towers_by_row.setdefault(tower.row_index, []).append(tower)

        for row in self._rows:
            row_towers = sorted(towers_by_row.get(row.row_index, []), key=lambda item: item.cell.stage_index)
            if not row_towers:
                continue

            row_color = QColor(row.status_color)
            if not row_color.isValid():
                row_color = QColor(NEUTRAL_TOWER_COLOR)
            row_line_color = QColor(row_color)
            row_line_color.setAlpha(138 if self._dark_mode else 112)
            row_fill_color = QColor(row_color)
            row_fill_color.setAlpha(90 if self._dark_mode else 74)

            label_rect = self._row_label_scene_rect(row_index=row.row_index, row_count=row_count)
            row_anchor = QPointF(label_rect.right() + 10.0, label_rect.center().y())
            row_points = [row_anchor] + [
                QPointF(float(tower.base_center.x()) - 6.0, float(tower.base_center.y()) + (TILE_DEPTH * 0.12))
                for tower in row_towers
            ]

            painter.setPen(QPen(row_line_color, 2.2, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            for start, end in zip(row_points, row_points[1:]):
                painter.drawLine(start, end)

            painter.setPen(Qt.NoPen)
            painter.setBrush(row_fill_color)
            painter.drawEllipse(row_anchor, 4.2, 4.2)
            for point in row_points[1:]:
                painter.drawEllipse(point, 3.6, 3.6)
        painter.restore()

    def _paint_row_labels(self, painter: QPainter, *, scale: float, offset_x: float, offset_y: float) -> None:
        if not self._rows:
            return

        painter.save()
        for row in self._rows:
            scene_rect = self._row_label_scene_rect(
                row_index=row.row_index,
                row_count=len(self._rows),
            )
            top_left = self._map_from_scene(scene_rect.topLeft(), scale=scale, offset_x=offset_x, offset_y=offset_y)
            bottom_right = self._map_from_scene(scene_rect.bottomRight(), scale=scale, offset_x=offset_x, offset_y=offset_y)
            rect = QRectF(top_left, bottom_right).normalized()

            fill = QColor("#13283C") if self._dark_mode else QColor("#FFFFFF")
            border = QColor("#4E7493") if self._dark_mode else QColor("#CBD5E1")
            subtle = QColor("#8FB4D4") if self._dark_mode else QColor("#64748B")
            status_chip = QColor(row.status_color)
            if not status_chip.isValid():
                status_chip = QColor(NEUTRAL_TOWER_COLOR)

            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 26 if self._dark_mode else 12))
            painter.drawRoundedRect(rect.adjusted(0.0, 3.0, 0.0, 3.0), 10.0, 10.0)
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 10.0, 10.0)

            chip_rect = QRectF(rect.left() + 10.0, rect.center().y() - 7.0, 14.0, 14.0)
            painter.setPen(Qt.NoPen)
            painter.setBrush(status_chip)
            painter.drawEllipse(chip_rect)

            text_left = rect.left() + 30.0
            text_width = rect.width() - 40.0

            truck_font = painter.font()
            truck_font.setBold(True)
            truck_font.setPointSize(9)
            painter.setFont(truck_font)
            painter.setPen(QColor("#E2F3FF") if self._dark_mode else QColor("#0F172A"))
            truck_text = painter.fontMetrics().elidedText(row.truck_number, Qt.ElideRight, int(text_width))
            painter.drawText(
                QRectF(text_left, rect.top() + 5.0, text_width, 16.0),
                Qt.AlignLeft | Qt.AlignVCenter,
                truck_text,
            )

            kit_font = painter.font()
            kit_font.setBold(False)
            kit_font.setPointSize(8)
            painter.setFont(kit_font)
            painter.setPen(subtle)
            kit_text = painter.fontMetrics().elidedText(str(row.kit.kit_name or ""), Qt.ElideRight, int(text_width))
            painter.drawText(
                QRectF(text_left, rect.bottom() - 19.0, text_width, 14.0),
                Qt.AlignLeft | Qt.AlignVCenter,
                kit_text,
            )
        painter.restore()

    def _paint_focus_card(self, painter: QPainter) -> None:
        painter.save()
        card_rect = QRectF(18.0, 18.0, min(320.0, max(250.0, self.width() * 0.26)), 100.0)
        fill = QColor("#10253A") if self._dark_mode else QColor("#FFFFFF")
        border = QColor("#4E7493") if self._dark_mode else QColor("#CBD5E1")
        muted = QColor("#9CCBE8") if self._dark_mode else QColor("#64748B")
        title = QColor("#E2F3FF") if self._dark_mode else QColor("#0F172A")
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 28 if self._dark_mode else 12))
        painter.drawRoundedRect(card_rect.adjusted(0.0, 4.0, 0.0, 4.0), 12.0, 12.0)
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(fill)
        painter.drawRoundedRect(card_rect, 12.0, 12.0)

        focus = self._focused_tower()
        if focus is None:
            painter.setPen(title)
            header_font = painter.font()
            header_font.setBold(True)
            header_font.setPointSize(11)
            painter.setFont(header_font)
            painter.drawText(QRectF(card_rect.left() + 14.0, card_rect.top() + 10.0, card_rect.width() - 28.0, 18.0), Qt.AlignLeft | Qt.AlignVCenter, "3D Flow")
            painter.setPen(muted)
            body_font = painter.font()
            body_font.setBold(False)
            body_font.setPointSize(9)
            painter.setFont(body_font)
            painter.drawText(
                QRectF(card_rect.left() + 14.0, card_rect.top() + 34.0, card_rect.width() - 28.0, 48.0),
                Qt.TextWordWrap,
                "Hover a tower to inspect it. Click to pin a row and double-click to open its PDF when one is linked.",
            )
            painter.restore()
            return

        cell = focus.cell
        color_chip = QColor(cell.fill_color)
        if not color_chip.isValid():
            color_chip = QColor(NEUTRAL_TOWER_COLOR)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color_chip)
        painter.drawRoundedRect(QRectF(card_rect.left() + 14.0, card_rect.top() + 14.0, 10.0, 38.0), 5.0, 5.0)

        header_font = painter.font()
        header_font.setBold(True)
        header_font.setPointSize(10)
        painter.setFont(header_font)
        painter.setPen(title)
        painter.drawText(
            QRectF(card_rect.left() + 32.0, card_rect.top() + 12.0, card_rect.width() - 46.0, 18.0),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"{cell.row.truck_number}  {cell.row.kit.kit_name}",
        )

        painter.setPen(muted)
        body_font = painter.font()
        body_font.setBold(False)
        body_font.setPointSize(8)
        painter.setFont(body_font)
        painter.drawText(
            QRectF(card_rect.left() + 32.0, card_rect.top() + 30.0, card_rect.width() - 46.0, 18.0),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"{cell.lane_label}  |  {cell.row.status_label}",
        )

        progress_text = "Progress n/a"
        if cell.is_current_stage:
            progress_text = f"Progress {int(round(max(0.0, min(1.0, cell.progress_ratio)) * 100.0))}%"
        painter.setPen(title)
        detail_text = f"Planned {_format_duration_weeks(cell.raw_duration_weeks)}   {progress_text}"
        painter.drawText(
            QRectF(card_rect.left() + 14.0, card_rect.top() + 60.0, card_rect.width() - 28.0, 16.0),
            Qt.AlignLeft | Qt.AlignVCenter,
            detail_text,
        )
        painter.setPen(muted)
        footer_text = "Linked PDF available" if cell.pdf_target else "No linked PDF"
        painter.drawText(
            QRectF(card_rect.left() + 14.0, card_rect.top() + 78.0, card_rect.width() - 28.0, 14.0),
            Qt.AlignLeft | Qt.AlignVCenter,
            footer_text,
        )
        painter.restore()

    def _paint_tower(self, painter: QPainter, tower: PaintedTower) -> None:
        cell = tower.cell
        outline_width = 2.0 if cell.is_current_stage else 1.0
        is_focused = tower.kit_id == self._selected_kit_id or self._hovered_key == (tower.kit_id, tower.lane_key)
        if is_focused:
            outline_width = 2.4

        base_color = cell.fill_color
        if cell.is_current_stage and cell.progress_ratio > 0.0:
            top_color = _lighten(base_color, 0.48)
            left_color = _blend_colors(base_color, NEUTRAL_TOWER_COLOR, 0.46)
            right_color = _blend_colors(base_color, NEUTRAL_TOWER_COLOR, 0.56)
        else:
            top_color = _lighten(base_color, 0.18)
            left_color = _darken(base_color, 0.14)
            right_color = _darken(base_color, 0.26)
        outline_color = _lighten(OUTLINE_COLOR, 0.45) if self._dark_mode else QColor(OUTLINE_COLOR)

        painter.save()
        if is_focused:
            glow = _lighten(base_color, 0.55)
            glow.setAlpha(58 if self._dark_mode else 48)
            painter.setPen(QPen(glow, 7.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(tower.body_path)
        painter.setPen(Qt.NoPen)
        shadow_color = QColor(0, 0, 0, 28 if self._dark_mode else 18)
        painter.setBrush(shadow_color)
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(point.x(), point.y() + 4.0)
                    for point in tower.right_polygon
                ]
            )
        )
        painter.setBrush(left_color)
        painter.drawPolygon(tower.left_polygon)
        painter.setBrush(right_color)
        painter.drawPolygon(tower.right_polygon)
        painter.setBrush(top_color)
        painter.drawPolygon(tower.top_polygon)

        if cell.is_current_stage and cell.progress_ratio > 0.0:
            filled_height = max(6.0, float(cell.tower_height) * max(0.0, min(1.0, float(cell.progress_ratio))))
            fill_top, fill_left, fill_right, _fill_path, _fill_bounds = self._build_tower_geometry(
                center=QPointF(tower.base_center.x(), tower.base_center.y() - 1.5),
                height=filled_height,
                width=max(18.0, TILE_WIDTH - 18.0),
                depth=max(10.0, TILE_DEPTH - 10.0),
            )
            painter.setPen(Qt.NoPen)
            fill_left_color = _darken(base_color, 0.06)
            fill_left_color.setAlpha(132)
            painter.setBrush(fill_left_color)
            painter.drawPolygon(fill_left)
            fill_right_color = _darken(base_color, 0.18)
            fill_right_color.setAlpha(148)
            painter.setBrush(fill_right_color)
            painter.drawPolygon(fill_right)
            fill_top_color = _lighten(base_color, 0.08)
            fill_top_color.setAlpha(164)
            painter.setBrush(fill_top_color)
            painter.drawPolygon(fill_top)

        painter.setPen(QPen(outline_color, outline_width))
        painter.setBrush(Qt.NoBrush)
        painter.drawPolygon(tower.left_polygon)
        painter.drawPolygon(tower.right_polygon)
        painter.drawPolygon(tower.top_polygon)

        if cell.is_current_stage:
            painter.setPen(QPen(_lighten(base_color, 0.55), 1.2))
            painter.drawLine(tower.top_polygon.at(3), tower.top_polygon.at(1))
        painter.restore()

    def mouseMoveEvent(self, event):  # type: ignore[override]
        tower = self._tower_at(self._map_to_scene(event.position()))
        hovered_key = None if tower is None else (tower.kit_id, tower.lane_key)
        if hovered_key != self._hovered_key:
            self._hovered_key = hovered_key
            self.update()

        if tower is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(event.globalPosition().toPoint(), tower.cell.tooltip_text, self)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self._hovered_key = None
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        tower = self._tower_at(self._map_to_scene(event.position()))
        if tower is None or tower.kit_id <= 0:
            super().mousePressEvent(event)
            return
        self._selected_kit_id = tower.kit_id
        self.kit_focused.emit(tower.kit_id)
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event):  # type: ignore[override]
        if event.button() != Qt.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        tower = self._tower_at(self._map_to_scene(event.position()))
        if tower is None:
            super().mouseDoubleClickEvent(event)
            return
        target = tower.cell.pdf_target
        if target and hasattr(os, "startfile"):
            try:
                os.startfile(target)  # type: ignore[attr-defined]
            except OSError:
                pass
        event.accept()

    def _tower_at(self, position: QPointF) -> PaintedTower | None:
        for tower in reversed(self._painted_towers):
            if not tower.bounds.contains(position):
                continue
            if tower.body_path.contains(position):
                return tower
        return None

    def _focused_tower(self) -> PaintedTower | None:
        if self._hovered_key is not None:
            hovered = next(
                (tower for tower in reversed(self._painted_towers) if (tower.kit_id, tower.lane_key) == self._hovered_key),
                None,
            )
            if hovered is not None:
                return hovered
        if self._selected_kit_id > 0:
            selected_current = next(
                (
                    tower
                    for tower in reversed(self._painted_towers)
                    if tower.kit_id == self._selected_kit_id and tower.cell.is_current_stage
                ),
                None,
            )
            if selected_current is not None:
                return selected_current
            return next(
                (tower for tower in reversed(self._painted_towers) if tower.kit_id == self._selected_kit_id),
                None,
            )
        return None

    @staticmethod
    def _row_label_scene_rect(*, row_index: int, row_count: int) -> QRectF:
        center = QPointF(
            LEFT_MARGIN + _row_horizontal_offset(row_index, row_count) - (TILE_WIDTH / 2.0) - ROW_LABEL_GAP - (ROW_LABEL_WIDTH / 2.0),
            TOP_MARGIN + _stage_diagonal_offset(0) + (row_index * ROW_DY) - 2.0,
        )
        return QRectF(
            center.x() - (ROW_LABEL_WIDTH / 2.0),
            center.y() - (ROW_LABEL_HEIGHT / 2.0),
            ROW_LABEL_WIDTH,
            ROW_LABEL_HEIGHT,
        )

    @staticmethod
    def _stage_header_scene_rect(*, stage_index: int, row_count: int, max_tower_height: int) -> QRectF:
        center = QPointF(
            LEFT_MARGIN + (stage_index * COLUMN_DX) + _row_horizontal_offset(0, row_count),
            TOP_MARGIN - max_tower_height - 56 + _stage_diagonal_offset(stage_index),
        )
        return QRectF(
            center.x() - (HEADER_RECT_WIDTH / 2.0),
            center.y() - (HEADER_RECT_HEIGHT / 2.0),
            HEADER_RECT_WIDTH,
            HEADER_RECT_HEIGHT,
        )


class IsoBoardWidget(QWidget):
    kit_focused = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._canvas = IsoBoardCanvas()
        self._canvas.kit_focused.connect(self.kit_focused.emit)
        self._canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        root_layout.addWidget(self._canvas, 1)
        self.set_dark_mode(False)

    def set_dark_mode(self, dark_mode: bool) -> None:
        self._canvas.set_dark_mode(dark_mode)
        self.setStyleSheet("background: transparent; border: none;")

    def set_data(
        self,
        trucks: list[Truck],
        current_week: float | None,
        kit_stage_windows_by_truck: dict[tuple[int, str, int], tuple[float, float]] | None = None,
        overlay_rows: list[OverlayRow] | None = None,
    ) -> None:
        self._canvas.set_data(
            trucks=trucks,
            current_week=current_week,
            kit_stage_windows_by_truck=kit_stage_windows_by_truck,
            overlay_rows=overlay_rows,
        )
