from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta

from PySide6.QtCore import QMimeData, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gantt_overlay import (
    LASER_START_POSITION,
    STATUS_COLORS,
    classify_front_status,
    expected_position_for_week,
    normalize_position_span,
    overlay_position_to_week,
)
from models import Truck, TruckKit, first_pdf_link
from stages import STAGE_SEQUENCE, Stage, stage_from_id, stage_label

TRUCK_COL_WIDTH = 200
STAGE_COL_WIDTH = 190
TRUCK_COL_MIN_WIDTH = 124
STAGE_COL_MIN_WIDTH = 112
BOARD_COL_SPACING = 4
BOARD_COL_MARGIN = 4
ACCENT_COLORS = ["#1F4E79", "#2F6B2F", "#8A5B1F", "#7A2F6B", "#006D77", "#5A4FCF"]
DRAG_MIME_PREFIX = "kitmove"
SCHEDULE_LANE_ORDER = ["late", "this_week", "next", "future", "unplanned"]


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return (148, 163, 184)
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def _rgba(value: str, alpha: int) -> str:
    red, green, blue = _hex_to_rgb(value)
    return f"rgba({red}, {green}, {blue}, {max(0, min(255, int(alpha)))})"


def _neutral_card_color() -> str:
    return "#94A3B8"


def _normalize_kit_name(value: str) -> str:
    return str(value or "").strip().lower()


def _kit_window_signature(
    *,
    truck_id: int,
    kit_name: str,
    kit_stage_windows_by_truck: dict[tuple[int, str, int], tuple[float, float]],
) -> tuple[tuple[int, float, float], ...]:
    normalized_name = _normalize_kit_name(kit_name)
    windows: list[tuple[int, float, float]] = []
    for stage in STAGE_SEQUENCE:
        bounds = kit_stage_windows_by_truck.get((truck_id, normalized_name, int(stage)))
        if bounds is None:
            continue
        windows.append((int(stage), round(float(bounds[0]), 4), round(float(bounds[1]), 4)))
    return tuple(windows)


def _kit_render_signature(
    *,
    truck_id: int,
    kit: TruckKit,
    hold_weeks_by_id: dict[int, float],
    kit_stage_windows_by_truck: dict[tuple[int, str, int], tuple[float, float]],
) -> tuple[object, ...]:
    hold_weeks = None
    if kit.id is not None:
        raw_hold_weeks = hold_weeks_by_id.get(int(kit.id))
        if raw_hold_weeks is not None:
            hold_weeks = round(float(raw_hold_weeks), 4)

    return (
        int(kit.id or -1),
        str(kit.kit_name or "").strip(),
        int(kit.kit_order),
        bool(kit.is_main_kit),
        str(kit.release_state or "").strip(),
        int(kit.front_stage_id),
        int(kit.back_stage_id),
        int(kit.front_position),
        int(kit.back_position),
        bool(getattr(kit, "keep_tail_at_head", True)),
        str(kit.blocker or "").strip(),
        first_pdf_link(getattr(kit, "pdf_links", "")),
        hold_weeks,
        _kit_window_signature(
            truck_id=truck_id,
            kit_name=str(kit.kit_name or ""),
            kit_stage_windows_by_truck=kit_stage_windows_by_truck,
        ),
    )


def _truck_render_signature(
    *,
    truck: Truck,
    hold_weeks_by_id: dict[int, float],
    current_week: float | None,
    kit_stage_windows_by_truck: dict[tuple[int, str, int], tuple[float, float]],
) -> tuple[object, ...]:
    truck_id = int(truck.id or -1)
    active_kits = [kit for kit in sorted(truck.kits, key=lambda value: value.kit_order) if kit.is_active]
    return (
        truck_id,
        str(truck.truck_number or "").strip(),
        str(truck.client or "").strip(),
        str(truck.planned_start_date or "").strip(),
        round(float(current_week), 4) if current_week is not None else None,
        tuple(
            _kit_render_signature(
                truck_id=truck_id,
                kit=kit,
                hold_weeks_by_id=hold_weeks_by_id,
                kit_stage_windows_by_truck=kit_stage_windows_by_truck,
            )
            for kit in active_kits
        ),
    )


def _calendar_year_from_date(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").year
    except ValueError:
        return None


def _weeks_in_iso_year(year: int) -> int:
    return int(date(year, 12, 28).isocalendar().week)


def _resolve_week_parts(value: float, base_year: int) -> tuple[int, int, float]:
    whole_week = int(value)
    fraction = float(value - whole_week)
    if fraction < 0.0:
        fraction = 0.0

    year = int(base_year)
    week = whole_week
    while week < 1:
        year -= 1
        week += _weeks_in_iso_year(year)
    while week > _weeks_in_iso_year(year):
        week -= _weeks_in_iso_year(year)
        year += 1
    return (year, week, fraction)


def _resolve_week_point_date(value: float, base_year: int) -> date:
    year, week, fraction = _resolve_week_parts(value=value, base_year=base_year)
    monday = date.fromisocalendar(year, week, 1)
    return monday + timedelta(days=(fraction * 7.0))


def _clear_layout(layout: QVBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
            continue
        nested_layout = item.layout()
        if nested_layout is not None:
            _clear_layout(nested_layout)


def _encode_drag_payload(kit: TruckKit) -> str:
    kit_id = int(kit.id or -1)
    truck_id = int(kit.truck_id or -1)
    return f"{DRAG_MIME_PREFIX}:{kit_id}:{truck_id}"


def _decode_drag_payload(payload: str) -> tuple[int, int] | None:
    parts = str(payload or "").strip().split(":")
    if len(parts) != 3 or parts[0] != DRAG_MIME_PREFIX:
        return None
    try:
        kit_id = int(parts[1])
        truck_id = int(parts[2])
    except ValueError:
        return None

    if kit_id <= 0:
        return None
    return (kit_id, truck_id)


class KitCard(QFrame):
    clicked = Signal(int)

    def __init__(
        self,
        kit: TruckKit,
        state_color: str | None = None,
        dark_mode: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._kit = kit
        self._press_pos: QPoint | None = None
        self._drag_started = False
        self._dark_mode = bool(dark_mode)
        self._ignore_release_click = False
        self._single_click_delay_ms = max(int(QApplication.doubleClickInterval()) + 150, 450)
        self._last_release_ts = 0.0
        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.setInterval(self._single_click_delay_ms)
        self._single_click_timer.timeout.connect(self._flush_pending_clicks)
        self.setCursor(Qt.OpenHandCursor)
        self.setAcceptDrops(True)

        front_stage = stage_from_id(kit.front_stage_id)
        is_complete = front_stage == Stage.COMPLETE
        card_color = str(state_color or _neutral_card_color()).strip() or _neutral_card_color()
        if self._dark_mode:
            if is_complete:
                border_color = "#3A516A"
                border_width = 1
                background_color = "rgba(12, 25, 39, 225)"
                title_color = "#8FA9C0"
            else:
                border_color = card_color
                border_width = 2 if kit.is_main_kit else 1
                background_color = _rgba(card_color, 58)
                title_color = "#F8FAFC"
        else:
            if is_complete:
                border_color = "#D1D5DB"
                border_width = 1
                background_color = "#F8FAFC"
                title_color = "#64748B"
            else:
                border_color = card_color
                border_width = 2 if kit.is_main_kit else 1
                background_color = _rgba(card_color, 24)
                title_color = card_color

        self.setStyleSheet(
            f"""
            QFrame {{
                background-color: {background_color};
                border: {border_width}px solid {border_color};
                border-radius: 6px;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)

        title = kit.kit_name
        title_label = QLabel(title)
        title_label.setWordWrap(True)
        title_label.setStyleSheet(f"font-weight: 700; color: {title_color};")
        title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        layout.addWidget(title_label)

        if kit.blocker.strip():
            blocker_label = QLabel(f"Blocker: {kit.blocker.strip()}")
            blocker_label.setWordWrap(True)
            if self._dark_mode:
                blocker_label.setStyleSheet("font-size: 10px; color: #FFB099;")
            else:
                blocker_label.setStyleSheet("font-size: 10px; color: #A53E2C;")
            blocker_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            layout.addWidget(blocker_label)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton and self._kit.id is not None:
            self._press_pos = event.position().toPoint()
            self._drag_started = False
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton and self._kit.id is not None:
            self._single_click_timer.stop()
            self._ignore_release_click = True
            self._press_pos = None
            self._drag_started = False
            self.setCursor(Qt.OpenHandCursor)
            self._open_pdf_link()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):  # type: ignore[override]
        if self._press_pos is None or self._kit.id is None:
            super().mouseMoveEvent(event)
            return
        if not (event.buttons() & Qt.LeftButton):
            super().mouseMoveEvent(event)
            return

        move_distance = (event.position().toPoint() - self._press_pos).manhattanLength()
        if move_distance < QApplication.startDragDistance():
            super().mouseMoveEvent(event)
            return

        self._single_click_timer.stop()
        self._drag_started = True
        self.setCursor(Qt.ClosedHandCursor)
        drag = QDrag(self)
        mime = QMimeData()
        mime.setText(_encode_drag_payload(self._kit))
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction)
        self.setCursor(Qt.OpenHandCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            if self._ignore_release_click:
                self._ignore_release_click = False
            elif not self._drag_started and self._kit.id is not None:
                now = time.monotonic()
                double_click_window = float(self._single_click_delay_ms) / 1000.0
                if self._single_click_timer.isActive() and (now - self._last_release_ts) <= double_click_window:
                    self._single_click_timer.stop()
                    self._last_release_ts = 0.0
                    self._open_pdf_link()
                else:
                    self._last_release_ts = now
                    self._queue_single_click()
            self._press_pos = None
            self._drag_started = False
            self.setCursor(Qt.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def _queue_single_click(self) -> None:
        self._single_click_timer.stop()
        self._single_click_timer.start()

    def _flush_pending_clicks(self) -> None:
        self._last_release_ts = 0.0
        if self._kit.id is not None:
            self.clicked.emit(int(self._kit.id))

    def _open_pdf_link(self) -> None:
        link = first_pdf_link(self._kit.pdf_links)
        if not link or not hasattr(os, "startfile"):
            return
        try:
            os.startfile(link)  # type: ignore[attr-defined]
        except OSError:
            return

    def _find_stage_drop_zone(self):
        widget = self.parentWidget()
        while widget is not None:
            if isinstance(widget, StageDropZone):
                return widget
            widget = widget.parentWidget()
        return None

    def dragEnterEvent(self, event):  # type: ignore[override]
        zone = self._find_stage_drop_zone()
        payload = _decode_drag_payload(event.mimeData().text())
        if zone and payload:
            zone._set_hover(True)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event):  # type: ignore[override]
        zone = self._find_stage_drop_zone()
        payload = _decode_drag_payload(event.mimeData().text())
        if zone and payload:
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event):  # type: ignore[override]
        zone = self._find_stage_drop_zone()
        if zone:
            zone._set_hover(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):  # type: ignore[override]
        zone = self._find_stage_drop_zone()
        payload = _decode_drag_payload(event.mimeData().text())
        if not zone or not payload:
            event.ignore()
            return
        zone._set_hover(False)
        zone.kit_dropped.emit(payload[0], zone._stage_id)
        event.acceptProposedAction()


class StageDropZone(QFrame):
    kit_dropped = Signal(int, int)

    def __init__(
        self,
        stage_id: int,
        truck_id: int,
        *,
        dark_mode: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._stage_id = int(stage_from_id(stage_id))
        self._truck_id = truck_id
        self._dark_mode = bool(dark_mode)
        self.setFixedWidth(STAGE_COL_WIDTH)
        self.setAcceptDrops(True)
        self._set_hover(False)

    def _set_hover(self, is_hover: bool) -> None:
        if self._dark_mode:
            if is_hover:
                border = "#65D9FF"
                background = "rgba(14, 48, 77, 220)"
            else:
                border = "#5E7D97"
                background = "rgba(10, 22, 35, 210)"
        else:
            if is_hover:
                border = "#1D4ED8"
                background = "#E8F0FE"
            else:
                border = "#CBD5E1"
                background = "#F3F5F7"

        self.setStyleSheet(
            f"""
            QFrame {{
                background-color: {background};
                border: 1px dashed {border};
                border-radius: 6px;
            }}
            """
        )

    def _extract_payload(self, mime_text: str) -> tuple[int, int] | None:
        return _decode_drag_payload(mime_text)

    def dragEnterEvent(self, event):  # type: ignore[override]
        payload = self._extract_payload(event.mimeData().text())
        if payload:
            event.acceptProposedAction()
            self._set_hover(True)
            return
        event.ignore()

    def dragMoveEvent(self, event):  # type: ignore[override]
        payload = self._extract_payload(event.mimeData().text())
        if payload:
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event):  # type: ignore[override]
        self._set_hover(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):  # type: ignore[override]
        self._set_hover(False)
        payload = self._extract_payload(event.mimeData().text())
        if not payload:
            event.ignore()
            return
        kit_id, _truck_id = payload
        self.kit_dropped.emit(kit_id, self._stage_id)
        event.acceptProposedAction()


class StageForwardFrame(QFrame):
    def __init__(self, stage_box: StageDropZone, parent: QWidget | None = None):
        super().__init__(parent)
        self._stage_box = stage_box
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):  # type: ignore[override]
        payload = _decode_drag_payload(event.mimeData().text())
        if payload:
            self._stage_box._set_hover(True)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event):  # type: ignore[override]
        payload = _decode_drag_payload(event.mimeData().text())
        if payload:
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event):  # type: ignore[override]
        self._stage_box._set_hover(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):  # type: ignore[override]
        payload = _decode_drag_payload(event.mimeData().text())
        if not payload:
            event.ignore()
            return
        self._stage_box._set_hover(False)
        self._stage_box.kit_dropped.emit(payload[0], self._stage_box._stage_id)
        event.acceptProposedAction()


class TruckRowWidget(QFrame):
    kit_selected = Signal(int)
    kit_stage_dropped = Signal(int, int)

    def __init__(
        self,
        truck: Truck,
        accent_color: str,
        kit_release_hold_weeks_by_id: dict[int, float],
        current_week: float | None = None,
        kit_stage_windows_by_truck: dict[tuple[int, str, int], tuple[float, float]] | None = None,
        dark_mode: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._accent_color = accent_color
        self._hold_weeks_by_id = kit_release_hold_weeks_by_id
        self._current_week = current_week
        self._kit_stage_windows_by_truck = kit_stage_windows_by_truck or {}
        self._truck_id = int(truck.id or -1)
        self._dark_mode = bool(dark_mode)
        self._calendar_year = _calendar_year_from_date(truck.planned_start_date)
        row_bg = "rgba(8, 23, 37, 195)" if self._dark_mode else "#F8FAFC"
        row_border = "#4D6A84" if self._dark_mode else "#D5DEE7"
        self.setStyleSheet(
            f"""
            QFrame {{
                background-color: {row_bg};
                border: 1px solid {row_border};
                border-left: 6px solid {accent_color};
                border-radius: 8px;
            }}
            """
        )

        row_layout = QHBoxLayout(self)
        row_layout.setContentsMargins(BOARD_COL_MARGIN, BOARD_COL_MARGIN, BOARD_COL_MARGIN, BOARD_COL_MARGIN)
        row_layout.setSpacing(BOARD_COL_SPACING)

        truck_info = QWidget()
        self._truck_info = truck_info
        truck_info.setFixedWidth(TRUCK_COL_WIDTH)
        truck_info_layout = QVBoxLayout(truck_info)
        truck_info_layout.setContentsMargins(0, 0, 0, 0)
        truck_info_layout.setSpacing(1)

        truck_label = QLabel(truck.truck_number)
        truck_label.setWordWrap(True)
        truck_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        if self._dark_mode:
            truck_label.setStyleSheet("font-size: 16px; font-weight: 700; color: #D9EEFF;")
        else:
            truck_label.setStyleSheet("font-size: 16px; font-weight: 700; color: #0F172A;")
        truck_info_layout.addWidget(truck_label)

        if str(truck.client).strip():
            client_label = QLabel(truck.client.strip())
            client_label.setWordWrap(True)
            client_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            if self._dark_mode:
                client_label.setStyleSheet("font-size: 12px; color: #96B5CD;")
            else:
                client_label.setStyleSheet("font-size: 12px; color: #475569;")
            truck_info_layout.addWidget(client_label)

        if str(truck.planned_start_date).strip():
            date_label = QLabel(f"Day Zero: {truck.planned_start_date.strip()}")
            date_label.setWordWrap(True)
            date_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            if self._dark_mode:
                date_label.setStyleSheet("font-size: 12px; color: #96B5CD;")
            else:
                date_label.setStyleSheet("font-size: 12px; color: #475569;")
            truck_info_layout.addWidget(date_label)

        truck_info_layout.addStretch(1)
        row_layout.addWidget(truck_info)

        active_kits = [kit for kit in sorted(truck.kits, key=lambda x: x.kit_order) if kit.is_active]
        self._stage_boxes: list[StageDropZone] = []
        for stage in STAGE_SEQUENCE:
            stage_id = int(stage)
            stage_box = StageDropZone(
                stage_id=stage_id,
                truck_id=self._truck_id,
                dark_mode=self._dark_mode,
            )
            self._stage_boxes.append(stage_box)
            stage_box.kit_dropped.connect(self.kit_stage_dropped.emit)

            stage_layout = QVBoxLayout(stage_box)
            stage_layout.setContentsMargins(4, 4, 4, 4)
            stage_layout.setSpacing(4)

            stage_kits = [kit for kit in active_kits if int(stage_from_id(kit.front_stage_id)) == stage_id]
            if not stage_kits:
                drop_hint = StageForwardFrame(stage_box=stage_box)
                if self._dark_mode:
                    drop_hint.setStyleSheet(
                        """
                        QFrame {
                            background-color: rgba(17, 35, 52, 210);
                            border: 1px dashed #5E7D97;
                            border-radius: 5px;
                        }
                        """
                    )
                else:
                    drop_hint.setStyleSheet(
                        """
                        QFrame {
                            background-color: #EEF2F7;
                            border: 1px dashed #CBD5E1;
                            border-radius: 5px;
                        }
                        """
                    )
                drop_hint_layout = QVBoxLayout(drop_hint)
                drop_hint_layout.setContentsMargins(2, 2, 2, 2)
                drop_hint_layout.setSpacing(1)
                hint_label = QLabel("Drop Here")
                hint_label.setAlignment(Qt.AlignCenter)
                hint_label.setWordWrap(True)
                if self._dark_mode:
                    hint_label.setStyleSheet("font-size: 9px; color: #8EA9BE;")
                else:
                    hint_label.setStyleSheet("font-size: 9px; color: #64748B;")
                drop_hint_layout.addWidget(hint_label)
                stage_layout.addWidget(drop_hint)
            elif stage != Stage.COMPLETE:
                self._add_cards_bucketed(stage_layout, stage_kits, stage_id, stage_box)
            else:
                self._add_cards_flat(stage_layout, stage_kits, stage_id)
            stage_layout.addStretch(1)
            row_layout.addWidget(stage_box)

    def set_column_widths(self, *, truck_width: int, stage_width: int) -> None:
        self._truck_info.setFixedWidth(int(truck_width))
        for stage_box in self._stage_boxes:
            stage_box.setFixedWidth(int(stage_width))

    def _resolve_stage_window(self, kit: TruckKit, stage_id: int) -> tuple[float, float] | None:
        key = (self._truck_id, _normalize_kit_name(kit.kit_name), stage_id)
        return self._kit_stage_windows_by_truck.get(key)

    def _schedule_bucket_for_kit(self, kit: TruckKit, stage_id: int) -> str:
        if stage_from_id(kit.front_stage_id) == Stage.COMPLETE:
            return "unplanned"

        window = self._resolve_stage_window(kit, stage_id)
        if window is None:
            if kit.id is not None:
                hold_weeks = self._hold_weeks_by_id.get(kit.id)
                if hold_weeks is not None and hold_weeks > 0:
                    return "late"
            return "unplanned"

        start_week, end_week = window
        if self._calendar_year is not None:
            today = date.today()
            start_date = _resolve_week_point_date(start_week, self._calendar_year)
            end_date = _resolve_week_point_date(end_week, self._calendar_year)
            if today > end_date:
                return "late"
            if start_date <= today <= end_date:
                return "this_week"
            if start_date <= today + timedelta(days=7):
                return "next"
            return "future"

        if self._current_week is None:
            return "unplanned"
        if self._current_week > end_week:
            return "late"
        if start_week <= self._current_week <= end_week:
            return "this_week"
        if start_week <= self._current_week + 1.0:
            return "next"
        return "future"

    def _create_kit_card(
        self,
        kit: TruckKit,
        stage_id: int,
    ) -> KitCard:
        card = KitCard(
            kit=kit,
            state_color=self._status_color_for_kit(kit),
            dark_mode=self._dark_mode,
        )
        card.clicked.connect(self.kit_selected.emit)
        return card

    def _status_color_for_kit(self, kit: TruckKit) -> str | None:
        if self._current_week is None:
            return None

        front_stage = stage_from_id(kit.front_stage_id)
        if front_stage == Stage.COMPLETE:
            return None

        baseline_windows: dict[Stage, tuple[float, float]] = {}
        for stage in (Stage.LASER, Stage.BEND, Stage.WELD):
            bounds = self._resolve_stage_window(kit, int(stage))
            if bounds is None:
                continue
            baseline_windows[stage] = (float(bounds[0]), float(bounds[1]))
        if not baseline_windows:
            return None

        released = bool(
            kit.release_state == "released"
            or front_stage > Stage.RELEASE
        )
        blocked = bool(str(getattr(kit, "blocker", "") or "").strip())
        front_position, _back_position = normalize_position_span(
            getattr(kit, "front_position", None),
            getattr(kit, "back_position", None),
            front_stage_id=front_stage,
            back_stage_id=stage_from_id(kit.back_stage_id),
        )
        expected_position = expected_position_for_week(
            current_week=float(self._current_week),
            baseline_windows=baseline_windows,
        )
        display_front_position = LASER_START_POSITION if not released else front_position
        front_week = overlay_position_to_week(
            position=display_front_position,
            windows=baseline_windows,
            fallback_week=float(self._current_week),
        )
        expected_week = None
        if expected_position >= LASER_START_POSITION:
            expected_week = overlay_position_to_week(
                position=expected_position,
                windows=baseline_windows,
                fallback_week=float(self._current_week),
            )
        _status_key, status_color = classify_front_status(
            released=released,
            blocked=blocked,
            front_stage=front_stage,
            expected_position=expected_position,
            front_position=display_front_position,
            expected_week=expected_week,
            front_week=front_week,
            current_week=float(self._current_week),
        )
        return status_color

    def _add_cards_flat(self, stage_layout: QVBoxLayout, stage_kits: list[TruckKit], stage_id: int) -> None:
        for kit in stage_kits:
            stage_layout.addWidget(self._create_kit_card(kit=kit, stage_id=stage_id))

    def _add_cards_bucketed(
        self,
        stage_layout: QVBoxLayout,
        stage_kits: list[TruckKit],
        stage_id: int,
        stage_box: StageDropZone,
    ) -> None:
        buckets = {name: [] for name in SCHEDULE_LANE_ORDER}
        for kit in stage_kits:
            bucket = self._schedule_bucket_for_kit(kit, stage_id)
            buckets.setdefault(bucket, []).append(kit)

        for bucket_name in SCHEDULE_LANE_ORDER:
            bucket_kits = buckets.get(bucket_name, [])
            if not bucket_kits:
                continue

            lane = StageForwardFrame(stage_box=stage_box)
            if self._dark_mode:
                lane.setStyleSheet(
                    """
                    QFrame {
                        background-color: rgba(17, 35, 52, 210);
                        border: 1px solid #54718A;
                        border-radius: 5px;
                    }
                    """
                )
            else:
                lane.setStyleSheet(
                    """
                    QFrame {
                        background-color: #EEF2F7;
                        border: 1px solid #D1DAE5;
                        border-radius: 5px;
                    }
                    """
                )
            lane_layout = QVBoxLayout(lane)
            lane_layout.setContentsMargins(3, 3, 3, 3)
            lane_layout.setSpacing(3)

            for kit in bucket_kits:
                lane_layout.addWidget(
                    self._create_kit_card(
                        kit=kit,
                        stage_id=stage_id,
                    )
                )

            stage_layout.addWidget(lane)


class BoardWidget(QWidget):
    kit_selected = Signal(int)
    kit_stage_drop_requested = Signal(int, int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._dark_mode = False
        self._row_widgets: list[TruckRowWidget] = []
        self._row_signatures: list[tuple[object, ...]] = []
        self._stage_headers: list[QLabel] = []
        self._truck_header: QLabel | None = None
        self._header_widget: QWidget | None = None
        self._last_trucks: list[Truck] = []
        self._last_hold_weeks_map: dict[int, float] = {}
        self._last_current_week: float | None = None
        self._last_stage_windows_map: dict[tuple[int, str, int], tuple[float, float]] = {}

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(4)

        root_layout.addWidget(self._build_header())

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._content_widget = QWidget()
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(4)
        self._scroll_area.setWidget(self._content_widget)

        root_layout.addWidget(self._scroll_area)
        self._apply_visual_mode()

    def set_dark_mode(self, dark_mode: bool) -> None:
        updated = bool(dark_mode)
        if self._dark_mode == updated:
            return
        self._dark_mode = updated
        self._apply_visual_mode()
        if self._last_trucks:
            self.set_data(
                self._last_trucks,
                self._last_hold_weeks_map,
                self._last_current_week,
                self._last_stage_windows_map,
                force_rebuild=True,
            )

    def _apply_visual_mode(self) -> None:
        if self._dark_mode:
            header_color = "#9CEBFF"
            empty_color = "#88A5BA"
            scroll_bg = "rgba(4, 15, 27, 175)"
        else:
            header_color = "#334155"
            empty_color = "#64748B"
            scroll_bg = "transparent"

        if self._header_widget is not None:
            self._header_widget.setStyleSheet("background: transparent;")
        if self._truck_header is not None:
            self._truck_header.setStyleSheet(f"font-weight: 700; color: {header_color};")
        for label in self._stage_headers:
            label.setStyleSheet(f"font-weight: 700; color: {header_color};")
        self._scroll_area.setStyleSheet(
            f"""
            QScrollArea {{
                background: {scroll_bg};
                border: none;
            }}
            """
        )
        self._content_widget.setStyleSheet("background: transparent;")
        for label in self.findChildren(QLabel, "board_empty_state"):
            label.setStyleSheet(f"padding: 20px; color: {empty_color};")

    def set_data(
        self,
        trucks: list[Truck],
        kit_release_hold_weeks_by_id: dict[int, float] | None = None,
        current_week: float | None = None,
        kit_stage_windows_by_truck: dict[tuple[int, str, int], tuple[float, float]] | None = None,
        *,
        force_rebuild: bool = False,
    ) -> None:
        self._last_trucks = list(trucks)
        hold_weeks_map = kit_release_hold_weeks_by_id or {}
        stage_windows_map = kit_stage_windows_by_truck or {}
        self._last_hold_weeks_map = dict(hold_weeks_map)
        self._last_current_week = current_week
        self._last_stage_windows_map = dict(stage_windows_map)

        desired_signatures = [
            _truck_render_signature(
                truck=truck,
                hold_weeks_by_id=hold_weeks_map,
                current_week=current_week,
                kit_stage_windows_by_truck=stage_windows_map,
            )
            for truck in trucks
        ]
        desired_ids = [int(truck.id or -1) for truck in trucks]
        existing_ids = [int(getattr(row_widget, "_truck_id", -1)) for row_widget in self._row_widgets]

        if (
            not force_rebuild
            and trucks
            and self._row_widgets
            and desired_ids == existing_ids
            and len(self._row_signatures) == len(desired_signatures)
        ):
            changed = False
            for index, truck in enumerate(trucks):
                if self._row_signatures[index] == desired_signatures[index]:
                    continue
                row_widget = self._create_row_widget(
                    truck=truck,
                    accent_color=ACCENT_COLORS[index % len(ACCENT_COLORS)],
                    hold_weeks_map=hold_weeks_map,
                    current_week=current_week,
                    stage_windows_map=stage_windows_map,
                )
                old_widget = self._row_widgets[index]
                self._content_layout.insertWidget(index, row_widget)
                self._content_layout.removeWidget(old_widget)
                old_widget.deleteLater()
                self._row_widgets[index] = row_widget
                self._row_signatures[index] = desired_signatures[index]
                changed = True

            if changed:
                QTimer.singleShot(0, self._apply_column_widths)
            return

        _clear_layout(self._content_layout)
        self._row_widgets = []
        self._row_signatures = []

        if not trucks:
            empty_label = QLabel("No trucks in flow. Use the CSV registry to add trucks.")
            empty_label.setObjectName("board_empty_state")
            empty_label.setAlignment(Qt.AlignCenter)
            if self._dark_mode:
                empty_label.setStyleSheet("padding: 20px; color: #88A5BA;")
            else:
                empty_label.setStyleSheet("padding: 20px; color: #64748B;")
            self._content_layout.addWidget(empty_label)
            self._content_layout.addStretch(1)
            return

        for index, truck in enumerate(trucks):
            row_widget = self._create_row_widget(
                truck=truck,
                accent_color=ACCENT_COLORS[index % len(ACCENT_COLORS)],
                hold_weeks_map=hold_weeks_map,
                current_week=current_week,
                stage_windows_map=stage_windows_map,
            )
            self._row_widgets.append(row_widget)
            self._row_signatures.append(desired_signatures[index])
            self._content_layout.addWidget(row_widget)

        self._content_layout.addStretch(1)
        QTimer.singleShot(0, self._apply_column_widths)

    def _create_row_widget(
        self,
        *,
        truck: Truck,
        accent_color: str,
        hold_weeks_map: dict[int, float],
        current_week: float | None,
        stage_windows_map: dict[tuple[int, str, int], tuple[float, float]],
    ) -> TruckRowWidget:
        row_widget = TruckRowWidget(
            truck=truck,
            accent_color=accent_color,
            kit_release_hold_weeks_by_id=hold_weeks_map,
            current_week=current_week,
            kit_stage_windows_by_truck=stage_windows_map,
            dark_mode=self._dark_mode,
        )
        row_widget.kit_selected.connect(self.kit_selected.emit)
        row_widget.kit_stage_dropped.connect(self.kit_stage_drop_requested.emit)
        return row_widget

    def _build_header(self) -> QWidget:
        header_widget = QWidget()
        self._header_widget = header_widget
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(BOARD_COL_MARGIN, 0, BOARD_COL_MARGIN, 0)
        header_layout.setSpacing(BOARD_COL_SPACING)

        truck_header = QLabel("TRUCK")
        truck_header.setWordWrap(True)
        truck_header.setFixedWidth(TRUCK_COL_WIDTH)
        truck_header.setStyleSheet("font-weight: 700; color: #334155;")
        self._truck_header = truck_header
        header_layout.addWidget(truck_header)

        for stage in STAGE_SEQUENCE:
            label = QLabel(stage_label(stage).upper())
            label.setWordWrap(True)
            label.setFixedWidth(STAGE_COL_WIDTH)
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("font-weight: 700; color: #334155;")
            self._stage_headers.append(label)
            header_layout.addWidget(label)

        return header_widget

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        self._apply_column_widths()

    def _apply_column_widths(self) -> None:
        truck_header = self._truck_header
        if truck_header is None:
            return

        stage_count = len(STAGE_SEQUENCE)
        viewport_width = int(self._scroll_area.viewport().width())
        if viewport_width <= 0:
            viewport_width = int(self.width())
        if viewport_width <= 0:
            return

        available_width = max(
            0,
            viewport_width - (BOARD_COL_MARGIN * 2) - (BOARD_COL_SPACING * stage_count),
        )
        preferred_total = TRUCK_COL_WIDTH + (STAGE_COL_WIDTH * stage_count)
        if available_width <= 0 or preferred_total <= 0:
            return

        scale = min(1.0, float(available_width) / float(preferred_total))
        truck_width = max(TRUCK_COL_MIN_WIDTH, min(TRUCK_COL_WIDTH, int(TRUCK_COL_WIDTH * scale)))
        stage_width = max(STAGE_COL_MIN_WIDTH, min(STAGE_COL_WIDTH, int(STAGE_COL_WIDTH * scale)))

        while (truck_width + (stage_width * stage_count)) > available_width:
            if stage_width > STAGE_COL_MIN_WIDTH:
                stage_width -= 1
                continue
            if truck_width > TRUCK_COL_MIN_WIDTH:
                truck_width -= 1
                continue
            break

        used_width = truck_width + (stage_width * stage_count)
        leftover = max(0, available_width - used_width)
        if leftover > 0 and stage_count > 0:
            stage_growth = min(STAGE_COL_WIDTH - stage_width, leftover // stage_count)
            if stage_growth > 0:
                stage_width += stage_growth
                leftover -= stage_growth * stage_count
        if leftover > 0:
            truck_width = min(TRUCK_COL_WIDTH, truck_width + leftover)

        truck_header.setFixedWidth(int(truck_width))
        for label in self._stage_headers:
            label.setFixedWidth(int(stage_width))
        for row_widget in self._row_widgets:
            row_widget.set_column_widths(truck_width=int(truck_width), stage_width=int(stage_width))
