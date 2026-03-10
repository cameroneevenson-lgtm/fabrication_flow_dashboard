from __future__ import annotations

from PySide6.QtCore import QMimeData, QPoint, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from models import STAGE_ORDER, Truck, TruckKit

TRUCK_COL_WIDTH = 220
STAGE_COL_WIDTH = 190
ACCENT_COLORS = ["#1F4E79", "#2F6B2F", "#8A5B1F", "#7A2F6B", "#006D77", "#5A4FCF"]
DRAG_MIME_PREFIX = "kitmove"


def _format_label(value: str) -> str:
    return value.replace("_", " ").title()


def _fmt_week(value: float) -> str:
    return f"W{value:.1f}"


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
        accent_color: str,
        release_hold_weeks: float | None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._kit = kit
        self._press_pos: QPoint | None = None
        self._drag_started = False
        self.setCursor(Qt.OpenHandCursor)

        border_color = accent_color if kit.is_main_kit else "#C6CDD4"
        border_width = 2 if kit.is_main_kit else 1
        self.setStyleSheet(
            f"""
            QFrame {{
                background-color: #FFFFFF;
                border: {border_width}px solid {border_color};
                border-radius: 6px;
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)

        title = kit.kit_name + (" (BODY)" if kit.is_main_kit else "")
        title_label = QLabel(title)
        title_label.setWordWrap(True)
        title_label.setStyleSheet("font-weight: 700; color: #1F2933;")

        meta_label = QLabel(
            f"{_format_label(kit.release_state)} | {_format_label(kit.magnitude)}"
        )
        meta_label.setWordWrap(True)
        meta_label.setStyleSheet("font-size: 11px; color: #4F5D6B;")

        layout.addWidget(title_label)
        layout.addWidget(meta_label)

        if release_hold_weeks is not None:
            hold_label = QLabel(f"ENG HOLD: {release_hold_weeks:.1f} week(s) past planned start")
            hold_label.setStyleSheet("font-size: 11px; font-weight: 700; color: #B91C1C;")
            hold_label.setWordWrap(True)
            layout.addWidget(hold_label)

        if kit.blocker.strip():
            blocker_label = QLabel(f"Blocker: {kit.blocker.strip()}")
            blocker_label.setWordWrap(True)
            blocker_label.setStyleSheet("font-size: 11px; color: #A53E2C;")
            layout.addWidget(blocker_label)

    def mousePressEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton and self._kit.id is not None:
            self._press_pos = event.position().toPoint()
            self._drag_started = False
            self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

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

        self._drag_started = True
        drag = QDrag(self)
        mime = QMimeData()
        mime.setText(_encode_drag_payload(self._kit))
        drag.setMimeData(mime)
        drag.exec(Qt.MoveAction)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # type: ignore[override]
        if event.button() == Qt.LeftButton:
            if not self._drag_started and self._kit.id is not None:
                self.clicked.emit(self._kit.id)
            self._press_pos = None
            self._drag_started = False
            self.setCursor(Qt.OpenHandCursor)
        super().mouseReleaseEvent(event)


class StageDropZone(QFrame):
    kit_dropped = Signal(int, str)

    def __init__(self, stage: str, truck_id: int, parent: QWidget | None = None):
        super().__init__(parent)
        self._stage = stage
        self._truck_id = truck_id
        self.setFixedWidth(STAGE_COL_WIDTH)
        self.setAcceptDrops(True)
        self._set_hover(False)

    def _set_hover(self, is_hover: bool) -> None:
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
        if payload and payload[1] == self._truck_id:
            event.acceptProposedAction()
            self._set_hover(True)
            return
        event.ignore()

    def dragMoveEvent(self, event):  # type: ignore[override]
        payload = self._extract_payload(event.mimeData().text())
        if payload and payload[1] == self._truck_id:
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
        kit_id, truck_id = payload
        if truck_id != self._truck_id:
            event.ignore()
            return

        self.kit_dropped.emit(kit_id, self._stage)
        event.acceptProposedAction()


class TruckRowWidget(QFrame):
    kit_selected = Signal(int)
    kit_stage_dropped = Signal(int, str)

    def __init__(
        self,
        truck: Truck,
        accent_color: str,
        planned_start_week: float | None,
        kit_release_hold_weeks_by_id: dict[int, float],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setStyleSheet(
            f"""
            QFrame {{
                background-color: #F8FAFC;
                border: 1px solid #D5DEE7;
                border-left: 6px solid {accent_color};
                border-radius: 8px;
            }}
            """
        )

        row_layout = QHBoxLayout(self)
        row_layout.setContentsMargins(8, 8, 8, 8)
        row_layout.setSpacing(8)

        truck_info = QWidget()
        truck_info.setFixedWidth(TRUCK_COL_WIDTH)
        truck_info_layout = QVBoxLayout(truck_info)
        truck_info_layout.setContentsMargins(0, 0, 0, 0)
        truck_info_layout.setSpacing(2)

        truck_label = QLabel(truck.truck_number)
        truck_label.setWordWrap(True)
        truck_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        truck_label.setStyleSheet("font-weight: 700; color: #0F172A;")
        truck_info_layout.addWidget(truck_label)

        if planned_start_week is not None:
            schedule_label = QLabel(f"Planned Start: {_fmt_week(planned_start_week)}")
            schedule_label.setWordWrap(True)
            schedule_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            schedule_label.setStyleSheet("font-size: 11px; color: #475569;")
            truck_info_layout.addWidget(schedule_label)

        truck_info_layout.addStretch(1)
        row_layout.addWidget(truck_info)

        active_kits = [kit for kit in sorted(truck.kits, key=lambda x: x.kit_order) if kit.is_active]
        truck_id = int(truck.id or -1)

        for stage in STAGE_ORDER:
            stage_box = StageDropZone(stage=stage, truck_id=truck_id)
            stage_box.kit_dropped.connect(self.kit_stage_dropped.emit)

            stage_layout = QVBoxLayout(stage_box)
            stage_layout.setContentsMargins(6, 6, 6, 6)
            stage_layout.setSpacing(6)

            stage_kits = [kit for kit in active_kits if kit.current_stage == stage]
            if not stage_kits:
                placeholder = QLabel(" ")
                placeholder.setWordWrap(True)
                placeholder.setStyleSheet("font-size: 10px; color: #94A3B8;")
                stage_layout.addWidget(placeholder)
            else:
                for kit in stage_kits:
                    hold_weeks = None
                    if kit.id is not None:
                        hold_weeks = kit_release_hold_weeks_by_id.get(kit.id)
                    card = KitCard(
                        kit=kit,
                        accent_color=accent_color,
                        release_hold_weeks=hold_weeks,
                    )
                    card.clicked.connect(self.kit_selected.emit)
                    stage_layout.addWidget(card)
            stage_layout.addStretch(1)
            row_layout.addWidget(stage_box)


class BoardWidget(QWidget):
    kit_selected = Signal(int)
    kit_stage_drop_requested = Signal(int, str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(8)

        root_layout.addWidget(self._build_header())

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._content_widget = QWidget()
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(8)
        self._scroll_area.setWidget(self._content_widget)

        root_layout.addWidget(self._scroll_area)

    def set_data(
        self,
        trucks: list[Truck],
        truck_planned_start_week_by_id: dict[int, float] | None = None,
        kit_release_hold_weeks_by_id: dict[int, float] | None = None,
    ) -> None:
        _clear_layout(self._content_layout)
        planned_start_map = truck_planned_start_week_by_id or {}
        hold_weeks_map = kit_release_hold_weeks_by_id or {}

        if not trucks:
            empty_label = QLabel("No trucks in flow. Use 'Add Truck' to create one.")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("padding: 20px; color: #64748B;")
            self._content_layout.addWidget(empty_label)
            self._content_layout.addStretch(1)
            return

        for index, truck in enumerate(trucks):
            accent_color = ACCENT_COLORS[index % len(ACCENT_COLORS)]
            planned_start_week = None
            if truck.id is not None:
                planned_start_week = planned_start_map.get(truck.id)

            row_widget = TruckRowWidget(
                truck=truck,
                accent_color=accent_color,
                planned_start_week=planned_start_week,
                kit_release_hold_weeks_by_id=hold_weeks_map,
            )
            row_widget.kit_selected.connect(self.kit_selected.emit)
            row_widget.kit_stage_dropped.connect(self.kit_stage_drop_requested.emit)
            self._content_layout.addWidget(row_widget)

        self._content_layout.addStretch(1)

    def _build_header(self) -> QWidget:
        header_widget = QWidget()
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(8, 0, 8, 0)
        header_layout.setSpacing(8)

        truck_header = QLabel("TRUCK / SCHEDULE")
        truck_header.setWordWrap(True)
        truck_header.setFixedWidth(TRUCK_COL_WIDTH)
        truck_header.setStyleSheet("font-weight: 700; color: #334155;")
        header_layout.addWidget(truck_header)

        for stage in STAGE_ORDER:
            label = QLabel(stage.upper())
            label.setWordWrap(True)
            label.setFixedWidth(STAGE_COL_WIDTH)
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("font-weight: 700; color: #334155;")
            header_layout.addWidget(label)

        return header_widget
