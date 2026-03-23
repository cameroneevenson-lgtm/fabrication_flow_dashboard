from __future__ import annotations

import os
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QToolTip, QVBoxLayout, QWidget

from dashboard_helpers import normalize_blocked_state_from_kit
from gantt_overlay import (
    LASER_START_POSITION,
    OverlayRow,
    STATUS_COLORS,
    classify_front_status,
    expected_position_for_week,
    normalize_position_span,
    overlay_position_to_week,
)
from models import Truck, TruckKit, pdf_link
from stages import (
    FABRICATION_STAGE_POSITION_SCALE,
    FABRICATION_STAGES,
    Stage,
    stage_from_id,
    stage_label,
)

NEUTRAL_TOWER_COLOR = "#94A3B8"
COMPLETE_TILE_COLOR = "#64748B"
OUTLINE_COLOR = "#0F172A"
DISPLAY_STAGES: tuple[Stage, ...] = FABRICATION_STAGES

TILE_WIDTH = 84.0
TILE_DEPTH = 40.0
COLUMN_DX = 132.0
COLUMN_DY = 18.0
ROW_DX = 30.0
ROW_DY = 56.0

LEFT_MARGIN = 250.0
TOP_MARGIN = 150.0
RIGHT_MARGIN = 180.0
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
    stage_id: int
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


def _cell_fill_color(row: IsoBoardRow, stage: Stage) -> str:
    if _display_stage_for_current(row.current_stage) == stage:
        return row.status_color
    return _blend_colors(row.status_color, NEUTRAL_TOWER_COLOR, 0.34).name()


def _stage_diagonal_offset(stage_index: int) -> float:
    # Mirror the stage axis so earlier time sits lower-left and later time rises up-right.
    return float((len(DISPLAY_STAGES) - 1) - int(stage_index)) * COLUMN_DY


class IsoBoardCanvas(QWidget):
    kit_focused = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._rows: list[IsoBoardRow] = []
        self._painted_towers: list[PaintedTower] = []
        self._content_size = QSize(900, 520)
        self._max_tower_height = MAX_TOWER_HEIGHT
        self._selected_kit_id = -1
        self._hovered_key: tuple[int, int] | None = None
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
            truck_number, kit_name = self._parse_overlay_label(overlay_row.row_label)
            match = kit_lookup.get((truck_number, _normalize_kit_name(kit_name)))
            if match is None:
                continue
            truck_id, clean_truck_number, kit = match
            stage_windows = self._crop_stage_windows(
                {
                    stage: (float(bounds[0]), float(bounds[1]))
                    for stage, bounds in overlay_row.windows.items()
                    if stage in DISPLAY_STAGES
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
        for row in self._rows:
            for stage_index, stage in enumerate(DISPLAY_STAGES):
                cell = self._build_cell(row=row, stage=stage, stage_index=stage_index)
                if cell is None:
                    continue
                center = QPointF(
                    LEFT_MARGIN + (stage_index * COLUMN_DX) + (row.row_index * ROW_DX),
                    TOP_MARGIN + _stage_diagonal_offset(stage_index) + (row.row_index * ROW_DY),
                )
                towers.append(self._build_painted_tower(cell=cell, center=center))
                max_height = max(max_height, cell.tower_height)

        towers.sort(key=lambda item: (item.base_center.y(), item.base_center.x()))
        row_count = max(len(self._rows), 1)
        width = int(round(LEFT_MARGIN + ((len(DISPLAY_STAGES) - 1) * COLUMN_DX) + ((row_count - 1) * ROW_DX) + TILE_WIDTH + RIGHT_MARGIN))
        height = int(round(TOP_MARGIN + max_height + ((len(DISPLAY_STAGES) - 1) * COLUMN_DY) + ((row_count - 1) * ROW_DY) + TILE_DEPTH + BOTTOM_MARGIN))

        self._painted_towers = towers
        self._max_tower_height = max_height
        self._content_size = QSize(max(900, width), max(520, height))
        self.update()

    def resizeEvent(self, event):  # type: ignore[override]
        self.update()
        super().resizeEvent(event)

    def _build_cell(
        self,
        *,
        row: IsoBoardRow,
        stage: Stage,
        stage_index: int,
    ) -> IsoBoardCell | None:
        raw_duration_weeks: float | None = None
        tower_height = 0
        progress_ratio = 0.0

        bounds = row.stage_windows.get(stage)
        if bounds is None:
            if _display_stage_for_current(row.current_stage) != stage:
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
            f"Stage: {stage_label(stage)}",
            f"Planned duration: {_format_duration_weeks(raw_duration_weeks)}",
            f"Status: {status_label}",
        ]
        if row.blocked_reason:
            tooltip_lines.append(f"Blocker: {row.blocked_reason}")

        return IsoBoardCell(
            row=row,
            stage=stage,
            stage_index=stage_index,
            raw_duration_weeks=raw_duration_weeks,
            tower_height=tower_height,
            progress_ratio=progress_ratio,
            fill_color=_cell_fill_color(row, stage),
            tooltip_text="\n".join(tooltip_lines),
            pdf_target=pdf_link(getattr(row.kit, "pdf_links", "")),
            is_current_stage=_display_stage_for_current(row.current_stage) == stage,
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
            stage_id=int(cell.stage),
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
    ) -> tuple[QPolygonF, QPolygonF, QPolygonF, QPainterPath, QRectF]:
        half_width = TILE_WIDTH / 2.0
        half_depth = TILE_DEPTH / 2.0
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
        for tower in self._painted_towers:
            self._paint_tower(painter, tower)
        painter.restore()
        self._paint_tower_labels(painter, scale=scale, offset_x=offset_x, offset_y=offset_y)
        self._paint_stage_headers(painter, scale=scale, offset_x=offset_x, offset_y=offset_y)
        self._paint_row_labels(painter, scale=scale, offset_x=offset_x, offset_y=offset_y)

    def _render_transform(self) -> tuple[float, float, float]:
        scene_width = max(1.0, float(self._content_size.width()))
        scene_height = max(1.0, float(self._content_size.height()))
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
        offset_x = available_rect.left() + ((available_rect.width() - content_width) / 2.0)
        offset_y = available_rect.top() + ((available_rect.height() - content_height) / 2.0)
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

    def _paint_empty_state(self, painter: QPainter) -> None:
        painter.save()
        painter.setPen(QColor("#88A5BA") if self._dark_mode else QColor("#64748B"))
        painter.drawText(self.rect(), Qt.AlignCenter, "No active truck/kit rows available for the 3D Flow board.")
        painter.restore()

    def _paint_stage_headers(self, painter: QPainter, *, scale: float, offset_x: float, offset_y: float) -> None:
        painter.save()
        for stage_index, stage in enumerate(DISPLAY_STAGES):
            scene_center = QPointF(
                LEFT_MARGIN + (stage_index * COLUMN_DX),
                TOP_MARGIN - self._max_tower_height - 52 + _stage_diagonal_offset(stage_index),
            )
            center = self._map_from_scene(scene_center, scale=scale, offset_x=offset_x, offset_y=offset_y)
            rect_width = max(88.0, min(114.0, 102.0 * max(scale, 0.9)))
            rect = QRectF(center.x() - (rect_width / 2.0), center.y() - 15.0, rect_width, 30.0)
            fill = QColor("#17324A") if self._dark_mode else QColor("#FFFFFF")
            border = QColor("#5D7C96") if self._dark_mode else QColor("#CBD5E1")
            text_color = QColor("#CBEAFF") if self._dark_mode else QColor("#334155")
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 8.0, 8.0)
            painter.setPen(text_color)
            font = painter.font()
            font.setBold(True)
            font.setPointSize(10)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignCenter, stage_label(stage).upper())
        painter.restore()

    def _paint_row_labels(self, painter: QPainter, *, scale: float, offset_x: float, offset_y: float) -> None:
        painter.save()
        board_left_scene = LEFT_MARGIN - (TILE_WIDTH / 2.0) - 10.0
        board_left = self._map_from_scene(
            QPointF(board_left_scene, 0.0),
            scale=scale,
            offset_x=offset_x,
            offset_y=offset_y,
        ).x()
        for row in self._rows:
            scene_center = QPointF(0.0, TOP_MARGIN + (row.row_index * ROW_DY))
            center_y = self._map_from_scene(scene_center, scale=scale, offset_x=offset_x, offset_y=offset_y).y()
            label_left = 18.0
            label_width = max(140.0, board_left - label_left - 18.0)
            label_rect = QRectF(label_left, center_y - 16.0, label_width, 32.0)
            is_selected = int(row.kit.id or -1) == self._selected_kit_id
            if is_selected:
                fill = QColor("#102437") if self._dark_mode else QColor("#E2E8F0")
                border = QColor("#78D9FF") if self._dark_mode else QColor("#94A3B8")
                painter.setPen(QPen(border, 1.0))
                painter.setBrush(fill)
                painter.drawRoundedRect(label_rect.adjusted(0.0, -2.0, 0.0, 2.0), 8.0, 8.0)

            guide_pen = QPen(QColor(98, 130, 154, 120) if self._dark_mode else QColor(148, 163, 184, 120), 1.0)
            painter.setPen(guide_pen)
            painter.drawLine(
                QPointF(label_rect.right() + 8.0, center_y),
                QPointF(board_left, center_y),
            )

            truck_font = painter.font()
            truck_font.setBold(True)
            truck_font.setPointSize(10)
            painter.setFont(truck_font)
            painter.setPen(QColor("#D9EEFF") if self._dark_mode else QColor("#0F172A"))
            painter.drawText(label_rect, Qt.AlignLeft | Qt.AlignVCenter, row.truck_number)
        painter.restore()

    def _paint_tower(self, painter: QPainter, tower: PaintedTower) -> None:
        cell = tower.cell
        outline_width = 2.0 if cell.is_current_stage else 1.0
        if tower.kit_id == self._selected_kit_id or self._hovered_key == (tower.kit_id, tower.stage_id):
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
        painter.setPen(QPen(outline_color, outline_width))
        painter.setBrush(Qt.NoBrush)
        painter.drawPolygon(tower.left_polygon)
        painter.drawPolygon(tower.right_polygon)
        painter.drawPolygon(tower.top_polygon)

        if cell.is_current_stage and cell.progress_ratio > 0.0:
            filled_height = max(6.0, float(cell.tower_height) * max(0.0, min(1.0, float(cell.progress_ratio))))
            fill_top, fill_left, fill_right, _fill_path, _fill_bounds = self._build_tower_geometry(
                center=tower.base_center,
                height=filled_height,
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(_darken(base_color, 0.06))
            painter.drawPolygon(fill_left)
            painter.setBrush(_darken(base_color, 0.18))
            painter.drawPolygon(fill_right)
            painter.setBrush(_lighten(base_color, 0.08))
            painter.drawPolygon(fill_top)
            painter.setPen(QPen(_lighten(base_color, 0.55), 1.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawPolygon(fill_top)

        if cell.is_current_stage:
            painter.setPen(QPen(_lighten(base_color, 0.55), 1.2))
            painter.drawLine(tower.top_polygon.at(3), tower.top_polygon.at(1))
        painter.restore()

    def _paint_tower_labels(self, painter: QPainter, *, scale: float, offset_x: float, offset_y: float) -> None:
        painter.save()
        for tower in self._painted_towers:
            if not tower.cell.is_current_stage:
                continue
            face_bounds = tower.right_polygon.boundingRect()
            screen_rect = QRectF(
                offset_x + (face_bounds.x() * scale) + 6.0,
                offset_y + (face_bounds.y() * scale) + 6.0,
                max(0.0, (face_bounds.width() * scale) - 12.0),
                max(0.0, (face_bounds.height() * scale) - 12.0),
            )
            if screen_rect.width() < 54.0 or screen_rect.height() < 18.0:
                continue

            label_text = str(tower.cell.row.kit.kit_name or "").strip()
            if not label_text:
                continue

            font = painter.font()
            font.setBold(True)
            font.setPointSize(9)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            elided = metrics.elidedText(label_text, Qt.ElideRight, int(screen_rect.width()))

            painter.setPen(QColor(0, 0, 0, 110))
            painter.drawText(screen_rect.translated(1.0, 1.0), Qt.AlignCenter, elided)
            painter.setPen(QColor("#F8FAFC"))
            painter.drawText(screen_rect, Qt.AlignCenter, elided)
        painter.restore()

    def mouseMoveEvent(self, event):  # type: ignore[override]
        tower = self._tower_at(self._map_to_scene(event.position()))
        hovered_key = None if tower is None else (tower.kit_id, tower.stage_id)
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
