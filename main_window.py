from __future__ import annotations

import sqlite3

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
        self._status_label.setStyleSheet("color: #475569;")
        controls.addWidget(self._status_label)

        root_layout.addLayout(controls)

        self._health_strip = self._build_health_strip()
        root_layout.addWidget(self._health_strip)

        middle_layout = QHBoxLayout()
        middle_layout.setSpacing(10)

        self._board_widget = BoardWidget()
        self._board_widget.kit_selected.connect(self._on_kit_selected)
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
        self._trucks = sort_trucks_natural(self.database.load_trucks_with_kits(active_only=True))
        self._schedule_insights = build_schedule_insights(self._trucks)

        self._kit_index = {}
        for truck in self._trucks:
            for kit in truck.kits:
                if kit.id is not None:
                    self._kit_index[kit.id] = (truck, kit)

        self._board_widget.set_data(
            self._trucks,
            truck_planned_start_by_id=self._schedule_insights.truck_planned_start_by_id,
            kit_release_hold_days_by_id=self._schedule_insights.kit_release_hold_days_by_id,
        )

        metrics = compute_dashboard_metrics(self._trucks, schedule_insights=self._schedule_insights)
        self._update_health_strip(metrics)
        self._update_schedule_panel()
        self._update_attention_panel(metrics)

        hold_count = len(self._schedule_insights.release_hold_items)
        self._status_label.setText(
            f"Trucks: {len(self._trucks)} | Active Kits: {len(self._kit_index)} | Engineering Holds: {hold_count}"
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

        try:
            self.database.create_truck(clean_truck_number)
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

    def _build_health_strip(self) -> QWidget:
        strip = QWidget()
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._tile_widgets = {
            "next_main": self._create_tile("Next Main Kit Risk"),
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
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #0F172A;")
        layout.addWidget(title)

        self._schedule_start_label = QLabel("")
        self._schedule_start_label.setStyleSheet("font-size: 12px; color: #334155;")
        layout.addWidget(self._schedule_start_label)

        self._truck_lag_label = QLabel("")
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
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #0F172A;")
        layout.addWidget(title)

        self._attention_list = QListWidget()
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
        title_label.setStyleSheet("font-size: 12px; color: #334155;")

        value_label = QLabel("-")
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
            self._schedule_start_label.setText("Project Start (Master): -")
            self._truck_lag_label.setText("Truck Start Lag: -")
            self._hold_summary_label.setText("")
            self._standards_label.setText("")
            return

        self._schedule_start_label.setText(
            f"Project Start (Master): {self._schedule_insights.master_start_date.isoformat()}"
        )
        self._truck_lag_label.setText(
            f"Standard Truck Start Lag: {self._schedule_insights.truck_start_lag_days} day(s)"
        )

        hold_items = self._schedule_insights.release_hold_items
        if hold_items:
            oldest = hold_items[0]
            self._hold_summary_label.setText(
                "Engineering release hold: "
                f"{len(hold_items)} kit(s) blocked; oldest {oldest.hold_days} day(s) "
                f"late ({oldest.truck_number} {oldest.kit_name})."
            )
            self._hold_summary_label.setStyleSheet("font-size: 12px; font-weight: 700; color: #B91C1C;")
        else:
            self._hold_summary_label.setText("Engineering release hold: none currently past planned start.")
            self._hold_summary_label.setStyleSheet("font-size: 12px; font-weight: 700; color: #2E7D32;")

        lines = ["Standard Lag / Duration (from truck planned start):"]
        for standard in self._schedule_insights.standards:
            lines.append(
                f"{standard.kit_name}: +{standard.lag_days}d lag, {standard.duration_days}d duration"
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
            row = QListWidgetItem(text)
            if item.priority >= 90:
                row.setForeground(Qt.red)
            elif item.priority >= 70:
                row.setForeground(Qt.darkYellow)
            self._attention_list.addItem(row)
