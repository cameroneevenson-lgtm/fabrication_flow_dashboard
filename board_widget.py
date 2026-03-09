from __future__ import annotations

from datetime import date

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from models import STAGE_ORDER, Truck, TruckKit

TRUCK_COL_WIDTH = 220
STAGE_COL_WIDTH = 190
ACCENT_COLORS = ["#1F4E79", "#2F6B2F", "#8A5B1F", "#7A2F6B", "#006D77", "#5A4FCF"]


def _format_label(value: str) -> str:
    return value.replace("_", " ").title()


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


class KitCard(QFrame):
    clicked = Signal(int)

    def __init__(
        self,
        kit: TruckKit,
        accent_color: str,
        release_hold_days: int | None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._kit = kit
        self.setCursor(Qt.PointingHandCursor)

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

        title = kit.kit_name + (" (MAIN)" if kit.is_main_kit else "")
        title_label = QLabel(title)
        title_label.setStyleSheet("font-weight: 700; color: #1F2933;")

        meta_label = QLabel(
            f"{_format_label(kit.release_state)} | {_format_label(kit.magnitude)}"
        )
        meta_label.setStyleSheet("font-size: 11px; color: #4F5D6B;")

        layout.addWidget(title_label)
        layout.addWidget(meta_label)

        if release_hold_days is not None:
            hold_label = QLabel(f"ENG HOLD: {release_hold_days}d past planned start")
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
            self.clicked.emit(self._kit.id)
        super().mousePressEvent(event)


class TruckRowWidget(QFrame):
    kit_selected = Signal(int)

    def __init__(
        self,
        truck: Truck,
        accent_color: str,
        planned_start: date | None,
        kit_release_hold_days_by_id: dict[int, int],
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
        truck_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        truck_label.setStyleSheet("font-weight: 700; color: #0F172A;")
        truck_info_layout.addWidget(truck_label)

        if planned_start is not None:
            schedule_label = QLabel(f"Planned Start: {planned_start.isoformat()}")
            schedule_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            schedule_label.setStyleSheet("font-size: 11px; color: #475569;")
            truck_info_layout.addWidget(schedule_label)

        truck_info_layout.addStretch(1)
        row_layout.addWidget(truck_info)

        active_kits = [kit for kit in sorted(truck.kits, key=lambda x: x.kit_order) if kit.is_active]
        for stage in STAGE_ORDER:
            stage_box = QFrame()
            stage_box.setFixedWidth(STAGE_COL_WIDTH)
            stage_box.setStyleSheet(
                """
                QFrame {
                    background-color: #F3F5F7;
                    border: 1px dashed #CBD5E1;
                    border-radius: 6px;
                }
                """
            )
            stage_layout = QVBoxLayout(stage_box)
            stage_layout.setContentsMargins(6, 6, 6, 6)
            stage_layout.setSpacing(6)

            stage_kits = [kit for kit in active_kits if kit.current_stage == stage]
            if not stage_kits:
                placeholder = QLabel(" ")
                placeholder.setStyleSheet("font-size: 10px; color: #94A3B8;")
                stage_layout.addWidget(placeholder)
            else:
                for kit in stage_kits:
                    hold_days = None
                    if kit.id is not None:
                        hold_days = kit_release_hold_days_by_id.get(kit.id)
                    card = KitCard(
                        kit=kit,
                        accent_color=accent_color,
                        release_hold_days=hold_days,
                    )
                    card.clicked.connect(self.kit_selected.emit)
                    stage_layout.addWidget(card)
            stage_layout.addStretch(1)
            row_layout.addWidget(stage_box)


class BoardWidget(QWidget):
    kit_selected = Signal(int)

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
        truck_planned_start_by_id: dict[int, date] | None = None,
        kit_release_hold_days_by_id: dict[int, int] | None = None,
    ) -> None:
        _clear_layout(self._content_layout)
        planned_start_map = truck_planned_start_by_id or {}
        hold_days_map = kit_release_hold_days_by_id or {}

        if not trucks:
            empty_label = QLabel("No trucks in flow. Use 'Add Truck' to create one.")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("padding: 20px; color: #64748B;")
            self._content_layout.addWidget(empty_label)
            self._content_layout.addStretch(1)
            return

        for index, truck in enumerate(trucks):
            accent_color = ACCENT_COLORS[index % len(ACCENT_COLORS)]
            planned_start = None
            if truck.id is not None:
                planned_start = planned_start_map.get(truck.id)

            row_widget = TruckRowWidget(
                truck=truck,
                accent_color=accent_color,
                planned_start=planned_start,
                kit_release_hold_days_by_id=hold_days_map,
            )
            row_widget.kit_selected.connect(self.kit_selected.emit)
            self._content_layout.addWidget(row_widget)

        self._content_layout.addStretch(1)

    def _build_header(self) -> QWidget:
        header_widget = QWidget()
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(8, 0, 8, 0)
        header_layout.setSpacing(8)

        truck_header = QLabel("TRUCK / SCHEDULE")
        truck_header.setFixedWidth(TRUCK_COL_WIDTH)
        truck_header.setStyleSheet("font-weight: 700; color: #334155;")
        header_layout.addWidget(truck_header)

        for stage in STAGE_ORDER:
            label = QLabel(stage.upper())
            label.setFixedWidth(STAGE_COL_WIDTH)
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("font-weight: 700; color: #334155;")
            header_layout.addWidget(label)

        return header_widget
