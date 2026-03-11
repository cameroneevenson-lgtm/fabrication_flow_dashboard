from __future__ import annotations

import os
import re
import sqlite3
import json
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
import time

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from board_widget import BoardWidget
from database import FabricationDatabase
from metrics import (
    BossLensMetrics,
    DashboardMetrics,
    compute_boss_lens_metrics,
    compute_dashboard_metrics,
    sort_trucks_natural,
)
from models import RELEASE_STATES, Truck, TruckKit
from schedule import ScheduleInsights, build_schedule_insights
from stages import STAGE_SEQUENCE, Stage, normalize_stage_span, stage_from_id, stage_label, stage_options
from teams_card import build_teams_webhook_payload

DEFAULT_TEAMS_WEBHOOK_URL = (
    "https://default97009fec357647f39ce0fc3d1496b7.b8.environment.api.powerplatform.com:443/"
    "powerautomate/automations/direct/workflows/98b3a4e7ea8c439090e2d40232163817/triggers/manual/"
    "paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=ggEqWDyQT6T3GEouJCsp0jiZPF8mgQI5j5bl4T8T4CQ"
)


def _fmt_week(value: float) -> str:
    return f"W{value:.1f}"


def _current_week_of_label() -> str:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday.strftime("%b %d, %Y")


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
    PDF_LOOKUP_ROOT = Path(r"W:\LASER\For Battleshield Fabrication")

    def __init__(self, truck_number: str, kit: TruckKit, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit Kit - {kit.kit_name}")
        self.setModal(True)
        self.resize(520, 420)
        self._truck_number = str(truck_number or "").strip()

        self._release_combo = QComboBox()
        self._release_combo.addItems(RELEASE_STATES)
        self._release_combo.setCurrentText(kit.release_state)

        self._front_stage_combo = QComboBox()
        self._back_stage_combo = QComboBox()
        for stage_id, label in stage_options():
            self._front_stage_combo.addItem(label, stage_id)
            self._back_stage_combo.addItem(label, stage_id)
        self._set_stage_combo_value(self._front_stage_combo, kit.front_stage_id)
        self._set_stage_combo_value(self._back_stage_combo, kit.back_stage_id)

        self._blocker_input = QLineEdit(kit.blocker)
        self._pdf_links_input = QPlainTextEdit()
        self._pdf_links_input.setPlaceholderText("One PDF path or URL per line")
        self._pdf_links_input.setPlainText(kit.pdf_links.strip())
        self._pdf_links_input.setMinimumHeight(100)

        self._active_checkbox = QCheckBox("Kit is active")
        self._active_checkbox.setChecked(kit.is_active)

        form = QFormLayout()
        form.addRow("Truck", QLabel(truck_number))
        form.addRow("Kit", QLabel(kit.kit_name))
        form.addRow("Release State", self._release_combo)
        form.addRow("Front Stage", self._front_stage_combo)
        form.addRow("Back Stage", self._back_stage_combo)
        form.addRow("Blocker", self._blocker_input)
        form.addRow("PDF Links", self._pdf_links_input)
        form.addRow("", self._active_checkbox)

        remove_button = QPushButton("Remove Kit (Soft)")
        remove_button.clicked.connect(self._mark_removed)

        open_pdf_button = QPushButton("Open PDF Link(s)")
        open_pdf_button.clicked.connect(self._open_pdf_links)
        select_pdf_button = QPushButton("Select PDF File(s)")
        select_pdf_button.clicked.connect(self._select_pdf_links)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)

        actions_layout = QHBoxLayout()
        actions_layout.addWidget(remove_button)
        actions_layout.addWidget(open_pdf_button)
        actions_layout.addWidget(select_pdf_button)
        actions_layout.addStretch(1)

        layout.addLayout(actions_layout)
        layout.addWidget(buttons)

    def _mark_removed(self) -> None:
        self._active_checkbox.setChecked(False)

    @staticmethod
    def _set_stage_combo_value(combo: QComboBox, stage_id: int) -> None:
        index = combo.findData(int(stage_from_id(stage_id)))
        if index < 0:
            index = 0
        combo.setCurrentIndex(index)

    def _normalized_pdf_links(self) -> list[str]:
        values: list[str] = []
        raw_text = self._pdf_links_input.toPlainText().replace(";", "\n")
        for part in raw_text.splitlines():
            clean = part.strip().strip('"')
            if clean:
                values.append(clean)
        return values

    def _open_pdf_links(self) -> None:
        links = self._normalized_pdf_links()
        if not links:
            QMessageBox.information(self, "No Links", "Add at least one PDF path or URL first.")
            return

        if not hasattr(os, "startfile"):
            QMessageBox.warning(self, "Unsupported", "Opening external files is not supported on this platform.")
            return

        failed: list[str] = []
        for link in links:
            try:
                os.startfile(link)  # type: ignore[attr-defined]
            except OSError:
                failed.append(link)

        if failed:
            sample = "\n".join(failed[:3])
            QMessageBox.warning(
                self,
                "Open Failed",
                "Could not open one or more links:\n" f"{sample}",
            )

    @staticmethod
    def _as_local_path(link: str) -> Path | None:
        text = str(link or "").strip().strip('"')
        if not text:
            return None
        # URLs are opened via the existing "Open PDF Link(s)" action.
        if "://" in text:
            return None
        path = Path(text)
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        else:
            path = path.resolve()
        return path

    def _default_pdf_lookup_dir(self) -> str:
        root = self.PDF_LOOKUP_ROOT
        if not root.exists():
            return str(Path.cwd())

        match = re.search(r"(F\d+)", self._truck_number, re.IGNORECASE)
        truck_code = match.group(1).upper() if match else ""
        if truck_code:
            direct = root / truck_code
            if direct.exists():
                return str(direct)
            matches = sorted(path for path in root.glob(f"{truck_code}*") if path.is_dir())
            if matches:
                return str(matches[0])

        fallback_matches = sorted(path for path in root.glob("F*") if path.is_dir())
        if fallback_matches:
            return str(fallback_matches[0])
        return str(root)

    def _select_pdf_links(self) -> None:
        existing = self._normalized_pdf_links()
        start_dir = self._default_pdf_lookup_dir()
        for value in existing:
            local_path = self._as_local_path(value)
            if local_path is None:
                continue
            candidate = local_path if local_path.is_dir() else local_path.parent
            if candidate.exists():
                start_dir = str(candidate)
                break

        selected_paths, _filter_used = QFileDialog.getOpenFileNames(
            self,
            "Select PDF File(s)",
            start_dir,
            "PDF Files (*.pdf);;All Files (*.*)",
        )
        if not selected_paths:
            return

        merged: list[str] = []
        seen: set[str] = set()
        for value in existing + selected_paths:
            clean = str(value).strip().strip('"')
            if not clean:
                continue
            key = clean.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(clean)

        self._pdf_links_input.setPlainText("\n".join(merged))

    def get_values(self) -> dict[str, object]:
        return {
            "release_state": self._release_combo.currentText(),
            "front_stage_id": int(self._front_stage_combo.currentData()),
            "back_stage_id": int(self._back_stage_combo.currentData()),
            "blocker": self._blocker_input.text(),
            "pdf_links": "\n".join(self._normalized_pdf_links()),
            "is_active": self._active_checkbox.isChecked(),
        }


class TruckPlanDialog(QDialog):
    def __init__(self, trucks: list[Truck], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Manage Truck Plan")
        self.setModal(True)
        self.resize(560, 460)

        self._trucks: list[Truck] = [truck for truck in trucks if truck.id is not None]
        self._planned_start_dates_by_id: dict[int, str] = {
            int(truck.id): str(truck.planned_start_date or "").strip()
            for truck in self._trucks
            if truck.id is not None
        }
        self._clients_by_id: dict[int, str] = {
            int(truck.id): str(truck.client or "").strip()
            for truck in self._trucks
            if truck.id is not None
        }
        self._is_visible_by_id: dict[int, bool] = {
            int(truck.id): bool(truck.is_visible)
            for truck in self._trucks
            if truck.id is not None
        }

        self._truck_list = QListWidget()
        self._truck_list.currentRowChanged.connect(self._on_selected_row_changed)

        self._move_up_button = QPushButton("Move Up")
        self._move_up_button.clicked.connect(lambda: self._move_selected(-1))
        self._move_down_button = QPushButton("Move Down")
        self._move_down_button.clicked.connect(lambda: self._move_selected(1))

        self._planned_start_input = QDateEdit()
        self._planned_start_input.setCalendarPopup(True)
        self._planned_start_input.setDisplayFormat("yyyy-MM-dd")
        self._planned_start_input.setDate(QDate.currentDate())
        self._planned_start_input.dateChanged.connect(self._on_planned_start_changed)

        clear_date_button = QPushButton("Clear Date")
        clear_date_button.clicked.connect(self._on_clear_date)

        self._client_input = QLineEdit()
        self._client_input.textChanged.connect(self._on_client_changed)
        self._is_visible_checkbox = QCheckBox("Show truck on main board")
        self._is_visible_checkbox.toggled.connect(self._on_visibility_toggled)

        list_controls = QVBoxLayout()
        list_controls.addWidget(self._move_up_button)
        list_controls.addWidget(self._move_down_button)
        list_controls.addStretch(1)

        list_row = QHBoxLayout()
        list_row.addWidget(self._truck_list, 1)
        list_row.addLayout(list_controls)

        date_form = QFormLayout()
        date_form.addRow("Selected Client", self._client_input)
        date_form.addRow("Selected Day Zero", self._planned_start_input)
        date_form.addRow("", self._is_visible_checkbox)
        date_form.addRow("", clear_date_button)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(list_row)
        layout.addLayout(date_form)
        layout.addWidget(buttons)

        self._refresh_truck_list()

    def _truck_label_text(self, truck: Truck, index: int) -> str:
        truck_id = int(truck.id or 0)
        planned = self._planned_start_dates_by_id.get(truck_id, "").strip()
        client_text = self._clients_by_id.get(truck_id, "").strip() or "-"
        visible_text = "Yes" if self._is_visible_by_id.get(truck_id, True) else "No"
        day_zero_text = planned if planned else "-"
        return (
            f"{index + 1}. {truck.truck_number} | Client: {client_text} | Day Zero: {day_zero_text} "
            f"| Main View: {visible_text}"
        )

    def _refresh_truck_list(self, selected_row: int | None = None) -> None:
        current_row = self._truck_list.currentRow() if selected_row is None else selected_row
        self._truck_list.blockSignals(True)
        self._truck_list.clear()
        for index, truck in enumerate(self._trucks):
            self._truck_list.addItem(self._truck_label_text(truck, index))
        self._truck_list.blockSignals(False)

        if self._truck_list.count() == 0:
            self._move_up_button.setEnabled(False)
            self._move_down_button.setEnabled(False)
            self._planned_start_input.setEnabled(False)
            self._client_input.setEnabled(False)
            self._is_visible_checkbox.setEnabled(False)
            return

        target_row = max(0, min(current_row, self._truck_list.count() - 1))
        self._truck_list.setCurrentRow(target_row)
        self._on_selected_row_changed(target_row)

    def _current_truck(self) -> Truck | None:
        row = self._truck_list.currentRow()
        if row < 0 or row >= len(self._trucks):
            return None
        return self._trucks[row]

    def _move_selected(self, direction: int) -> None:
        current_row = self._truck_list.currentRow()
        if current_row < 0:
            return
        target_row = current_row + direction
        if target_row < 0 or target_row >= len(self._trucks):
            return
        self._trucks[current_row], self._trucks[target_row] = self._trucks[target_row], self._trucks[current_row]
        self._refresh_truck_list(selected_row=target_row)

    def _on_selected_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._trucks):
            self._move_up_button.setEnabled(False)
            self._move_down_button.setEnabled(False)
            self._planned_start_input.setEnabled(False)
            self._client_input.setEnabled(False)
            self._is_visible_checkbox.setEnabled(False)
            return

        self._move_up_button.setEnabled(row > 0)
        self._move_down_button.setEnabled(row < len(self._trucks) - 1)
        self._planned_start_input.setEnabled(True)
        self._client_input.setEnabled(True)
        self._is_visible_checkbox.setEnabled(True)

        truck = self._trucks[row]
        truck_id = int(truck.id or 0)
        planned_start = self._planned_start_dates_by_id.get(truck_id, "").strip()
        client = self._clients_by_id.get(truck_id, "").strip()
        parsed = QDate.fromString(planned_start, "yyyy-MM-dd")
        if not parsed.isValid():
            parsed = QDate.currentDate()
        self._client_input.blockSignals(True)
        self._client_input.setText(client)
        self._client_input.blockSignals(False)
        self._planned_start_input.blockSignals(True)
        self._planned_start_input.setDate(parsed)
        self._planned_start_input.blockSignals(False)
        self._is_visible_checkbox.blockSignals(True)
        self._is_visible_checkbox.setChecked(self._is_visible_by_id.get(truck_id, True))
        self._is_visible_checkbox.blockSignals(False)

    def _on_planned_start_changed(self, value: QDate) -> None:
        truck = self._current_truck()
        if truck is None or truck.id is None:
            return
        self._planned_start_dates_by_id[int(truck.id)] = value.toString("yyyy-MM-dd")
        self._refresh_current_item_label()

    def _on_clear_date(self) -> None:
        truck = self._current_truck()
        if truck is None or truck.id is None:
            return
        self._planned_start_dates_by_id[int(truck.id)] = ""
        self._refresh_current_item_label()

    def _on_client_changed(self, value: str) -> None:
        truck = self._current_truck()
        if truck is None or truck.id is None:
            return
        self._clients_by_id[int(truck.id)] = str(value or "").strip()
        self._refresh_current_item_label()

    def _on_visibility_toggled(self, checked: bool) -> None:
        truck = self._current_truck()
        if truck is None or truck.id is None:
            return
        self._is_visible_by_id[int(truck.id)] = bool(checked)
        self._refresh_current_item_label()

    def _refresh_current_item_label(self) -> None:
        row = self._truck_list.currentRow()
        if row < 0 or row >= len(self._trucks):
            return
        self._truck_list.item(row).setText(self._truck_label_text(self._trucks[row], row))

    def get_updates(self) -> list[tuple[int, int, str, str, bool]]:
        updates: list[tuple[int, int, str, str, bool]] = []
        for index, truck in enumerate(self._trucks, start=1):
            if truck.id is None:
                continue
            planned_start = self._planned_start_dates_by_id.get(int(truck.id), "").strip()
            client = self._clients_by_id.get(int(truck.id), "").strip()
            is_visible = self._is_visible_by_id.get(int(truck.id), True)
            updates.append((int(truck.id), index, planned_start, client, is_visible))
        return updates


class MainWindow(QMainWindow):
    def __init__(
        self,
        database: FabricationDatabase,
        hot_reload_active: bool = False,
        *,
        runtime_dir: Path | None = None,
    ):
        super().__init__()
        self.database = database

        self._trucks: list[Truck] = []
        self._kit_index: dict[int, tuple[Truck, TruckKit]] = {}
        self._schedule_insights: ScheduleInsights | None = None
        self._week_lens_enabled = True
        self._hot_reload_enabled = hot_reload_active
        self._hot_reload_request_id: str = ""
        self._hot_reload_canceled_request_id: str = ""
        self._hot_reload_request_path: Path | None = None
        self._hot_reload_response_path: Path | None = None
        self._hot_reload_bar: QFrame | None = None
        self._hot_reload_timer = None
        self._hot_reload_last_check = 0.0
        self._hot_reload_end_time: float | None = None

        self.setWindowTitle("Fabrication Flow Dashboard")
        self.resize(1600, 900)

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(10)
        self.setCentralWidget(root)

        if self._hot_reload_enabled:
            self._hot_reload_request_path = runtime_dir / "_runtime" / "hot_reload_request.json" if runtime_dir else None
            self._hot_reload_response_path = runtime_dir / "_runtime" / "hot_reload_response.json" if runtime_dir else None

            hot_reload_bar = QFrame()
            hot_reload_bar.setVisible(False)
            hot_reload_bar.setFixedHeight(36)
            hot_reload_bar.setStyleSheet(
                "QFrame { background: #fff4cf; border: 1px solid #d7be6f; border-radius: 6px; }"
                "QLabel { color: #4f3f07; background: transparent; border: none; }"
            )
            hot_reload_layout = QHBoxLayout(hot_reload_bar)
            hot_reload_layout.setContentsMargins(10, 3, 10, 3)
            hot_reload_layout.setSpacing(8)
            hot_reload_label = QLabel("Hot reload requested.")
            hot_reload_label.setStyleSheet("font-size: 13px; font-weight: 700;")
            hot_reload_label.setObjectName("hot_reload_label")
            hot_reload_cancel_button = QPushButton("Cancel Reload")
            hot_reload_cancel_button.setMinimumHeight(24)
            hot_reload_cancel_button.clicked.connect(self._cancel_hot_reload_from_banner)
            hot_reload_layout.addWidget(hot_reload_label)
            hot_reload_layout.addWidget(hot_reload_cancel_button)
            root_layout.addWidget(hot_reload_bar)
            self._hot_reload_bar = hot_reload_bar
            self._hot_reload_label = hot_reload_label
            self._hot_reload_cancel_button = hot_reload_cancel_button

            self._hot_reload_timer = self.startTimer(800)
            self._poll_hot_reload_request()

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_operations_tab(), "Operations")
        root_layout.addWidget(self._tabs, 1)

        self.refresh_view()

    def timerEvent(self, event):  # type: ignore[override]
        if self._hot_reload_timer is not None and event.timerId() == self._hot_reload_timer:
            self._poll_hot_reload_request()
            return
        super().timerEvent(event)

    def _poll_hot_reload_request(self) -> None:
        if not self._hot_reload_enabled:
            return
        if self._hot_reload_request_path is None:
            return

        if not self._hot_reload_request_path.exists():
            if self._hot_reload_request_id:
                self._hot_reload_request_id = ""
                self._hot_reload_canceled_request_id = ""
                self._clear_hot_reload_banner()
            return

        request = self._read_hot_reload_request()
        request_id = request.get("request_id", "").strip()
        if not request_id:
            return
        if request_id == self._hot_reload_canceled_request_id:
            return
        if request_id != self._hot_reload_request_id:
            self._hot_reload_request_id = request_id
            self._hot_reload_canceled_request_id = ""
            ts_epoch = request.get("ts_epoch", 0)
            timeout_sec = request.get("decision_timeout_sec", 10.0)
            try:
                ts_float = float(ts_epoch)
            except (TypeError, ValueError):
                ts_float = float(time.time())
            try:
                timeout_float = max(1.0, float(timeout_sec))
            except (TypeError, ValueError):
                timeout_float = 10.0
            self._hot_reload_end_time = ts_float + timeout_float

        now = float(time.time())
        end_time = self._hot_reload_end_time
        if end_time is None:
            end_time = now + 10.0
            self._hot_reload_end_time = end_time

        file_count = request.get("change_count", None)
        files = request.get("files", [])
        seconds_remaining = max(0, int(end_time - now))
        file_text = f"{int(file_count)} file(s)" if isinstance(file_count, int) else "update(s)"
        if files:
            sample = ", ".join(str(x) for x in files[:3])
            if len(files) > 3:
                sample += ", ..."
            self._hot_reload_label.setText(
                f"Hot reload requested ({file_text}). Auto-reload in {seconds_remaining}s unless canceled. "
                f"Sample: {sample}"
            )
        else:
            self._hot_reload_label.setText(
                f"Hot reload requested ({file_text}). Auto-reload in {seconds_remaining}s unless canceled."
            )
        if self._hot_reload_bar is not None:
            self._hot_reload_bar.setVisible(True)

    def _read_hot_reload_request(self) -> dict[str, str | int | float | list[str]]:
        if self._hot_reload_request_path is None or not self._hot_reload_request_path.exists():
            return {}
        try:
            with self._hot_reload_request_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        out: dict[str, str | int | float | list[str]] = {}
        for key in ("request_id", "change_count", "files", "ts_epoch", "decision_timeout_sec"):
            if key not in payload:
                continue
            out[key] = payload[key]  # type: ignore[assignment]
        return out

    def _clear_hot_reload_banner(self) -> None:
        if self._hot_reload_bar is not None:
            self._hot_reload_bar.setVisible(False)

    def _cancel_hot_reload_from_banner(self) -> None:
        if not self._hot_reload_request_id:
            return
        self._write_hot_reload_response("reject")
        self._hot_reload_canceled_request_id = self._hot_reload_request_id
        self._clear_hot_reload_banner()
        self.statusBar().showMessage("Hot reload canceled for current change batch.", 3000)

    def _write_hot_reload_response(self, action: str) -> None:
        if not self._hot_reload_response_path or not self._hot_reload_request_id:
            return
        payload = {
            "request_id": self._hot_reload_request_id,
            "action": str(action or "").strip().lower(),
        }
        try:
            self._hot_reload_response_path.parent.mkdir(parents=True, exist_ok=True)
            self._hot_reload_response_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            return

    def _build_operations_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        controls = QHBoxLayout()
        plan_trucks_button = QPushButton("Manage Truck Plan")
        plan_trucks_button.clicked.connect(self._on_manage_truck_plan)
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh_view)
        controls.addWidget(plan_trucks_button)
        controls.addWidget(refresh_button)
        controls.addStretch(1)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #475569;")
        controls.addWidget(self._status_label)
        layout.addLayout(controls)

        self._health_strip = self._build_health_strip()
        layout.addWidget(self._health_strip)

        middle_layout = QHBoxLayout()
        middle_layout.setSpacing(10)

        self._board_widget = BoardWidget()
        self._board_widget.set_week_lens_enabled(self._week_lens_enabled)
        self._board_widget.kit_selected.connect(self._on_kit_selected)
        self._board_widget.kit_stage_drop_requested.connect(self._on_kit_stage_drop_requested)
        self._board_widget.kit_tail_forward_requested.connect(self._on_kit_tail_forward_requested)
        middle_layout.addWidget(self._board_widget, 4)

        right_column = QWidget()
        right_column_layout = QVBoxLayout(right_column)
        right_column_layout.setContentsMargins(0, 0, 0, 0)
        right_column_layout.setSpacing(10)
        right_column_layout.addWidget(self._build_attention_panel(), 1)
        middle_layout.addWidget(right_column, 1)

        layout.addLayout(middle_layout)
        return tab

    def _build_boss_lens_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        publish_panel = QFrame()
        publish_panel.setFrameShape(QFrame.StyledPanel)
        publish_panel.setStyleSheet(
            """
            QFrame {
                background-color: #F8FAFC;
                border: 1px solid #D5DEE7;
                border-radius: 8px;
            }
            """
        )
        publish_layout = QHBoxLayout(publish_panel)
        publish_layout.setContentsMargins(10, 8, 10, 8)
        publish_layout.setSpacing(8)

        publish_label = QLabel("Teams Webhook URL")
        publish_label.setStyleSheet("font-size: 12px; color: #334155;")
        publish_layout.addWidget(publish_label)

        default_webhook = DEFAULT_TEAMS_WEBHOOK_URL
        self._teams_webhook_input = QLineEdit(default_webhook)
        self._teams_webhook_input.setPlaceholderText(
            "Paste Power Automate / Teams webhook URL"
        )
        publish_layout.addWidget(self._teams_webhook_input, 1)

        publish_button = QPushButton("Publish to Teams")
        publish_button.clicked.connect(self._publish_boss_lens_to_teams)
        test_auth_button = QPushButton("Test Auth")
        test_auth_button.clicked.connect(self._test_teams_webhook_auth)
        publish_my_version_button = QPushButton("Publish My Version")
        publish_my_version_button.clicked.connect(self._publish_my_version_to_teams)
        publish_layout.addWidget(publish_my_version_button)
        publish_layout.addWidget(test_auth_button)
        publish_layout.addWidget(publish_button)
        layout.addWidget(publish_panel)

        tiles_panel = QFrame()
        tiles_panel.setFrameShape(QFrame.StyledPanel)
        tiles_panel.setStyleSheet(
            """
            QFrame {
                background-color: #F8FAFC;
                border: 1px solid #D5DEE7;
                border-radius: 8px;
            }
            """
        )
        tiles_layout = QGridLayout(tiles_panel)
        tiles_layout.setContentsMargins(8, 8, 8, 8)
        tiles_layout.setHorizontalSpacing(8)
        tiles_layout.setVerticalSpacing(8)

        tile_specs = [
            ("active_trucks", "Active Trucks"),
            ("next_main_released", "Next Main Kit Released"),
            ("bend_buffer", "Bend Buffer Health"),
            ("weld_feed", "Weld Feed Health"),
            ("behind_kits", "Kits Behind Master Schedule"),
            ("late_releases", "Late Releases"),
            ("blocked_kits", "Blocked Kits"),
        ]
        self._boss_tile_widgets: dict[str, dict[str, QWidget]] = {}
        for index, (key, title) in enumerate(tile_specs):
            row = 0 if index < 4 else 1
            col = index if index < 4 else index - 4
            tile = self._create_tile(title)
            tiles_layout.addWidget(tile["frame"], row, col)
            self._boss_tile_widgets[key] = tile

        layout.addWidget(tiles_panel)

        summary_panel = QFrame()
        summary_panel.setFrameShape(QFrame.StyledPanel)
        summary_panel.setStyleSheet(
            """
            QFrame {
                background-color: #F8FAFC;
                border: 1px solid #D5DEE7;
                border-radius: 8px;
            }
            QLabel {
                color: #334155;
            }
            """
        )
        summary_layout = QVBoxLayout(summary_panel)
        summary_layout.setContentsMargins(10, 8, 10, 8)
        summary_layout.setSpacing(6)

        sync_title = QLabel("Schedule Sync")
        sync_title.setStyleSheet("font-size: 14px; font-weight: 700; color: #0F172A;")
        self._boss_sync_label = QLabel("-")
        self._boss_sync_label.setWordWrap(True)
        self._boss_sync_label.setStyleSheet("font-size: 12px;")

        release_title = QLabel("Release Alignment")
        release_title.setStyleSheet("font-size: 14px; font-weight: 700; color: #0F172A;")
        self._boss_release_label = QLabel("-")
        self._boss_release_label.setWordWrap(True)
        self._boss_release_label.setStyleSheet("font-size: 12px;")

        flow_title = QLabel("Flow Health")
        flow_title.setStyleSheet("font-size: 14px; font-weight: 700; color: #0F172A;")
        self._boss_flow_label = QLabel("-")
        self._boss_flow_label.setWordWrap(True)
        self._boss_flow_label.setStyleSheet("font-size: 12px;")

        summary_layout.addWidget(sync_title)
        summary_layout.addWidget(self._boss_sync_label)
        summary_layout.addWidget(release_title)
        summary_layout.addWidget(self._boss_release_label)
        summary_layout.addWidget(flow_title)
        summary_layout.addWidget(self._boss_flow_label)
        layout.addWidget(summary_panel)

        truck_panel = QFrame()
        truck_panel.setFrameShape(QFrame.StyledPanel)
        truck_panel.setStyleSheet(
            """
            QFrame {
                background-color: #F8FAFC;
                border: 1px solid #D5DEE7;
                border-radius: 8px;
            }
            """
        )
        truck_layout = QVBoxLayout(truck_panel)
        truck_layout.setContentsMargins(8, 8, 8, 8)
        truck_layout.setSpacing(6)

        truck_title = QLabel("Per-Truck Summary")
        truck_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #0F172A;")
        truck_layout.addWidget(truck_title)

        self._boss_table = QTableWidget(0, 6)
        self._boss_table.setHorizontalHeaderLabels(
            ["Truck", "Main Kit", "Sync", "Main Released", "Risk", "Issue"]
        )
        self._boss_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._boss_table.setSelectionMode(QAbstractItemView.NoSelection)
        self._boss_table.setAlternatingRowColors(True)
        self._boss_table.verticalHeader().setVisible(False)
        header = self._boss_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        truck_layout.addWidget(self._boss_table)

        layout.addWidget(truck_panel, 1)
        return tab

    def refresh_view(self) -> None:
        loaded_trucks = sort_trucks_natural(self.database.load_trucks_with_kits(active_only=True))
        self._trucks = [
            truck for truck in loaded_trucks if truck.is_visible and not self._is_truck_complete(truck)
        ]
        self._schedule_insights = build_schedule_insights(self._trucks)
        kit_stage_windows_by_truck = self._build_kit_stage_windows_map()

        self._kit_index = {}
        for truck in self._trucks:
            for kit in truck.kits:
                if kit.id is not None:
                    self._kit_index[kit.id] = (truck, kit)

        # Positional call keeps compatibility if board_widget.py is briefly out of sync
        # (e.g., during hot-reload) and still expects the old final parameter name.
        self._board_widget.set_data(
            self._trucks,
            self._schedule_insights.truck_planned_start_week_by_id,
            self._schedule_insights.kit_release_hold_weeks_by_id,
            self._schedule_insights.current_week,
            kit_stage_windows_by_truck,
        )

        metrics = compute_dashboard_metrics(self._trucks, schedule_insights=self._schedule_insights)
        boss_metrics = compute_boss_lens_metrics(
            self._trucks,
            schedule_insights=self._schedule_insights,
            dashboard_metrics=metrics,
        )
        self._update_health_strip(metrics)
        self._update_attention_panel(metrics)
        self._update_boss_lens_view(boss_metrics)

        hold_count = len(self._schedule_insights.release_hold_items)
        self._status_label.setText(
            f"Week of {_current_week_of_label()} | Trucks: {len(self._trucks)} "
            f"| Active Kits: {len(self._kit_index)} | Engineering Holds: {hold_count}"
        )

    def _build_kit_stage_windows_map(self) -> dict[tuple[int, str, int], tuple[float, float]]:
        if not self._schedule_insights:
            return {}
        mapping: dict[tuple[int, str, int], tuple[float, float]] = {}
        planned_start_by_truck_id = self._schedule_insights.truck_planned_start_week_by_id
        for window in self._schedule_insights.kit_operation_windows:
            kit_name = str(window.kit_name or "").strip().lower()
            for truck in self._trucks:
                if truck.id is None:
                    continue
                truck_start_week = planned_start_by_truck_id.get(int(truck.id))
                if truck_start_week is None:
                    continue
                key = (int(truck.id), kit_name, int(window.stage_id))
                mapping[key] = (
                    round(truck_start_week + window.start_week, 2),
                    round(truck_start_week + window.end_week, 2),
                )
        return mapping

    def _on_manage_truck_plan(self) -> None:
        all_trucks = sort_trucks_natural(self.database.load_trucks_with_kits(active_only=True))
        planned_trucks = [truck for truck in all_trucks if not self._is_truck_complete(truck)]
        if not planned_trucks:
            QMessageBox.information(self, "No Trucks", "There are no trucks available to plan.")
            return

        dialog = TruckPlanDialog(trucks=planned_trucks, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return

        updates = dialog.get_updates()
        if not updates:
            return

        self.database.update_truck_plans(updates)
        self.refresh_view()
        self.statusBar().showMessage("Truck plan updated.", 3000)

    def _on_kit_selected(self, kit_id: int) -> None:
        result = self._kit_index.get(kit_id)
        if not result:
            return

        truck, kit = result
        dialog = KitEditDialog(truck_number=truck.truck_number, kit=kit, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return

        values = dialog.get_values()
        front_stage_id, back_stage_id = normalize_stage_span(
            front_stage_id=int(values["front_stage_id"]),
            back_stage_id=int(values["back_stage_id"]),
        )
        try:
            self.database.update_truck_kit(
                kit_id=kit_id,
                release_state=str(values["release_state"]),
                front_stage_id=front_stage_id,
                back_stage_id=back_stage_id,
                blocker=str(values["blocker"]),
                pdf_links=str(values["pdf_links"]),
                is_active=bool(values["is_active"]),
            )
        except sqlite3.Error as exc:
            QMessageBox.critical(self, "Update Failed", f"Could not save kit changes: {exc}")
            return
        self.refresh_view()

    def _on_kit_stage_drop_requested(self, kit_id: int, target_stage_id: int) -> None:
        result = self._kit_index.get(kit_id)
        if not result:
            return

        truck, kit = result
        current_stage = stage_from_id(kit.front_stage_id)
        target_stage = stage_from_id(target_stage_id)
        if int(target_stage) != int(target_stage_id):
            return
        if int(target_stage) == int(current_stage):
            return

        release_state = kit.release_state
        if release_state == "not_released" and target_stage != Stage.RELEASE:
            # Moving into fabrication implies engineering released the kit.
            release_state = "released"

        next_back_stage_id = int(stage_from_id(kit.back_stage_id))
        if target_stage == Stage.COMPLETE:
            next_back_stage_id = int(Stage.COMPLETE)

        front_stage_id, back_stage_id = normalize_stage_span(
            front_stage_id=int(target_stage),
            back_stage_id=next_back_stage_id,
        )

        try:
            self.database.update_truck_kit(
                kit_id=kit_id,
                release_state=release_state,
                front_stage_id=front_stage_id,
                back_stage_id=back_stage_id,
                blocker=kit.blocker,
                is_active=kit.is_active,
            )
        except sqlite3.Error as exc:
            QMessageBox.critical(self, "Move Failed", f"Could not move kit to {stage_label(target_stage)}: {exc}")
            return
        self.refresh_view()
        self.statusBar().showMessage(
            f"Moved {truck.truck_number} {kit.kit_name} to {stage_label(target_stage)}",
            3000,
        )

    def _on_kit_tail_forward_requested(self, kit_id: int) -> None:
        result = self._kit_index.get(kit_id)
        if not result:
            return

        truck, kit = result
        front_stage = stage_from_id(kit.front_stage_id)
        back_stage = stage_from_id(kit.back_stage_id)
        if back_stage >= front_stage:
            return

        back_index = STAGE_SEQUENCE.index(back_stage)
        next_back_stage = STAGE_SEQUENCE[min(back_index + 1, len(STAGE_SEQUENCE) - 1)]
        if next_back_stage > front_stage:
            next_back_stage = front_stage
        if next_back_stage == back_stage:
            return

        reply = QMessageBox.question(
            self,
            "Confirm Tail Collapse",
            (
                f"Collapse the tail for {truck.truck_number} {kit.kit_name} "
                f"from {stage_label(back_stage)} to {stage_label(next_back_stage)}?"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            self.database.update_truck_kit(
                kit_id=kit_id,
                release_state=kit.release_state,
                front_stage_id=int(front_stage),
                back_stage_id=int(next_back_stage),
                blocker=kit.blocker,
                is_active=kit.is_active,
            )
        except sqlite3.Error as exc:
            QMessageBox.critical(self, "Tail Collapse Failed", f"Could not collapse kit tail: {exc}")
            return

        self.refresh_view()
        self.statusBar().showMessage(
            f"Collapsed tail for {truck.truck_number} {kit.kit_name} to {stage_label(next_back_stage)}",
            3000,
        )

    @staticmethod
    def _is_truck_complete(truck: Truck) -> bool:
        active_kits = [kit for kit in truck.kits if kit.is_active]
        if not active_kits:
            return False
        return all(stage_from_id(kit.front_stage_id) == Stage.COMPLETE for kit in active_kits)

    def _build_health_strip(self) -> QWidget:
        strip = QWidget()
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._tile_widgets = {
            "next_main": self._create_tile("Next Body Risk"),
            "bend_buffer": self._create_tile("Bend Buffer Health"),
            "weld_feed": self._create_tile("Weld Feed Status"),
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

        lines = ["Kit Lag / Duration (from truck planned start):"]
        for standard in self._schedule_insights.standards:
            lines.append(
                f"{standard.kit_name}: +{standard.lag_weeks:.1f}w lag, {standard.duration_weeks:.1f}w duration"
            )

        lines.append("")
        lines.append("Operation Standards (weeks from Day Zero):")
        for operation in self._schedule_insights.operation_standards:
            lines.append(
                f"{stage_label(operation.stage_id).upper()}: +{operation.start_offset_weeks:.1f}w start, "
                f"{operation.duration_weeks:.1f}w duration, "
                f"{operation.work_days:.1f}d work, "
                f"{operation.spare_days:+.1f}d spare"
            )

        lines.append("")
        cycle_plan = self._schedule_insights.cycle_plan
        lines.append(
            f"Cycle Plan: repeat every {cycle_plan.repeat_weeks:.1f}w on a {cycle_plan.cycle_weeks:.1f}w cycle "
            f"with {cycle_plan.odd_jobs_weeks:.1f}w odd-jobs reserve"
        )
        cycle_state = "odd-jobs window" if cycle_plan.in_odd_jobs_window else "production window"
        lines.append(
            f"Current cycle position: {_fmt_week(cycle_plan.cycle_position_week)} / "
            f"{cycle_plan.cycle_weeks:.1f}w ({cycle_state})"
        )

        lines.append("")
        kit_windows = self._schedule_insights.kit_operation_windows
        if kit_windows:
            lines.append("Kit Operation Windows (weeks from Day Zero):")
            grouped: dict[str, list[str]] = {}
            for window in kit_windows:
                stage_text = (
                    f"{stage_label(window.stage_id).upper()} {_fmt_week(window.start_week)}-{_fmt_week(window.end_week)}"
                )
                grouped.setdefault(window.kit_name, []).append(stage_text)
            for kit_name, stages in grouped.items():
                lines.append(f"{kit_name}: " + "; ".join(stages))
        else:
            lines.append("Kit Operation Windows: none")

        lines.append("")
        overlaps = self._schedule_insights.operation_overlaps
        if overlaps:
            lines.append("Planned Overlap Windows:")
            for overlap in overlaps:
                lines.append(
                    f"{stage_label(overlap.upstream_stage_id).upper()} -> "
                    f"{stage_label(overlap.downstream_stage_id).upper()}: "
                    f"{overlap.overlap_weeks:.1f}w overlap"
                )
        else:
            lines.append("Planned Overlap Windows: none")

        lines.append("")
        weld_label = stage_label(Stage.WELD).lower()
        concurrency_items = self._schedule_insights.concurrency_items
        if concurrency_items:
            lines.append(f"Live Concurrency ({weld_label} started with upstream still active):")
            for item in concurrency_items[:5]:
                lines.append(
                    f"{item.truck_number}: {item.upstream_open_count} upstream kit(s) still open"
                )
            if len(concurrency_items) > 5:
                lines.append(f"+{len(concurrency_items) - 5} more truck(s)")
        else:
            lines.append("Live Concurrency: none")

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
            detail=(
                f"Released kits in {stage_label(Stage.LASER).lower()}/"
                f"{stage_label(Stage.BEND).lower()}"
            ),
            tone=metrics.bend_buffer.level,
        )

        self._set_tile(
            "weld_feed",
            value=f"{metrics.weld_feed.score:.1f} ({metrics.weld_feed.level.upper()})",
            detail=(
                f"Active kits in {stage_label(Stage.BEND).lower()}/"
                f"{stage_label(Stage.WELD).lower()}"
            ),
            tone=metrics.weld_feed.level,
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

    def _update_boss_lens_view(self, metrics: BossLensMetrics) -> None:
        for tile in metrics.tiles:
            widget = self._boss_tile_widgets.get(tile.key)
            if not widget:
                continue

            color_map = {
                "ok": "#2E7D32",
                "caution": "#A16207",
                "problem": "#C62828",
            }
            color = color_map.get(tile.tone, "#0F172A")
            widget["value"].setText(tile.value)
            widget["value"].setStyleSheet(f"font-size: 20px; font-weight: 700; color: {color};")
            widget["detail"].setText(tile.detail)

        sync = metrics.sync_summary
        self._boss_sync_label.setText(
            f"{sync.in_sync_kits} kits in sync | {sync.behind_kits} kits behind | {sync.ahead_kits} kits ahead"
        )
        self._boss_release_label.setText(metrics.release_summary.summary)
        self._boss_flow_label.setText(metrics.flow_summary)

        self._boss_table.setRowCount(len(metrics.truck_rows))
        for row_index, row in enumerate(metrics.truck_rows):
            values = [
                row.truck_number,
                row.main_stage,
                row.sync_status,
                row.main_released,
                row.risk_category,
                row.issue_summary,
            ]
            for col_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self._boss_table.setItem(row_index, col_index, item)

            tone_color = {
                "ok": "#1F2937",
                "caution": "#92400E",
                "problem": "#991B1B",
            }.get(row.tone, "#1F2937")
            risk_item = self._boss_table.item(row_index, 4)
            issue_item = self._boss_table.item(row_index, 5)
            if risk_item is not None:
                risk_item.setForeground(QColor(tone_color))
            if issue_item is not None:
                issue_item.setForeground(QColor(tone_color))

    def _publish_boss_lens_to_teams(self) -> None:
        webhook_url = self._current_teams_webhook_url()
        if not webhook_url:
            QMessageBox.warning(
                self,
                "Webhook URL Required",
                "Enter a Teams/Power Automate webhook URL first.",
            )
            return

        if self._schedule_insights is None:
            self.refresh_view()
            if self._schedule_insights is None:
                QMessageBox.warning(self, "No Data", "No dashboard data is available to publish.")
                return

        dashboard_metrics = compute_dashboard_metrics(
            self._trucks,
            schedule_insights=self._schedule_insights,
        )
        payload = build_teams_webhook_payload(
            trucks=self._trucks,
            dashboard_metrics=dashboard_metrics,
            schedule_insights=self._schedule_insights,
            max_trucks=20,
        )

        output_path = Path(__file__).resolve().parent / "_runtime" / "boss_lens_teams_card.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        try:
            status = self._post_json_webhook(webhook_url, payload)
            self.statusBar().showMessage(f"Published dashboard snapshot to Teams ({status}).", 4000)
            QMessageBox.information(
                self,
                "Published",
                f"Dashboard snapshot published to Teams.\nHTTP status: {status}\nPayload: {output_path}",
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            body = f"\n\n{detail}" if detail else ""
            QMessageBox.critical(
                self,
                "Publish Failed",
                f"Webhook HTTP error {exc.code}: {exc.reason}{body}",
            )
        except urllib.error.URLError as exc:
            QMessageBox.critical(
                self,
                "Publish Failed",
                f"Webhook URL error: {exc.reason}",
            )
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Publish Failed",
                f"Unexpected error while publishing: {exc}",
            )

    def _test_teams_webhook_auth(self) -> None:
        webhook_url = self._current_teams_webhook_url()
        if not webhook_url:
            QMessageBox.warning(
                self,
                "Webhook URL Required",
                "Enter a Teams/Power Automate webhook URL first.",
            )
            return

        payload = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {
                                "type": "TextBlock",
                                "text": "Fabrication Dashboard Teams Auth Test",
                                "weight": "Bolder",
                                "wrap": True,
                            },
                            {
                                "type": "TextBlock",
                                "text": f"Auth test timestamp: {int(time.time())}",
                                "isSubtle": True,
                                "wrap": True,
                            },
                        ],
                    },
                }
            ],
        }

        output_path = Path(__file__).resolve().parent / "_runtime" / "teams_auth_test_payload.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        try:
            status = self._post_json_webhook(webhook_url, payload)
            self.statusBar().showMessage(f"Teams auth test sent ({status}).", 4000)
            QMessageBox.information(
                self,
                "Auth Test Sent",
                f"Webhook accepted auth test.\nHTTP status: {status}\nPayload: {output_path}",
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            body = f"\n\n{detail}" if detail else ""
            QMessageBox.critical(
                self,
                "Auth Test Failed",
                f"Webhook HTTP error {exc.code}: {exc.reason}{body}",
            )
        except urllib.error.URLError as exc:
            QMessageBox.critical(
                self,
                "Auth Test Failed",
                f"Webhook URL error: {exc.reason}",
            )
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Auth Test Failed",
                f"Unexpected error while sending auth test: {exc}",
            )

    def _publish_my_version_to_teams(self) -> None:
        webhook_url = self._current_teams_webhook_url()
        if not webhook_url:
            QMessageBox.warning(
                self,
                "Webhook URL Required",
                "Enter a Teams/Power Automate webhook URL first.",
            )
            return

        payload_path = Path(__file__).resolve().parent / "_runtime" / "boss_lens_teams_card.json"
        if not payload_path.exists():
            QMessageBox.warning(
                self,
                "Payload Not Found",
                f"Could not find payload file:\n{payload_path}",
            )
            return

        try:
            payload_obj = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.critical(
                self,
                "Publish Failed",
                f"Could not read or parse payload JSON:\n{payload_path}\n\n{exc}",
            )
            return

        if not isinstance(payload_obj, dict):
            QMessageBox.critical(
                self,
                "Publish Failed",
                "Payload file must contain a JSON object.",
            )
            return

        payload: dict[str, object]
        if "attachments" in payload_obj:
            payload = payload_obj
        elif payload_obj.get("type") == "AdaptiveCard":
            payload = {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "contentUrl": None,
                        "content": payload_obj,
                    }
                ],
            }
        else:
            QMessageBox.critical(
                self,
                "Publish Failed",
                "Payload JSON must be a Teams message payload or an AdaptiveCard object.",
            )
            return

        try:
            status = self._post_json_webhook(webhook_url, payload)
            self.statusBar().showMessage(f"Published your JSON payload to Teams ({status}).", 4000)
            QMessageBox.information(
                self,
                "Published",
                f"Published your payload file to Teams.\nHTTP status: {status}\nPayload: {payload_path}",
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            body = f"\n\n{detail}" if detail else ""
            QMessageBox.critical(
                self,
                "Publish Failed",
                f"Webhook HTTP error {exc.code}: {exc.reason}{body}",
            )
        except urllib.error.URLError as exc:
            QMessageBox.critical(
                self,
                "Publish Failed",
                f"Webhook URL error: {exc.reason}",
            )
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Publish Failed",
                f"Unexpected error while publishing payload: {exc}",
            )

    def _current_teams_webhook_url(self) -> str:
        return str(self._teams_webhook_input.text() if hasattr(self, "_teams_webhook_input") else "").strip()

    @staticmethod
    def _post_json_webhook(webhook_url: str, payload: dict[str, object]) -> int:
        raw = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return int(getattr(response, "status", response.getcode()))



