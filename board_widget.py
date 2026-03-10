from __future__ import annotations

from datetime import date, datetime, timedelta

from PySide6.QtCore import QMimeData, QPoint, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from models import STAGE_ORDER, Truck, TruckKit

TRUCK_COL_WIDTH = 200
STAGE_COL_WIDTH = 190
ACCENT_COLORS = ["#1F4E79", "#2F6B2F", "#8A5B1F", "#7A2F6B", "#006D77", "#5A4FCF"]
DRAG_MIME_PREFIX = "kitmove"
WEEK_LENS_LANE_ORDER = ["late", "this_week", "next", "future", "unplanned"]
WEEK_LENS_LANE_LABELS = {
    "late": "Late",
    "this_week": "This Week",
    "next": "Next",
    "future": "Future",
    "unplanned": "Unplanned",
}


def _format_label(value: str) -> str:
    return value.replace("_", " ").title()


def _fmt_week(value: float) -> str:
    return f"W{value:.1f}"


def _normalize_kit_name(value: str) -> str:
    return str(value or "").strip().lower()


def _iso_week_start(value: float, year: int | None = None) -> str:
    target_year = int(year or datetime.now().year)
    monday = _resolve_week_monday(value=value, base_year=target_year)
    return monday.strftime("%b %d, %Y")


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


def _resolve_week_monday(value: float, base_year: int) -> date:
    year, week, _fraction = _resolve_week_parts(value=value, base_year=base_year)
    return date.fromisocalendar(year, week, 1)


def _resolve_week_point_date(value: float, base_year: int) -> date:
    year, week, fraction = _resolve_week_parts(value=value, base_year=base_year)
    monday = date.fromisocalendar(year, week, 1)
    return monday + timedelta(days=(fraction * 7.0))


def _pdf_link_count(raw_links: str) -> int:
    return sum(1 for part in str(raw_links).replace(";", "\n").splitlines() if part.strip())


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
        stage_window: tuple[float, float] | None = None,
        calendar_year: int | None = None,
        week_bucket: str | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._kit = kit
        self._press_pos: QPoint | None = None
        self._drag_started = False
        self.setCursor(Qt.OpenHandCursor)
        self.setAcceptDrops(True)

        is_complete = str(kit.current_stage).strip().lower() == "complete"
        if is_complete:
            border_color = "#D1D5DB"
            border_width = 1
            background_color = "#F8FAFC"
            title_color = "#64748B"
            meta_color = "#94A3B8"
        else:
            border_color = accent_color if kit.is_main_kit else "#C6CDD4"
            border_width = 2 if kit.is_main_kit else 1
            background_color = "#FFFFFF"
            title_color = "#1F2933"
            meta_color = "#4F5D6B"

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

        title = kit.kit_name + (" (BODY)" if kit.is_main_kit else "")
        title_label = QLabel(title)
        title_label.setWordWrap(True)
        title_label.setStyleSheet(f"font-weight: 700; color: {title_color};")

        meta_label = QLabel(f"{_format_label(kit.release_state)}")
        meta_label.setWordWrap(True)
        meta_label.setStyleSheet(f"font-size: 10px; color: {meta_color};")

        layout.addWidget(title_label)
        layout.addWidget(meta_label)

        if release_hold_weeks is not None and not is_complete:
            hold_label = QLabel(f"ENG HOLD: {release_hold_weeks:.1f} week(s) past planned start")
            hold_label.setStyleSheet("font-size: 10px; font-weight: 700; color: #B91C1C;")
            hold_label.setWordWrap(True)
            layout.addWidget(hold_label)

        if stage_window is not None and not is_complete:
            start_date = _iso_week_start(stage_window[0], calendar_year)
            end_date = _iso_week_start(stage_window[1], calendar_year)
            week_text = f"Plan: week of {start_date} to week of {end_date}"
            if week_bucket and week_bucket in WEEK_LENS_LANE_LABELS:
                week_text = f"{week_text} ({WEEK_LENS_LANE_LABELS[week_bucket]})"
            week_label = QLabel(week_text)
            week_label.setWordWrap(True)
            if week_bucket == "late":
                week_label.setStyleSheet("font-size: 10px; font-weight: 700; color: #B91C1C;")
            else:
                week_label.setStyleSheet("font-size: 10px; color: #334155;")
            layout.addWidget(week_label)

        pdf_link_count = _pdf_link_count(kit.pdf_links)
        if pdf_link_count > 0:
            pdf_label = QLabel(f"PDF Link(s): {pdf_link_count}")
            pdf_label.setWordWrap(True)
            pdf_label.setStyleSheet("font-size: 10px; color: #1D4ED8;")
            layout.addWidget(pdf_label)

        if kit.blocker.strip():
            blocker_label = QLabel(f"Blocker: {kit.blocker.strip()}")
            blocker_label.setWordWrap(True)
            blocker_label.setStyleSheet("font-size: 10px; color: #A53E2C;")
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
        zone.kit_dropped.emit(payload[0], zone._stage)
        event.acceptProposedAction()


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

        self.kit_dropped.emit(kit_id, self._stage)
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
        self._stage_box.kit_dropped.emit(payload[0], self._stage_box._stage)
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
        week_lens_enabled: bool = False,
        current_week: float | None = None,
        kit_stage_windows_by_truck: dict[tuple[int, str, str], tuple[float, float]] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._accent_color = accent_color
        self._hold_weeks_by_id = kit_release_hold_weeks_by_id
        self._week_lens_enabled = week_lens_enabled
        self._current_week = current_week
        self._kit_stage_windows_by_truck = kit_stage_windows_by_truck or {}
        self._truck_id = int(truck.id or -1)
        self._calendar_year = _calendar_year_from_date(truck.planned_start_date)
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
        row_layout.setContentsMargins(6, 6, 6, 6)
        row_layout.setSpacing(6)

        truck_info = QWidget()
        truck_info.setFixedWidth(TRUCK_COL_WIDTH)
        truck_info_layout = QVBoxLayout(truck_info)
        truck_info_layout.setContentsMargins(0, 0, 0, 0)
        truck_info_layout.setSpacing(1)

        truck_label = QLabel(truck.truck_number)
        truck_label.setWordWrap(True)
        truck_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        truck_label.setStyleSheet("font-weight: 700; color: #0F172A;")
        truck_info_layout.addWidget(truck_label)

        if planned_start_week is not None:
            schedule_label = QLabel(
                f"Planned Start: week of {_iso_week_start(planned_start_week, self._calendar_year)}"
            )
            schedule_label.setWordWrap(True)
            schedule_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            schedule_label.setStyleSheet("font-size: 10px; color: #475569;")
            truck_info_layout.addWidget(schedule_label)

        if str(truck.client).strip():
            client_label = QLabel(f"Client: {truck.client.strip()}")
            client_label.setWordWrap(True)
            client_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            client_label.setStyleSheet("font-size: 10px; color: #475569;")
            truck_info_layout.addWidget(client_label)

        if str(truck.planned_start_date).strip():
            date_label = QLabel(f"Day Zero: {truck.planned_start_date.strip()}")
            date_label.setWordWrap(True)
            date_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            date_label.setStyleSheet("font-size: 10px; color: #475569;")
            truck_info_layout.addWidget(date_label)

        truck_info_layout.addStretch(1)
        row_layout.addWidget(truck_info)

        active_kits = [kit for kit in sorted(truck.kits, key=lambda x: x.kit_order) if kit.is_active]
        for stage in STAGE_ORDER:
            stage_box = StageDropZone(stage=stage, truck_id=self._truck_id)
            stage_box.kit_dropped.connect(self.kit_stage_dropped.emit)

            stage_layout = QVBoxLayout(stage_box)
            stage_layout.setContentsMargins(4, 4, 4, 4)
            stage_layout.setSpacing(4)

            stage_kits = [kit for kit in active_kits if kit.current_stage == stage]
            if not stage_kits:
                drop_hint = StageForwardFrame(stage_box=stage_box)
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
                hint_label.setStyleSheet("font-size: 9px; color: #64748B;")
                drop_hint_layout.addWidget(hint_label)
                stage_layout.addWidget(drop_hint)
            elif self._week_lens_enabled and stage != "complete":
                self._add_cards_week_lens(stage_layout, stage_kits, stage, stage_box)
            else:
                self._add_cards_flat(stage_layout, stage_kits, stage)
            stage_layout.addStretch(1)
            row_layout.addWidget(stage_box)

    def _resolve_stage_window(self, kit: TruckKit, stage: str) -> tuple[float, float] | None:
        key = (self._truck_id, _normalize_kit_name(kit.kit_name), stage)
        return self._kit_stage_windows_by_truck.get(key)

    def _week_bucket_for_kit(self, kit: TruckKit, stage: str) -> str:
        if str(kit.current_stage).strip().lower() == "complete":
            return "unplanned"

        window = self._resolve_stage_window(kit, stage)
        if window is None:
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
        stage: str,
        week_bucket: str | None = None,
    ) -> KitCard:
        hold_weeks = None
        if kit.id is not None:
            hold_weeks = self._hold_weeks_by_id.get(kit.id)
        card = KitCard(
            kit=kit,
            accent_color=self._accent_color,
            release_hold_weeks=hold_weeks,
            stage_window=self._resolve_stage_window(kit, stage),
            calendar_year=self._calendar_year,
            week_bucket=week_bucket,
        )
        card.clicked.connect(self.kit_selected.emit)
        return card

    def _add_cards_flat(self, stage_layout: QVBoxLayout, stage_kits: list[TruckKit], stage: str) -> None:
        for kit in stage_kits:
            stage_layout.addWidget(self._create_kit_card(kit=kit, stage=stage))

    def _add_cards_week_lens(
        self,
        stage_layout: QVBoxLayout,
        stage_kits: list[TruckKit],
        stage: str,
        stage_box: StageDropZone,
    ) -> None:
        buckets = {name: [] for name in WEEK_LENS_LANE_ORDER}
        for kit in stage_kits:
            bucket = self._week_bucket_for_kit(kit, stage)
            buckets.setdefault(bucket, []).append(kit)

        for bucket_name in WEEK_LENS_LANE_ORDER:
            bucket_kits = buckets.get(bucket_name, [])
            if not bucket_kits:
                continue

            lane = StageForwardFrame(stage_box=stage_box)
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

            title_label = QLabel(f"{WEEK_LENS_LANE_LABELS[bucket_name]} ({len(bucket_kits)})")
            title_label.setWordWrap(True)
            if bucket_name == "late":
                title_label.setStyleSheet("font-size: 9px; font-weight: 700; color: #B91C1C;")
            else:
                title_label.setStyleSheet("font-size: 9px; font-weight: 700; color: #334155;")
            lane_layout.addWidget(title_label)

            for kit in bucket_kits:
                lane_layout.addWidget(
                    self._create_kit_card(
                        kit=kit,
                        stage=stage,
                        week_bucket=bucket_name,
                    )
                )

            stage_layout.addWidget(lane)


class BoardWidget(QWidget):
    kit_selected = Signal(int)
    kit_stage_drop_requested = Signal(int, str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._week_lens_enabled = False

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(4)

        root_layout.addWidget(self._build_header())

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._content_widget = QWidget()
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(4)
        self._scroll_area.setWidget(self._content_widget)

        root_layout.addWidget(self._scroll_area)

    def set_week_lens_enabled(self, enabled: bool) -> None:
        self._week_lens_enabled = bool(enabled)

    def set_data(
        self,
        trucks: list[Truck],
        truck_planned_start_week_by_id: dict[int, float] | None = None,
        kit_release_hold_weeks_by_id: dict[int, float] | None = None,
        current_week: float | None = None,
        kit_stage_windows_by_truck: dict[tuple[int, str, str], tuple[float, float]] | None = None,
    ) -> None:
        _clear_layout(self._content_layout)
        planned_start_map = truck_planned_start_week_by_id or {}
        hold_weeks_map = kit_release_hold_weeks_by_id or {}
        stage_windows_map = kit_stage_windows_by_truck or {}

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
                week_lens_enabled=self._week_lens_enabled,
                current_week=current_week,
                kit_stage_windows_by_truck=stage_windows_map,
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

