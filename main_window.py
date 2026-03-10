from __future__ import annotations

import sqlite3
from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from board_widget import BoardWidget
from database import FabricationDatabase
from metrics import DashboardMetrics, compute_dashboard_metrics, sort_trucks_natural
from models import MAGNITUDE_VALUES, RELEASE_STATES, STAGE_ORDER, Truck, TruckKit
from schedule import ScheduleInsights, build_schedule_insights


def _fmt_week(value: float) -> str:
    return f"W{value:.1f}"


class WrappingListWidget(QListWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setWordWrap(True)
        self.setUniformItemSizes(False)

    def add_wrapped_item(self, text: str, color: str) -> None:
        item = QListWidgetItem()
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        label.setStyleSheet(f"padding: 6px 8px; color: {color};")
        self.addItem(item)
        self.setItemWidget(item, label)
        self._refresh_item_heights()

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        self._refresh_item_heights()

    def _refresh_item_heights(self) -> None:
        target_width = max(120, self.viewport().width() - 10)
        for index in range(self.count()):
            item = self.item(index)
            widget = self.itemWidget(item)
            if widget is None:
                continue
            widget.setFixedWidth(target_width)
            widget.adjustSize()
            item.setSizeHint(widget.sizeHint())


class KitEditDialog(QDialog):
    def __init__(self, truck_number: str, kit: TruckKit, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit Kit - {kit.kit_name}")
        self.setModal(True)
        self.resize(420, 260)

        self._release_combo = QComboBox()
        self._release_combo.addItems(RELEASE_STATES)
        self._release_combo.setCurrentText(kit.release_state)

        self._stage_combo = QComboBox()
        self._stage_combo.addItems(STAGE_ORDER)
        self._stage_combo.setCurrentText(kit.current_stage)

        self._magnitude_combo = QComboBox()
        self._magnitude_combo.addItems(MAGNITUDE_VALUES)
        self._magnitude_combo.setCurrentText(kit.magnitude)

        self._blocker_input = QLineEdit(kit.blocker)
        self._active_checkbox = QCheckBox("Kit is active")
        self._active_checkbox.setChecked(kit.is_active)

        form = QFormLayout()
        form.addRow("Truck", QLabel(truck_number))
        form.addRow("Kit", QLabel(kit.kit_name))
        form.addRow("Release State", self._release_combo)
        form.addRow("Current Stage", self._stage_combo)
        form.addRow("Magnitude", self._magnitude_combo)
        form.addRow("Blocker", self._blocker_input)
        form.addRow("", self._active_checkbox)

        remove_button = QPushButton("Remove Kit (Soft)")
        remove_button.clicked.connect(self._mark_removed)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(remove_button, alignment=Qt.AlignLeft)
        layout.addWidget(buttons)

    def _mark_removed(self) -> None:
        self._active_checkbox.setChecked(False)

    def get_values(self) -> dict[str, object]:
        return {
            "release_state": self._release_combo.currentText(),
            "current_stage": self._stage_combo.currentText(),
            "magnitude": self._magnitude_combo.currentText(),
            "blocker": self._blocker_input.text(),
            "is_active": self._active_checkbox.isChecked(),
        }


class MainWindow(QMainWindow):
    def __init__(self, database: FabricationDatabase):
        super().__init__()
        self.database = database

        self._trucks: list[Truck] = []
        self._kit_index: dict[int, tuple[Truck, TruckKit]] = {}
        self._schedule_insights: ScheduleInsights | None = None

        self.setWindowTitle("Fabrication Flow Dashboard")
        self.resize(1600, 900)

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)
        self.setCentralWidget(root)

        controls = QHBoxLayout()
        add_truck_button = QPushButton("Add Truck")
        add_truck_button.clicked.connect(self._on_add_truck)
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh_view)
        controls.addWidget(add_truck_button)
        controls.addWidget(refresh_button)
        controls.addStretch(1)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #475569;")
        controls.addWidget(self._status_label)

        root_layout.addLayout(controls)

        self._health_strip = self._build_health_strip()
        root_layout.addWidget(self._health_strip)

        middle_layout = QHBoxLayout()
        middle_layout.setSpacing(10)

        self._board_widget = BoardWidget()
        self._board_widget.kit_selected.connect(self._on_kit_selected)
        self._board_widget.kit_stage_drop_requested.connect(self._on_kit_stage_drop_requested)
        middle_layout.addWidget(self._board_widget, 4)

        right_column = QWidget()
        right_column_layout = QVBoxLayout(right_column)
        right_column_layout.setContentsMargins(0, 0, 0, 0)
        right_column_layout.setSpacing(10)

        schedule_panel = self._build_schedule_panel()
        attention_panel = self._build_attention_panel()

        right_column_layout.addWidget(schedule_panel)
        right_column_layout.addWidget(attention_panel, 1)

        middle_layout.addWidget(right_column, 1)

        root_layout.addLayout(middle_layout)

        self.refresh_view()

    def refresh_view(self) -> None:
        loaded_trucks = sort_trucks_natural(self.database.load_trucks_with_kits(active_only=True))
        self._trucks = [truck for truck in loaded_trucks if not self._is_truck_complete(truck)]
        self._schedule_insights = build_schedule_insights(self._trucks)

        self._kit_index = {}
        for truck in self._trucks:
            for kit in truck.kits:
                if kit.id is not None:
                    self._kit_index[kit.id] = (truck, kit)

        self._board_widget.set_data(
            self._trucks,
            truck_planned_start_week_by_id=self._schedule_insights.truck_planned_start_week_by_id,
            kit_release_hold_weeks_by_id=self._schedule_insights.kit_release_hold_weeks_by_id,
        )

        metrics = compute_dashboard_metrics(self._trucks, schedule_insights=self._schedule_insights)
        self._update_health_strip(metrics)
        self._update_schedule_panel()
        self._update_attention_panel(metrics)

        hold_count = len(self._schedule_insights.release_hold_items)
        self._status_label.setText(
            f"Week: {_fmt_week(self._schedule_insights.current_week)} | Trucks: {len(self._trucks)} "
            f"| Active Kits: {len(self._kit_index)} | Engineering Holds: {hold_count}"
        )

    def _on_add_truck(self) -> None:
        truck_number, accepted = QInputDialog.getText(
            self,
            "Add Truck",
            "Truck number:",
        )
        if not accepted:
            return

        clean_truck_number = truck_number.strip()
        if not clean_truck_number:
            QMessageBox.warning(self, "Invalid Input", "Truck number is required.")
            return

        day_zero_text, day_zero_accepted = QInputDialog.getText(
            self,
            "Add Truck",
            "Calendar Day Zero (YYYY-MM-DD):",
        )
        if not day_zero_accepted:
            return

        clean_day_zero = day_zero_text.strip()
        if not clean_day_zero:
            QMessageBox.warning(self, "Invalid Input", "Calendar Day Zero is required.")
            return

        try:
            parsed_day_zero = date.fromisoformat(clean_day_zero)
        except ValueError:
            QMessageBox.warning(self, "Invalid Input", "Calendar Day Zero must be YYYY-MM-DD.")
            return

        notes = f"Calendar Day Zero: {parsed_day_zero.isoformat()}"

        try:
            self.database.create_truck(clean_truck_number, notes=notes)
        except sqlite3.IntegrityError:
            QMessageBox.warning(
                self,
                "Duplicate Truck",
                f"Truck number '{clean_truck_number}' already exists.",
            )
            return
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Input", str(exc))
            return

        self.refresh_view()
    def _on_kit_selected(self, kit_id: int) -> None:
        result = self._kit_index.get(kit_id)
        if not result:
            return

        truck, kit = result
        dialog = KitEditDialog(truck_number=truck.truck_number, kit=kit, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return

        values = dialog.get_values()
        self.database.update_truck_kit(
            kit_id=kit_id,
            release_state=str(values["release_state"]),
            current_stage=str(values["current_stage"]),
            magnitude=str(values["magnitude"]),
            blocker=str(values["blocker"]),
            is_active=bool(values["is_active"]),
        )
        self.refresh_view()

    def _on_kit_stage_drop_requested(self, kit_id: int, target_stage: str) -> None:
        result = self._kit_index.get(kit_id)
        if not result:
            return
        if target_stage not in STAGE_ORDER:
            return

        truck, kit = result
        current_idx = STAGE_ORDER.index(kit.current_stage)
        target_idx = STAGE_ORDER.index(target_stage)
        if target_idx <= current_idx:
            if target_idx < current_idx:
                self.statusBar().showMessage(
                    "Drag-and-drop only moves kits forward. Use kit edit to move backward.",
                    4000,
                )
            return

        release_state = kit.release_state
        if release_state == "not_released" and target_stage != "release":
            # Moving into fabrication implies engineering has at least partially released.
            release_state = "partial"

        self.database.update_truck_kit(
            kit_id=kit_id,
            release_state=release_state,
            current_stage=target_stage,
            magnitude=kit.magnitude,
            blocker=kit.blocker,
            is_active=kit.is_active,
        )
        self.refresh_view()
        self.statusBar().showMessage(
            f"Moved {truck.truck_number} {kit.kit_name} to {target_stage.upper()}",
            3000,
        )

    @staticmethod
    def _is_truck_complete(truck: Truck) -> bool:
        active_kits = [kit for kit in truck.kits if kit.is_active]
        if not active_kits:
            return False
        return all(str(kit.current_stage).strip().lower() == "welded" for kit in active_kits)

    def _build_health_strip(self) -> QWidget:
        strip = QWidget()
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._tile_widgets = {
            "next_main": self._create_tile("Next Body Risk"),
            "bend_buffer": self._create_tile("Bend Buffer Health"),
            "weld_feed": self._create_tile("Weld Feed Status"),
            "release_gap": self._create_tile("Release Gap Warning"),
        }

        for tile in self._tile_widgets.values():
            layout.addWidget(tile["frame"])

        return strip

    def _build_schedule_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.StyledPanel)
        panel.setStyleSheet(
            """
            QFrame {
                background-color: #F8FAFC;
                border: 1px solid #D5DEE7;
                border-radius: 8px;
            }
            """
        )

        layout = QVBoxLayout(panel)
        title = QLabel("Master Schedule Reference")
        title.setWordWrap(True)
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #0F172A;")
        layout.addWidget(title)

        self._schedule_start_label = QLabel("")
        self._schedule_start_label.setWordWrap(True)
        self._schedule_start_label.setStyleSheet("font-size: 12px; color: #334155;")
        layout.addWidget(self._schedule_start_label)

        self._current_week_label = QLabel("")
        self._current_week_label.setWordWrap(True)
        self._current_week_label.setStyleSheet("font-size: 12px; color: #334155;")
        layout.addWidget(self._current_week_label)

        self._truck_lag_label = QLabel("")
        self._truck_lag_label.setWordWrap(True)
        self._truck_lag_label.setStyleSheet("font-size: 12px; color: #334155;")
        layout.addWidget(self._truck_lag_label)

        self._hold_summary_label = QLabel("")
        self._hold_summary_label.setWordWrap(True)
        self._hold_summary_label.setStyleSheet("font-size: 12px; font-weight: 700; color: #B91C1C;")
        layout.addWidget(self._hold_summary_label)

        self._standards_label = QLabel("")
        self._standards_label.setWordWrap(True)
        self._standards_label.setStyleSheet("font-size: 11px; color: #475569;")
        layout.addWidget(self._standards_label)

        return panel

    def _build_attention_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.StyledPanel)
        panel.setStyleSheet(
            """
            QFrame {
                background-color: #F8FAFC;
                border: 1px solid #D5DEE7;
                border-radius: 8px;
            }
            """
        )

        layout = QVBoxLayout(panel)
        title = QLabel("Attention")
        title.setWordWrap(True)
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #0F172A;")
        layout.addWidget(title)

        self._attention_list = WrappingListWidget()
        self._attention_list.setStyleSheet(
            """
            QListWidget {
                background: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
            }
            """
        )
        layout.addWidget(self._attention_list)

        return panel

    def _create_tile(self, title: str) -> dict[str, QWidget]:
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(
            """
            QFrame {
                background-color: #F8FAFC;
                border: 1px solid #D5DEE7;
                border-radius: 8px;
            }
            """
        )
        frame.setMinimumHeight(100)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        title_label = QLabel(title)
        title_label.setWordWrap(True)
        title_label.setStyleSheet("font-size: 12px; color: #334155;")

        value_label = QLabel("-")
        value_label.setWordWrap(True)
        value_label.setStyleSheet("font-size: 20px; font-weight: 700; color: #0F172A;")

        detail_label = QLabel("")
        detail_label.setWordWrap(True)
        detail_label.setStyleSheet("font-size: 11px; color: #475569;")

        layout.addWidget(title_label)
        layout.addWidget(value_label)
        layout.addWidget(detail_label)

        return {"frame": frame, "value": value_label, "detail": detail_label}

    def _update_schedule_panel(self) -> None:
        if not self._schedule_insights:
            self._schedule_start_label.setText("Project Start: Day Zero (W0.0)")
            self._current_week_label.setText("Current Schedule Week: -")
            self._truck_lag_label.setText("Truck Start Lag: -")
            self._hold_summary_label.setText("")
            self._standards_label.setText("")
            return

        self._schedule_start_label.setText(
            f"Project Start Anchor: Day Zero ({_fmt_week(self._schedule_insights.day_zero_week)})"
        )
        self._current_week_label.setText(
            f"Current Schedule Week: {_fmt_week(self._schedule_insights.current_week)}"
        )
        self._truck_lag_label.setText(
            f"Standard Truck Start Lag: {self._schedule_insights.truck_start_lag_weeks:.1f} week(s)"
        )

        hold_items = self._schedule_insights.release_hold_items
        if hold_items:
            oldest = hold_items[0]
            self._hold_summary_label.setText(
                "Engineering release hold: "
                f"{len(hold_items)} kit(s) blocked; oldest {oldest.hold_weeks:.1f} week(s) "
                f"late ({oldest.truck_number} {oldest.kit_name})."
            )
            self._hold_summary_label.setStyleSheet("font-size: 12px; font-weight: 700; color: #B91C1C;")
        else:
            self._hold_summary_label.setText("Engineering release hold: none currently past planned start.")
            self._hold_summary_label.setStyleSheet("font-size: 12px; font-weight: 700; color: #2E7D32;")

        lines = ["Standard Lag / Duration (from truck planned start):"]
        for standard in self._schedule_insights.standards:
            lines.append(
                f"{standard.kit_name}: +{standard.lag_weeks:.1f}w lag, {standard.duration_weeks:.1f}w duration"
            )
        self._standards_label.setText("\n".join(lines))

    def _update_health_strip(self, metrics: DashboardMetrics) -> None:
        if metrics.next_main_kit_risk.is_warning:
            next_main_status = "WARNING"
            next_main_tone = "warning"
        else:
            next_main_status = "OK"
            next_main_tone = "ok"

        self._set_tile(
            "next_main",
            value=next_main_status,
            detail=metrics.next_main_kit_risk.message,
            tone=next_main_tone,
        )

        self._set_tile(
            "bend_buffer",
            value=f"{metrics.bend_buffer.kit_count} kits ({metrics.bend_buffer.level.upper()})",
            detail="Released kits in laser/bend",
            tone=metrics.bend_buffer.level,
        )

        self._set_tile(
            "weld_feed",
            value=f"{metrics.weld_feed.score:.1f} ({metrics.weld_feed.level.upper()})",
            detail="Magnitude-weighted bend/weld workload",
            tone=metrics.weld_feed.level,
        )

        if metrics.release_gap.is_warning:
            release_value = f"{metrics.release_gap.gap_count} gap(s)"
            release_tone = "warning"
        else:
            release_value = "CLEAR"
            release_tone = "ok"

        self._set_tile(
            "release_gap",
            value=release_value,
            detail=metrics.release_gap.message,
            tone=release_tone,
        )

    def _set_tile(self, key: str, value: str, detail: str, tone: str) -> None:
        color_map = {
            "ok": "#2E7D32",
            "warning": "#C62828",
            "empty": "#C62828",
            "low": "#C62828",
            "watch": "#EF6C00",
            "healthy": "#2E7D32",
        }
        color = color_map.get(tone, "#0F172A")

        tile = self._tile_widgets[key]
        value_label = tile["value"]
        detail_label = tile["detail"]

        value_label.setText(value)
        value_label.setStyleSheet(f"font-size: 20px; font-weight: 700; color: {color};")
        detail_label.setText(detail)

    def _update_attention_panel(self, metrics: DashboardMetrics) -> None:
        self._attention_list.clear()
        for index, item in enumerate(metrics.attention_items, start=1):
            text = f"{index}. {item.title}: {item.detail}"
            if item.priority >= 90:
                color = "#B91C1C"
            elif item.priority >= 70:
                color = "#A16207"
            else:
                color = "#1F2937"
            self._attention_list.add_wrapped_item(text=text, color=color)

