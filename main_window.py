from __future__ import annotations

import os
import re
import sqlite3
import json
import urllib.error
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import time

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtGui import QPixmap
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
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from board_widget import BoardWidget
from dashboard_attention import build_dashboard_attention_lines
from dashboard_helpers import is_truck_complete, signal_state_for_level, sort_trucks_natural
from dashboard_publish import (
    DEFAULT_TEAMS_WEBHOOK_URL,
    build_dashboard_publish_snapshot,
    build_sized_dashboard_publish_payload,
    load_active_dashboard_trucks,
    post_json_webhook,
    write_dashboard_payload,
)
from database import FabricationDatabase
from gantt_overlay import (
    OverlayRow,
    build_overlay_rows,
    compute_overlay_viewport,
    normalize_overlay_row_labels,
    render_overlay_png,
)
from metrics import (
    DashboardMetrics,
    compute_dashboard_metrics,
)
from models import RELEASE_STATES, Truck, TruckKit, first_pdf_link
from schedule import ScheduleInsights, build_schedule_insights
from stages import (
    FABRICATION_STAGE_POSITION_SCALE,
    STAGE_SEQUENCE,
    Stage,
    normalize_stage_span,
    stage_from_id,
    stage_label,
    stage_options,
)
TEAMS_ADAPTIVE_CARD_MAX_PAYLOAD_BYTES = 28_000
SIGNAL_TILE_HEIGHT = 136
SIGNAL_TILE_DETAIL_HEIGHT = 30


def _fmt_week(value: float) -> str:
    return f"W{value:.1f}"


def _current_week_of_label() -> str:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return monday.strftime("%b %d, %Y")


@dataclass(frozen=True)
class DashboardViewState:
    trucks: list[Truck]
    kit_index: dict[int, tuple[Truck, TruckKit]]
    schedule_insights: ScheduleInsights
    dashboard_metrics: DashboardMetrics
    kit_stage_windows_by_truck: dict[tuple[int, str, int], tuple[float, float]]


class WrappingListWidget(QListWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setWordWrap(True)
        self.setUniformItemSizes(False)
        self._rendered_entries: list[tuple[str, str]] = []

    def _append_wrapped_item(self, text: str, color: str) -> None:
        item = QListWidgetItem()
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        label.setStyleSheet(f"padding: 6px 8px; color: {color};")
        self.addItem(item)
        self.setItemWidget(item, label)

    def add_wrapped_item(self, text: str, color: str) -> None:
        self._append_wrapped_item(text=text, color=color)
        self._refresh_item_heights()

    def set_wrapped_items(self, entries: list[tuple[str, str]]) -> None:
        normalized_entries = [(str(text), str(color)) for text, color in entries]
        if normalized_entries == self._rendered_entries:
            return
        self._rendered_entries = list(normalized_entries)
        super().clear()
        for text, color in normalized_entries:
            self._append_wrapped_item(text=text, color=color)
        self._refresh_item_heights()

    def clear(self) -> None:  # type: ignore[override]
        self._rendered_entries = []
        super().clear()

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
        self._kit_name = str(kit.kit_name or "").strip()

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
        self._front_position = int(getattr(kit, "front_position", 10) or 10)
        self._back_position = int(getattr(kit, "back_position", 10) or 10)
        self._front_stage_combo.currentIndexChanged.connect(self._on_stage_selection_changed)
        self._back_stage_combo.currentIndexChanged.connect(self._on_stage_selection_changed)
        self._keep_tail_synced_checkbox = QCheckBox("Keep tail at head")
        self._keep_tail_synced_checkbox.setChecked(bool(getattr(kit, "keep_tail_at_head", True)))
        self._keep_tail_synced_checkbox.toggled.connect(self._on_keep_tail_synced_toggled)

        self._blocker_input = QLineEdit(kit.blocker)
        self._pdf_links_input = QLineEdit(first_pdf_link(kit.pdf_links))
        self._pdf_links_input.setPlaceholderText("Single PDF path or URL")
        position_controls = self._build_position_controls()

        self._active_checkbox = QCheckBox("Kit is active")
        self._active_checkbox.setChecked(kit.is_active)

        form = QFormLayout()
        form.addRow("Truck", QLabel(truck_number))
        form.addRow("Kit", QLabel(kit.kit_name))
        form.addRow("Release State", self._release_combo)
        form.addRow("Front Stage", self._front_stage_combo)
        form.addRow("Back Stage", self._back_stage_combo)
        form.addRow("Head / Tail", position_controls)
        form.addRow("Blocker", self._blocker_input)
        form.addRow("PDF", self._pdf_links_input)
        form.addRow("", self._active_checkbox)

        remove_button = QPushButton("Remove Kit (Soft)")
        remove_button.clicked.connect(self._mark_removed)

        open_pdf_button = QPushButton("Open PDF")
        open_pdf_button.clicked.connect(self._open_pdf_link)
        select_pdf_button = QPushButton("Select PDF")
        select_pdf_button.clicked.connect(self._select_pdf_link)

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
        self._on_stage_selection_changed()

    def _build_position_controls(self) -> QWidget:
        container = QFrame()
        container.setStyleSheet(
            """
            QFrame {
                background-color: #F8FAFC;
                border: 1px solid #D5DEE7;
                border-radius: 6px;
            }
            QLabel {
                border: none;
            }
            """
        )
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        front_row, self._front_position_value_label, self._front_position_back_button, self._front_position_forward_button = (
            self._create_position_stepper_row(
                label="HEAD",
                on_back=lambda: self._adjust_front_position(-1),
                on_forward=lambda: self._adjust_front_position(1),
            )
        )
        tail_row, self._back_position_value_label, self._back_position_back_button, self._back_position_forward_button = (
            self._create_position_stepper_row(
                label="TAIL",
                on_back=lambda: self._adjust_back_position(-1),
                on_forward=lambda: self._adjust_back_position(1),
            )
        )
        layout.addLayout(front_row)
        layout.addLayout(tail_row)
        sync_row = QHBoxLayout()
        sync_row.setContentsMargins(0, 0, 0, 0)
        sync_row.setSpacing(6)
        sync_row.addWidget(self._keep_tail_synced_checkbox)
        sync_now_button = QPushButton("Pull Tail to Head")
        sync_now_button.clicked.connect(self._sync_tail_to_head)
        sync_row.addWidget(sync_now_button)
        sync_row.addStretch(1)
        layout.addLayout(sync_row)
        return container

    def _create_position_stepper_row(self, *, label: str, on_back, on_forward) -> tuple[QHBoxLayout, QLabel, QPushButton, QPushButton]:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        label_widget = QLabel(label)
        label_widget.setStyleSheet("font-size: 10px; font-weight: 700; color: #475569;")
        label_widget.setFixedWidth(34)

        back_button = QPushButton("-")
        back_button.setFixedWidth(26)
        back_button.clicked.connect(on_back)

        value_label = QLabel("--")
        value_label.setAlignment(Qt.AlignCenter)
        value_label.setStyleSheet(
            "font-size: 11px; font-weight: 700; color: #0F172A; "
            "background-color: #FFFFFF; border: 1px solid #CBD5E1; border-radius: 4px; padding: 3px 8px;"
        )

        forward_button = QPushButton("+")
        forward_button.setFixedWidth(26)
        forward_button.clicked.connect(on_forward)

        row.addWidget(label_widget)
        row.addWidget(back_button)
        row.addWidget(value_label, 1)
        row.addWidget(forward_button)
        return (row, value_label, back_button, forward_button)

    def _on_stage_selection_changed(self) -> None:
        if self._keep_tail_synced_checkbox.isChecked():
            self._sync_tail_stage_to_head()

        front_stage_id, back_stage_id = normalize_stage_span(
            front_stage_id=int(self._front_stage_combo.currentData()),
            back_stage_id=int(self._back_stage_combo.currentData()),
        )

        if int(self._front_stage_combo.currentData()) != front_stage_id:
            self._front_stage_combo.blockSignals(True)
            self._set_stage_combo_value(self._front_stage_combo, front_stage_id)
            self._front_stage_combo.blockSignals(False)
        if int(self._back_stage_combo.currentData()) != back_stage_id:
            self._back_stage_combo.blockSignals(True)
            self._set_stage_combo_value(self._back_stage_combo, back_stage_id)
            self._back_stage_combo.blockSignals(False)

        self._front_position, self._back_position = FabricationDatabase._normalize_position_span(
            front_position=self._front_position,
            back_position=self._back_position,
            front_stage_id=front_stage_id,
            back_stage_id=back_stage_id,
        )
        if self._keep_tail_synced_checkbox.isChecked():
            self._back_position = int(self._front_position)
        self._refresh_position_controls()

    @staticmethod
    def _position_index(positions: tuple[int, ...], value: int) -> int:
        if int(value) in positions:
            return positions.index(int(value))
        return min(range(len(positions)), key=lambda idx: abs(positions[idx] - int(value)))

    @staticmethod
    def _format_position_percent(positions: tuple[int, ...], value: int) -> str:
        step_index = KitEditDialog._position_index(positions, value)
        display_steps = [0, 10, 50, 90, 100]
        if len(positions) == len(display_steps):
            return f"{display_steps[step_index]}%"
        if len(positions) <= 1:
            return "100%"
        percent = int(round((float(step_index) / float(len(positions) - 1)) * 100.0))
        return f"{percent}%"

    def _refresh_position_controls(self) -> None:
        front_stage = stage_from_id(self._front_stage_combo.currentData())
        front_positions = FABRICATION_STAGE_POSITION_SCALE.get(front_stage)
        if front_positions:
            front_index = self._position_index(front_positions, self._front_position)
            self._front_position_value_label.setText(
                self._format_position_percent(front_positions, self._front_position)
            )
            self._front_position_back_button.setEnabled(front_index > 0)
            self._front_position_forward_button.setEnabled(front_index < len(front_positions) - 1)
        else:
            self._front_position_value_label.setText("--")
            self._front_position_back_button.setEnabled(False)
            self._front_position_forward_button.setEnabled(False)

        back_stage = stage_from_id(self._back_stage_combo.currentData())
        back_positions = FABRICATION_STAGE_POSITION_SCALE.get(back_stage)
        tail_controls_enabled = not self._keep_tail_synced_checkbox.isChecked()
        if back_positions:
            back_index = self._position_index(back_positions, self._back_position)
            self._back_position_value_label.setText(
                self._format_position_percent(back_positions, self._back_position)
            )
            self._back_position_back_button.setEnabled(tail_controls_enabled and back_index > 0)
            self._back_position_forward_button.setEnabled(tail_controls_enabled and back_index < len(back_positions) - 1)
        else:
            self._back_position_value_label.setText("--")
            self._back_position_back_button.setEnabled(False)
            self._back_position_forward_button.setEnabled(False)
        self._back_stage_combo.setEnabled(tail_controls_enabled)

    def _adjust_front_position(self, delta: int) -> None:
        front_stage = stage_from_id(self._front_stage_combo.currentData())
        positions = FABRICATION_STAGE_POSITION_SCALE.get(front_stage)
        if not positions:
            return

        current_index = self._position_index(positions, self._front_position)
        target_index = max(0, min(len(positions) - 1, current_index + int(delta)))
        self._front_position = int(positions[target_index])
        if self._keep_tail_synced_checkbox.isChecked():
            self._back_position = int(self._front_position)
        self._on_stage_selection_changed()

    def _adjust_back_position(self, delta: int) -> None:
        if self._keep_tail_synced_checkbox.isChecked():
            return
        back_stage = stage_from_id(self._back_stage_combo.currentData())
        positions = FABRICATION_STAGE_POSITION_SCALE.get(back_stage)
        if not positions:
            return

        current_index = self._position_index(positions, self._back_position)
        target_index = max(0, min(len(positions) - 1, current_index + int(delta)))
        self._back_position = int(positions[target_index])
        self._on_stage_selection_changed()

    def _sync_tail_stage_to_head(self) -> None:
        front_stage_id = int(self._front_stage_combo.currentData())
        if int(self._back_stage_combo.currentData()) != front_stage_id:
            self._back_stage_combo.blockSignals(True)
            self._set_stage_combo_value(self._back_stage_combo, front_stage_id)
            self._back_stage_combo.blockSignals(False)

    def _sync_tail_to_head(self) -> None:
        self._sync_tail_stage_to_head()
        self._back_position = int(self._front_position)
        self._on_stage_selection_changed()

    def _on_keep_tail_synced_toggled(self, checked: bool) -> None:
        if checked:
            self._sync_tail_to_head()
            return
        self._refresh_position_controls()

    def _mark_removed(self) -> None:
        self._active_checkbox.setChecked(False)

    @staticmethod
    def _set_stage_combo_value(combo: QComboBox, stage_id: int) -> None:
        index = combo.findData(int(stage_from_id(stage_id)))
        if index < 0:
            index = 0
        combo.setCurrentIndex(index)

    def _normalized_pdf_link(self) -> str:
        return first_pdf_link(self._pdf_links_input.text())

    def _open_pdf_link(self) -> None:
        link = self._normalized_pdf_link()
        if not link:
            QMessageBox.information(self, "No PDF", "Add a PDF path or URL first.")
            return

        if not hasattr(os, "startfile"):
            QMessageBox.warning(self, "Unsupported", "Opening external files is not supported on this platform.")
            return

        try:
            os.startfile(link)  # type: ignore[attr-defined]
        except OSError:
            QMessageBox.warning(
                self,
                "Open Failed",
                f"Could not open:\n{link}",
            )

    @staticmethod
    def _as_local_path(link: str) -> Path | None:
        text = str(link or "").strip().strip('"')
        if not text:
            return None
        # URLs are opened directly from the single-PDF field.
        if "://" in text:
            return None
        path = Path(text)
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        else:
            path = path.resolve()
        return path

    @staticmethod
    def _normalized_lookup_text(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    @classmethod
    def _find_best_subdir_match(cls, parent: Path, query: str) -> Path | None:
        if not parent.exists() or not parent.is_dir():
            return None

        query_norm = cls._normalized_lookup_text(query)
        if not query_norm:
            return None

        tokens = [token for token in query_norm.split() if token]
        if not tokens:
            return None

        partial_pattern = re.compile(".*".join(re.escape(token) for token in tokens), re.IGNORECASE)
        best_path: Path | None = None
        best_score = -1

        try:
            children = sorted((path for path in parent.iterdir() if path.is_dir()), key=lambda p: p.name.lower())
        except OSError:
            return None

        for child in children:
            name_norm = cls._normalized_lookup_text(child.name)
            if not name_norm:
                continue

            score = 0
            if query_norm in name_norm:
                score += 100
            if name_norm in query_norm:
                score += 20
            if partial_pattern.search(name_norm):
                score += 30
            score += sum(1 for token in tokens if token in name_norm)

            if score > best_score and score > 0:
                best_score = score
                best_path = child

        return best_path

    @classmethod
    def _auto_descend_pdf_dir(cls, base_dir: Path, kit_name: str) -> Path:
        if not base_dir.exists() or not base_dir.is_dir():
            return base_dir

        kit_norm = cls._normalized_lookup_text(kit_name)
        if not kit_norm:
            return base_dir

        if "body" in kit_norm:
            body_match = cls._find_best_subdir_match(base_dir, "paint pack")
            if body_match is not None:
                return body_match

        if "pumphouse" in kit_norm or ("pump" in kit_norm and "house" in kit_norm):
            pump_pack_dir = cls._find_best_subdir_match(base_dir, "pump pack")
            if pump_pack_dir is not None:
                pump_house_dir = cls._find_best_subdir_match(pump_pack_dir, "pump house")
                if pump_house_dir is not None:
                    return pump_house_dir
                return pump_pack_dir
            direct_pump_house = cls._find_best_subdir_match(base_dir, "pump house")
            if direct_pump_house is not None:
                return direct_pump_house

        generic_match = cls._find_best_subdir_match(base_dir, kit_norm)
        if generic_match is not None:
            return generic_match
        return base_dir

    def _default_pdf_lookup_dir(self) -> str:
        root = self.PDF_LOOKUP_ROOT
        if not root.exists():
            return str(Path.cwd())

        base_dir = root
        match = re.search(r"(F\d+)", self._truck_number, re.IGNORECASE)
        truck_code = match.group(1).upper() if match else ""
        if truck_code:
            direct = root / truck_code
            if direct.exists():
                base_dir = direct
            else:
                matches = sorted(path for path in root.glob(f"{truck_code}*") if path.is_dir())
                if matches:
                    base_dir = matches[0]

        if base_dir == root:
            fallback_matches = sorted(path for path in root.glob("F*") if path.is_dir())
            if fallback_matches:
                base_dir = fallback_matches[0]

        return str(self._auto_descend_pdf_dir(base_dir, self._kit_name))

    def _select_pdf_link(self) -> None:
        existing = self._normalized_pdf_link()
        start_dir = self._default_pdf_lookup_dir()
        local_path = self._as_local_path(existing)
        if local_path is not None:
            candidate = local_path if local_path.is_dir() else local_path.parent
            if candidate.exists():
                start_dir = str(candidate)

        selected_path, _filter_used = QFileDialog.getOpenFileName(
            self,
            "Select PDF File",
            start_dir,
            "PDF Files (*.pdf);;All Files (*.*)",
        )
        if not selected_path:
            return

        self._pdf_links_input.setText(str(selected_path).strip().strip('"'))

    def get_values(self) -> dict[str, object]:
        return {
            "release_state": self._release_combo.currentText(),
            "front_stage_id": int(self._front_stage_combo.currentData()),
            "back_stage_id": int(self._back_stage_combo.currentData()),
            "front_position": int(self._front_position),
            "back_position": int(self._back_position),
            "keep_tail_at_head": self._keep_tail_synced_checkbox.isChecked(),
            "blocker": self._blocker_input.text(),
            "pdf_links": self._normalized_pdf_link(),
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
        self._dashboard_metrics: DashboardMetrics | None = None
        self._kit_stage_windows_by_truck: dict[tuple[int, str, int], tuple[float, float]] = {}
        self._minority_report_mode = False
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
        root.setObjectName("main_root")
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
            hot_reload_accept_button = QPushButton("Accept Reload")
            hot_reload_accept_button.setMinimumHeight(24)
            hot_reload_accept_button.clicked.connect(self._accept_hot_reload_from_banner)
            hot_reload_cancel_button = QPushButton("Cancel Reload")
            hot_reload_cancel_button.setMinimumHeight(24)
            hot_reload_cancel_button.clicked.connect(self._cancel_hot_reload_from_banner)
            hot_reload_layout.addWidget(hot_reload_label)
            hot_reload_layout.addWidget(hot_reload_accept_button)
            hot_reload_layout.addWidget(hot_reload_cancel_button)
            root_layout.addWidget(hot_reload_bar)
            self._hot_reload_bar = hot_reload_bar
            self._hot_reload_label = hot_reload_label
            self._hot_reload_accept_button = hot_reload_accept_button
            self._hot_reload_cancel_button = hot_reload_cancel_button

            self._hot_reload_timer = self.startTimer(800)
            self._poll_hot_reload_request()

        root_layout.addWidget(self._build_dashboard_view(), 1)

        self.refresh_view()
        self._apply_visual_mode()
        QTimer.singleShot(0, self._queue_gantt_pane_autosize)

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
                f"Click Accept Reload to apply now. "
                f"Sample: {sample}"
            )
        else:
            self._hot_reload_label.setText(
                f"Hot reload requested ({file_text}). Auto-reload in {seconds_remaining}s unless canceled. "
                f"Click Accept Reload to apply now."
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

    def _accept_hot_reload_from_banner(self) -> None:
        if not self._hot_reload_request_id:
            return
        self._write_hot_reload_response("accept")
        self._clear_hot_reload_banner()
        self.statusBar().showMessage("Hot reload accepted; restarting app.", 3000)

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

    def _build_dashboard_view(self) -> QWidget:
        view = QWidget()
        layout = QVBoxLayout(view)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        controls = QHBoxLayout()
        plan_trucks_button = QPushButton("Manage Truck Plan")
        plan_trucks_button.clicked.connect(self._on_manage_truck_plan)
        controls.addWidget(plan_trucks_button)
        update_gantt_button = QPushButton("Update Published Gantt")
        update_gantt_button.clicked.connect(self._publish_gantt_artifacts_only)
        controls.addWidget(update_gantt_button)
        publish_button = QPushButton("Publish to Teams")
        publish_button.clicked.connect(self._publish_dashboard_snapshot_to_teams)
        controls.addWidget(publish_button)
        self._minority_report_checkbox = QCheckBox("Dark Mode")
        self._minority_report_checkbox.setToolTip(
            "Enable transparent dark-mode chrome. Inspired by Minority Report."
        )
        self._minority_report_checkbox.toggled.connect(self._on_minority_report_toggled)
        controls.addWidget(self._minority_report_checkbox)
        controls.addStretch(1)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #475569;")
        controls.addWidget(self._status_label)
        layout.addLayout(controls)

        self._health_strip = self._build_health_strip()
        layout.addWidget(self._health_strip)

        self._board_widget = BoardWidget()
        self._board_widget.kit_selected.connect(self._on_kit_selected)
        self._board_widget.kit_stage_drop_requested.connect(self._on_kit_stage_drop_requested)

        right_column = QWidget()
        right_column_layout = QVBoxLayout(right_column)
        right_column_layout.setContentsMargins(0, 0, 0, 0)
        right_column_layout.setSpacing(10)
        right_column_layout.addWidget(self._build_attention_panel(), 1)

        board_gantt_splitter = QSplitter(Qt.Vertical)
        board_gantt_splitter.addWidget(self._board_widget)
        board_gantt_splitter.addWidget(self._build_gantt_panel())
        board_gantt_splitter.setStretchFactor(0, 4)
        board_gantt_splitter.setStretchFactor(1, 1)
        board_gantt_splitter.setCollapsible(0, False)
        board_gantt_splitter.setCollapsible(1, False)
        board_gantt_splitter.setSizes([760, 240])
        self._lock_splitter(board_gantt_splitter)
        self._board_gantt_splitter = board_gantt_splitter

        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.addWidget(board_gantt_splitter)
        main_splitter.addWidget(right_column)
        main_splitter.setStretchFactor(0, 4)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setCollapsible(0, False)
        main_splitter.setCollapsible(1, False)
        main_splitter.setSizes([1240, 360])
        self._lock_splitter(main_splitter)
        self._main_splitter = main_splitter
        self._right_column = right_column

        layout.addWidget(main_splitter, 1)
        return view

    def _on_minority_report_toggled(self, checked: bool) -> None:
        self._minority_report_mode = bool(checked)
        self._apply_visual_mode()
        if self._schedule_insights is None or self._dashboard_metrics is None:
            return
        with self._batch_dashboard_ui_updates():
            self._update_health_strip(self._dashboard_metrics)
            self._update_attention_panel(self._dashboard_metrics)
            self._update_gantt_panel()

    def _apply_visual_mode(self) -> None:
        dark = bool(self._minority_report_mode)

        if dark:
            self.setStyleSheet(
                """
                QWidget#main_root {
                    background-color: #040B16;
                }
                QPushButton {
                    color: #D8F5FF;
                    background-color: rgba(21, 46, 71, 210);
                    border: 1px solid rgba(124, 217, 255, 140);
                    border-radius: 6px;
                    padding: 5px 10px;
                }
                QPushButton:hover {
                    background-color: rgba(30, 74, 109, 220);
                }
                QCheckBox {
                    color: #B8E7FF;
                    spacing: 6px;
                }
                QCheckBox::indicator {
                    width: 14px;
                    height: 14px;
                    border: 1px solid rgba(124, 217, 255, 180);
                    background: rgba(5, 18, 34, 220);
                }
                QCheckBox::indicator:checked {
                    background: rgba(0, 220, 255, 170);
                }
                QLineEdit, QDateEdit, QComboBox, QPlainTextEdit {
                    color: #D8F5FF;
                    background-color: rgba(5, 18, 33, 220);
                    border: 1px solid rgba(122, 214, 255, 110);
                    border-radius: 6px;
                }
                QHeaderView::section {
                    background-color: rgba(19, 39, 62, 230);
                    color: #CBEAFF;
                    border: 1px solid rgba(122, 214, 255, 110);
                    padding: 4px;
                }
                """
            )
            panel_bg = "rgba(9, 24, 40, 190)"
            panel_border = "rgba(122, 214, 255, 120)"
            title_color = "#9CEBFF"
            text_color = "#C6D8E6"
            muted_color = "#88A5BA"
            list_bg = "rgba(3, 13, 25, 210)"
            list_border = "rgba(122, 214, 255, 120)"
            table_bg = "rgba(3, 13, 25, 220)"
            table_border = "rgba(122, 214, 255, 120)"
            status_color = "#86B6D3"
        else:
            self.setStyleSheet("")
            panel_bg = "#F8FAFC"
            panel_border = "#D5DEE7"
            title_color = "#0F172A"
            text_color = "#334155"
            muted_color = "#475569"
            list_bg = "#FFFFFF"
            list_border = "#CBD5E1"
            table_bg = "#FFFFFF"
            table_border = "#CBD5E1"
            status_color = "#475569"

        panel_style = (
            "QFrame {"
            f" background-color: {panel_bg};"
            f" border: 1px solid {panel_border};"
            " border-radius: 8px;"
            " }"
        )

        if hasattr(self, "_status_label"):
            self._status_label.setStyleSheet(f"color: {status_color};")

        if hasattr(self, "_board_widget"):
            self._board_widget.set_dark_mode(dark)

        if hasattr(self, "_attention_panel"):
            self._attention_panel.setStyleSheet(panel_style)
        if hasattr(self, "_attention_title_label"):
            self._attention_title_label.setStyleSheet(
                f"font-size: 16px; font-weight: 700; color: {title_color};"
            )
        if hasattr(self, "_attention_list"):
            self._attention_list.setStyleSheet(
                f"""
                QListWidget {{
                    background: {list_bg};
                    border: 1px solid {list_border};
                    border-radius: 6px;
                }}
                """
            )

        if hasattr(self, "_gantt_panel"):
            self._gantt_panel.setStyleSheet(panel_style)
        if hasattr(self, "_gantt_title_label"):
            self._gantt_title_label.setStyleSheet(
                f"font-size: 15px; font-weight: 700; color: {title_color};"
            )
        if hasattr(self, "_gantt_tabs"):
            tab_bg = panel_bg if dark else "#E2E8F0"
            selected_tab_bg = list_bg
            self._gantt_tabs.setStyleSheet(
                f"""
                QTabWidget::pane {{
                    border: 0;
                    top: -1px;
                }}
                QTabBar::tab {{
                    background: {tab_bg};
                    color: {muted_color};
                    border: 1px solid {panel_border};
                    border-bottom: none;
                    border-top-left-radius: 8px;
                    border-top-right-radius: 8px;
                    padding: 6px 12px;
                    margin-right: 3px;
                }}
                QTabBar::tab:selected {{
                    background: {selected_tab_bg};
                    color: {title_color};
                }}
                QTabBar::tab:hover {{
                    color: {text_color};
                }}
                """
            )
        for context in getattr(self, "_gantt_contexts", {}).values():
            context["meta_label"].setStyleSheet(f"font-size: 11px; color: {muted_color};")
            context["chart_scroll"].setStyleSheet(
                f"""
                QScrollArea {{
                    background: {list_bg};
                    border: 1px solid {list_border};
                    border-radius: 6px;
                }}
                """
            )
            context["chart_label"].setStyleSheet(f"background: {list_bg};")
            context["table"].setStyleSheet(
                f"""
                QTableWidget {{
                    background: {table_bg};
                    color: {text_color};
                    border: 1px solid {table_border};
                    border-radius: 6px;
                    font-family: Consolas, "Courier New", monospace;
                    font-size: 11px;
                }}
                """
            )

        for tile in getattr(self, "_tile_widgets", {}).values():
            frame = tile.get("frame")
            title_label = tile.get("title")
            detail_label = tile.get("detail")
            if frame is not None:
                frame.setStyleSheet(panel_style)
            if title_label is not None:
                weight = "700" if tile.get("signal_lights") is not None else "400"
                title_label.setStyleSheet(
                    f"font-size: 12px; font-weight: {weight}; color: {text_color};"
                )
            if detail_label is not None:
                detail_label.setStyleSheet(f"font-size: 11px; color: {muted_color};")
            if tile.get("signal_lights") is not None:
                signal_state = str(tile.get("signal_state", "off"))
                self._apply_signal_tile_state(tile, signal_state)

    @staticmethod
    def _build_kit_index(trucks: list[Truck]) -> dict[int, tuple[Truck, TruckKit]]:
        kit_index: dict[int, tuple[Truck, TruckKit]] = {}
        for truck in trucks:
            for kit in truck.kits:
                if kit.id is not None:
                    kit_index[int(kit.id)] = (truck, kit)
        return kit_index

    @staticmethod
    def _build_kit_stage_windows_map(
        trucks: list[Truck],
        schedule_insights: ScheduleInsights,
    ) -> dict[tuple[int, str, int], tuple[float, float]]:
        mapping: dict[tuple[int, str, int], tuple[float, float]] = {}
        planned_start_by_truck_id = schedule_insights.truck_planned_start_week_by_id
        for window in schedule_insights.kit_operation_windows:
            kit_name = str(window.kit_name or "").strip().lower()
            for truck in trucks:
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

    def _build_dashboard_view_state(self, trucks: list[Truck]) -> DashboardViewState:
        # Build one coherent snapshot so board, metrics, attention, and gantt all render from the same state.
        ordered_trucks = sort_trucks_natural(list(trucks))
        schedule_insights = build_schedule_insights(ordered_trucks)
        dashboard_metrics = compute_dashboard_metrics(
            ordered_trucks,
            schedule_insights=schedule_insights,
        )
        kit_index = self._build_kit_index(ordered_trucks)
        kit_stage_windows_by_truck = self._build_kit_stage_windows_map(
            ordered_trucks,
            schedule_insights,
        )
        return DashboardViewState(
            trucks=ordered_trucks,
            kit_index=kit_index,
            schedule_insights=schedule_insights,
            dashboard_metrics=dashboard_metrics,
            kit_stage_windows_by_truck=kit_stage_windows_by_truck,
        )

    @contextmanager
    def _batch_dashboard_ui_updates(self):
        # Suppress intermediate repaints while multiple dashboard surfaces are updated together.
        widgets: list[QWidget] = []
        for candidate in (
            self,
            getattr(self, "_board_widget", None),
            getattr(self, "_attention_list", None),
            getattr(self, "_gantt_tabs", None),
        ):
            if isinstance(candidate, QWidget) and candidate.updatesEnabled():
                candidate.setUpdatesEnabled(False)
                widgets.append(candidate)
        try:
            yield
        finally:
            for widget in reversed(widgets):
                widget.setUpdatesEnabled(True)
                widget.update()

    def _apply_dashboard_view_state(self, state: DashboardViewState) -> None:
        self._trucks = list(state.trucks)
        self._kit_index = dict(state.kit_index)
        self._schedule_insights = state.schedule_insights
        self._dashboard_metrics = state.dashboard_metrics
        self._kit_stage_windows_by_truck = dict(state.kit_stage_windows_by_truck)

        with self._batch_dashboard_ui_updates():
            self._board_widget.set_data(
                self._trucks,
                self._schedule_insights.kit_release_hold_weeks_by_id,
                self._schedule_insights.current_week,
                self._kit_stage_windows_by_truck,
            )
            self._update_health_strip(self._dashboard_metrics)
            self._update_attention_panel(self._dashboard_metrics)
            self._update_gantt_panel()

            hold_count = len(self._schedule_insights.release_hold_items)
            self._status_label.setText(
                f"Week of {_current_week_of_label()} | Trucks: {len(self._trucks)} "
                f"| Active Kits: {len(self._kit_index)} | Engineering Holds: {hold_count}"
            )

    def refresh_view(self) -> None:
        state = self._build_dashboard_view_state(load_active_dashboard_trucks(self.database))
        self._apply_dashboard_view_state(state)

    def _refresh_changed_truck(self, truck_id: int | None) -> None:
        if truck_id is None or truck_id <= 0 or not self._trucks:
            self.refresh_view()
            return

        # Reload only the edited truck from SQLite, then rebuild the derived view state from the in-memory list.
        updated_truck = self.database.load_truck_with_kits(int(truck_id), active_only=True)
        refreshed_trucks = [
            truck for truck in self._trucks
            if int(truck.id or -1) != int(truck_id)
        ]
        if updated_truck is not None and updated_truck.is_visible and not is_truck_complete(updated_truck):
            refreshed_trucks.append(updated_truck)

        state = self._build_dashboard_view_state(refreshed_trucks)
        self._apply_dashboard_view_state(state)

    def _on_manage_truck_plan(self) -> None:
        all_trucks = sort_trucks_natural(self.database.load_trucks_with_kits(active_only=True))
        planned_trucks = [truck for truck in all_trucks if not is_truck_complete(truck)]
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
                front_position=int(values["front_position"]),
                back_position=int(values["back_position"]),
                keep_tail_at_head=bool(values["keep_tail_at_head"]),
                blocker=str(values["blocker"]),
                pdf_links=str(values["pdf_links"]),
                is_active=bool(values["is_active"]),
            )
        except sqlite3.Error as exc:
            QMessageBox.critical(self, "Update Failed", f"Could not save kit changes: {exc}")
            return
        self._refresh_changed_truck(truck.id)

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

        next_back_stage_id = (
            int(target_stage)
            if bool(getattr(kit, "keep_tail_at_head", True))
            else int(kit.back_stage_id)
        )

        front_stage_id, back_stage_id = normalize_stage_span(
            front_stage_id=int(target_stage),
            back_stage_id=next_back_stage_id,
        )
        next_front_position = FabricationDatabase._entry_position_for_stage(front_stage_id)
        next_back_position = (
            next_front_position
            if bool(getattr(kit, "keep_tail_at_head", True))
            else int(getattr(kit, "back_position", FabricationDatabase._entry_position_for_stage(back_stage_id)))
        )

        try:
            self.database.update_truck_kit(
                kit_id=kit_id,
                release_state=release_state,
                front_stage_id=front_stage_id,
                back_stage_id=back_stage_id,
                front_position=next_front_position,
                back_position=next_back_position,
                keep_tail_at_head=bool(getattr(kit, "keep_tail_at_head", True)),
                blocker=kit.blocker,
                is_active=kit.is_active,
            )
        except sqlite3.Error as exc:
            QMessageBox.critical(self, "Move Failed", f"Could not move kit to {stage_label(target_stage)}: {exc}")
            return
        self._refresh_changed_truck(truck.id)
        self.statusBar().showMessage(
            f"Moved {truck.truck_number} {kit.kit_name} to {stage_label(target_stage)}",
            3000,
        )

    def _build_health_strip(self) -> QWidget:
        strip = QWidget()
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._tile_widgets = {
            "laser_buffer": self._create_signal_tile("LASER"),
            "bend_buffer": self._create_signal_tile("BRAKE"),
            "weld_feed_a": self._create_signal_tile("WELD A"),
            "weld_feed_b": self._create_signal_tile("WELD B"),
        }

        for tile in self._tile_widgets.values():
            layout.addWidget(tile["frame"])

        return strip

    def _build_attention_panel(self) -> QWidget:
        panel = QFrame()
        self._attention_panel = panel
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
        self._attention_title_label = title
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

    def _build_gantt_panel(self) -> QWidget:
        panel = QFrame()
        self._gantt_panel = panel
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
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        title = QLabel("Master Schedule vs Actual")
        self._gantt_title_label = title
        title.setWordWrap(True)
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: #0F172A;")
        layout.addWidget(title)

        gantt_tabs = QTabWidget()
        gantt_tabs.setDocumentMode(True)
        gantt_tabs.setUsesScrollButtons(True)
        gantt_tabs.setMovable(False)
        gantt_tabs.currentChanged.connect(self._handle_gantt_tab_changed)
        self._gantt_tabs = gantt_tabs
        layout.addWidget(gantt_tabs, 1)

        self._gantt_context = self._create_gantt_context()
        self._gantt_contexts: dict[str, dict[str, object]] = {"__all__": self._gantt_context}
        self._gantt_context["widget"].setProperty("gantt_key", "__all__")
        gantt_tabs.addTab(self._gantt_context["widget"], "ALL")

        return panel

    def _handle_gantt_tab_changed(self, _index: int) -> None:
        self._rescale_gantt_pixmaps()
        self._queue_gantt_pane_autosize()

    def _create_gantt_context(self) -> dict[str, object]:
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(6)

        meta_label = QLabel("")
        meta_label.setWordWrap(True)
        meta_label.setStyleSheet("font-size: 11px; color: #475569;")
        meta_label.setVisible(False)
        content_layout.addWidget(meta_label)

        chart_scroll = QScrollArea()
        chart_scroll.setWidgetResizable(False)
        chart_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        chart_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        chart_scroll.setStyleSheet(
            """
            QScrollArea {
                background: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
            }
            """
        )
        chart_label = QLabel("")
        chart_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        chart_label.setStyleSheet("background: #FFFFFF;")
        chart_scroll.setWidget(chart_label)
        chart_scroll.setVisible(False)
        content_layout.addWidget(chart_scroll)

        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Truck", "Scheduled", "Actual"])
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setStyleSheet(
            """
            QTableWidget {
                background: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                font-family: Consolas, "Courier New", monospace;
                font-size: 11px;
            }
            """
        )
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        content_layout.addWidget(table)

        return {
            "widget": content,
            "meta_label": meta_label,
            "chart_scroll": chart_scroll,
            "chart_label": chart_label,
            "table": table,
        }

    def _gantt_tab_key(self, truck: Truck) -> str:
        return f"truck:{str(truck.truck_number or '').strip()}"

    def _current_gantt_tab_key(self) -> str:
        tabs = getattr(self, "_gantt_tabs", None)
        if tabs is None or tabs.count() <= 0:
            return "__all__"
        widget = tabs.currentWidget()
        if widget is None:
            return "__all__"
        return str(widget.property("gantt_key") or "__all__")

    def _find_gantt_tab_index(self, key: str) -> int:
        tabs = getattr(self, "_gantt_tabs", None)
        if tabs is None:
            return -1
        for index in range(int(tabs.count())):
            widget = tabs.widget(index)
            if widget is not None and str(widget.property("gantt_key") or "") == str(key):
                return index
        return -1

    def _sync_gantt_tabs(self) -> None:
        tabs = getattr(self, "_gantt_tabs", None)
        contexts = getattr(self, "_gantt_contexts", None)
        if tabs is None or contexts is None:
            return

        current_key = self._current_gantt_tab_key()
        desired_keys = ["__all__"] + [self._gantt_tab_key(truck) for truck in self._trucks]

        for index in range(int(tabs.count()) - 1, -1, -1):
            widget = tabs.widget(index)
            if widget is None:
                continue
            key = str(widget.property("gantt_key") or "")
            if key and key not in desired_keys:
                tabs.removeTab(index)
                contexts.pop(key, None)
                widget.deleteLater()

        for truck in self._trucks:
            key = self._gantt_tab_key(truck)
            title = str(truck.truck_number or "").strip()
            index = self._find_gantt_tab_index(key)
            if index >= 0:
                tabs.setTabText(index, title)
                continue
            context = self._create_gantt_context()
            context["widget"].setProperty("gantt_key", key)
            contexts[key] = context
            tabs.addTab(context["widget"], title)

        current_index = self._find_gantt_tab_index(current_key)
        tabs.setCurrentIndex(current_index if current_index >= 0 else 0)

    @staticmethod
    def _week_to_chart_index(week_value: float, min_week: float, max_week: float, width: int) -> int:
        if width <= 1:
            return 0
        span = max(0.0001, float(max_week - min_week))
        ratio = (float(week_value) - min_week) / span
        idx = int(round(ratio * float(width - 1)))
        return max(0, min(width - 1, idx))

    @staticmethod
    def _week_value_to_date_label(week_value: float, current_week: float) -> str:
        today = date.today()
        current_monday = today - timedelta(days=today.weekday())
        delta_days = (float(week_value) - float(current_week)) * 7.0
        target_date = current_monday + timedelta(days=delta_days)
        return target_date.strftime("%m/%d/%y")

    def _set_gantt_message(self, context: dict[str, object], message: str) -> None:
        chart_label = context["chart_label"]
        chart_scroll = context["chart_scroll"]
        table = context["table"]
        chart_label.clear()
        chart_scroll.setVisible(False)
        table.setVisible(True)
        table.setRowCount(1)
        for col in range(3):
            text = message if col == 0 else ""
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            table.setItem(0, col, item)
        self._queue_gantt_pane_autosize()

    @staticmethod
    def _lock_splitter(splitter: QSplitter) -> None:
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(0)
        for index in range(1, splitter.count()):
            handle = splitter.handle(index)
            handle.setDisabled(True)
            handle.hide()

    def _queue_gantt_pane_autosize(self) -> None:
        if getattr(self, "_gantt_autosize_pending", False):
            return
        self._gantt_autosize_pending = True
        QTimer.singleShot(40, self._apply_queued_gantt_pane_autosize)

    def _apply_queued_gantt_pane_autosize(self) -> None:
        self._gantt_autosize_pending = False
        self._rescale_gantt_pixmaps()
        self._autosize_gantt_pane_to_content()
        self._rescale_gantt_pixmaps()
        self._autosize_gantt_pane_to_content()

    @staticmethod
    def _gantt_content_size(context: dict[str, object]) -> tuple[int, int]:
        chart_scroll = context["chart_scroll"]
        chart_label = context["chart_label"]
        table = context["table"]

        if chart_scroll.isVisible():
            label_size = chart_label.minimumSize()
            content_width = int(label_size.width()) if label_size.isValid() else int(chart_label.sizeHint().width())
            content_height = int(label_size.height()) if label_size.isValid() else int(chart_label.sizeHint().height())
            frame = int(chart_scroll.frameWidth()) * 2
            return (content_width + frame, content_height + frame)

        header = table.horizontalHeader()
        content_width = int(table.frameWidth()) * 2
        for col_index in range(int(table.columnCount())):
            content_width += max(
                int(table.columnWidth(col_index)),
                int(table.sizeHintForColumn(col_index)),
                int(header.sectionSizeHint(col_index)),
            )

        content_height = int(header.height())
        for row_index in range(int(table.rowCount())):
            content_height += int(table.rowHeight(row_index))
        content_height += int(table.frameWidth()) * 2
        return (content_width, content_height)

    def _autosize_gantt_pane_to_content(self) -> None:
        splitter = getattr(self, "_board_gantt_splitter", None)
        main_splitter = getattr(self, "_main_splitter", None)
        right_column = getattr(self, "_right_column", None)
        tabs = getattr(self, "_gantt_tabs", None)
        if splitter is None or main_splitter is None or right_column is None:
            return

        total_height = int(splitter.height())
        total_width = int(main_splitter.width())
        if total_height <= 0 or total_width <= 0:
            return

        panel = getattr(self, "_gantt_panel", None)
        layout = panel.layout() if panel is not None else None
        if layout is None:
            return

        context = getattr(self, "_gantt_context", None)
        if context is None:
            return
        meta_label = context["meta_label"]
        content_width, content_height = self._gantt_content_size(context)

        margins = layout.contentsMargins()
        chrome_height = int(margins.top() + margins.bottom())
        chrome_width = int(margins.left() + margins.right())
        content_pad = 8
        max_header_width = 0
        if hasattr(self, "_gantt_title_label") and self._gantt_title_label.isVisible():
            chrome_height += int(self._gantt_title_label.sizeHint().height())
            max_header_width = max(max_header_width, int(self._gantt_title_label.sizeHint().width()))
            if tabs is not None and tabs.isVisible():
                chrome_height += int(layout.spacing())
        if meta_label.isVisible():
            chrome_height += int(meta_label.sizeHint().height())
            max_header_width = max(max_header_width, int(meta_label.sizeHint().width()))
            chrome_height += int(context["widget"].layout().spacing())
        if tabs is not None and tabs.isVisible():
            tab_bar = tabs.tabBar()
            if tab_bar is not None and tab_bar.isVisible():
                chrome_height += int(tab_bar.sizeHint().height())

        autosize_signature = getattr(self, "_gantt_autosize_signature", None)
        main_sizes = main_splitter.sizes()
        main_handle = max(0, int(main_splitter.handleWidth()))
        handle = max(0, int(splitter.handleWidth()))
        min_board = 120
        splitter_sizes = splitter.sizes()

        cached_signature = getattr(self, "_gantt_locked_signature", None)
        cached_total_width = int(getattr(self, "_gantt_locked_total_width", -1))
        cached_total_height = int(getattr(self, "_gantt_locked_total_height", -1))
        cached_left = int(getattr(self, "_gantt_locked_left_width", 0))
        cached_gantt = int(getattr(self, "_gantt_locked_height", 0))

        if (
            autosize_signature is not None
            and autosize_signature == cached_signature
            and total_width == cached_total_width
            and total_height == cached_total_height
            and cached_left > 0
            and cached_gantt > 0
        ):
            target_left = cached_left
            target_right = max(0, total_width - target_left - main_handle)
            if len(main_sizes) >= 2:
                if abs(int(main_sizes[0]) - int(target_left)) > 1 or abs(int(main_sizes[1]) - int(target_right)) > 1:
                    main_splitter.setSizes([target_left, target_right])

            target_gantt = min(cached_gantt, max(0, total_height - min_board - handle))
            target_board = max(min_board, total_height - target_gantt - handle)
            if len(splitter_sizes) >= 2 and (
                abs(int(splitter_sizes[0]) - int(target_board)) > 1
                or abs(int(splitter_sizes[1]) - int(target_gantt)) > 1
            ):
                splitter.setSizes([target_board, target_gantt])
            return

        desired_left = max(
            900,
            max_header_width + chrome_width,
            content_width + chrome_width + content_pad,
        )
        cached_right_reserve = int(getattr(self, "_gantt_locked_right_width", 0))
        if cached_right_reserve <= 0:
            current_right_width = int(main_sizes[1]) if len(main_sizes) >= 2 else 0
            cached_right_reserve = max(
                260,
                current_right_width,
                int(right_column.minimumSizeHint().width()),
            )
            self._gantt_locked_right_width = cached_right_reserve
        max_left = max(0, total_width - cached_right_reserve - main_handle)
        target_left = max(0, min(desired_left, max_left if max_left > 0 else desired_left))
        target_right = max(0, total_width - target_left - main_handle)
        if len(main_sizes) >= 2:
            if abs(int(main_sizes[0]) - int(target_left)) > 1 or abs(int(main_sizes[1]) - int(target_right)) > 1:
                main_splitter.setSizes([target_left, target_right])

        desired = max(0, int(content_height) + chrome_height + content_pad)
        max_gantt = max(0, total_height - min_board - handle)
        target_gantt = min(desired, max_gantt)
        target_board = max(min_board, total_height - target_gantt - handle)
        if len(splitter_sizes) >= 2 and (
            abs(int(splitter_sizes[0]) - int(target_board)) > 1
            or abs(int(splitter_sizes[1]) - int(target_gantt)) > 1
        ):
            splitter.setSizes([target_board, target_gantt])

        self._gantt_locked_signature = autosize_signature
        self._gantt_locked_total_width = total_width
        self._gantt_locked_total_height = total_height
        self._gantt_locked_left_width = target_left
        self._gantt_locked_height = target_gantt

    def _render_gantt_chart_png(
        self,
        rows: list[OverlayRow],
        *,
        current_week: float,
        min_week: float,
        max_week: float,
        is_per_truck: bool = False,
    ) -> bytes | None:
        return render_overlay_png(
            rows=rows,
            current_week=current_week,
            min_week=min_week,
            max_week=max_week,
            week_label=self._week_value_to_date_label,
            fig_width=10.5,
            dpi=125,
            bar_height=0.58 if is_per_truck else 0.42,
            fig_min_height=2.1 if is_per_truck else 1.1,
            fig_height_per_row=0.24 if is_per_truck else 0.13,
            y_label_size=6.0,
            x_label_size=6.0,
            x_label_text="",
            legend_size=7.0,
            dark_mode=bool(self._minority_report_mode),
        )

    @staticmethod
    def _gantt_target_width(context: dict[str, object]) -> int:
        chart_scroll = context["chart_scroll"]
        viewport_width = int(chart_scroll.viewport().width())
        if viewport_width <= 0:
            viewport_width = int(chart_scroll.width()) - (int(chart_scroll.frameWidth()) * 2)
        return max(1, viewport_width - 4)

    def _gantt_shared_width(self) -> int:
        tabs = getattr(self, "_gantt_tabs", None)
        if tabs is not None:
            pane_width = int(tabs.contentsRect().width())
            if pane_width > 8:
                return max(1, pane_width - 8)
        return 0

    def _gantt_reference_width(self, context: dict[str, object]) -> int:
        shared_width = self._gantt_shared_width()
        if shared_width > 1:
            return shared_width
        return self._gantt_target_width(context)

    def _gantt_shared_canvas_size(self) -> tuple[int, int] | None:
        master_context = getattr(self, "_gantt_context", None)
        if not isinstance(master_context, dict):
            return None
        source_pixmap = master_context.get("source_pixmap")
        if not isinstance(source_pixmap, QPixmap) or source_pixmap.isNull():
            return None
        target_width = self._gantt_reference_width(master_context)
        if target_width <= 1:
            return None
        if source_pixmap.width() > target_width:
            fitted = source_pixmap.scaledToWidth(target_width, Qt.SmoothTransformation)
        else:
            fitted = source_pixmap
        return (int(fitted.width()), int(fitted.height()))

    def _set_context_pixmap(self, context: dict[str, object], pixmap: QPixmap) -> None:
        chart_label = context["chart_label"]
        target_width = self._gantt_reference_width(context)
        if not pixmap.isNull() and pixmap.width() > target_width:
            fitted = pixmap.scaledToWidth(target_width, Qt.SmoothTransformation)
        else:
            fitted = pixmap
        canvas_size = self._gantt_shared_canvas_size()
        if canvas_size is None:
            canvas_width = int(fitted.width())
            canvas_height = int(fitted.height())
        else:
            canvas_width, canvas_height = canvas_size
            canvas_width = max(canvas_width, int(fitted.width()))
            canvas_height = max(canvas_height, int(fitted.height()))
        chart_label.setPixmap(fitted)
        chart_label.resize(canvas_width, canvas_height)
        chart_label.setMinimumSize(canvas_width, canvas_height)

    def _rescale_gantt_pixmaps(self) -> None:
        for context in getattr(self, "_gantt_contexts", {}).values():
            source_pixmap = context.get("source_pixmap")
            if not isinstance(source_pixmap, QPixmap):
                continue
            self._set_context_pixmap(context, source_pixmap)

    @staticmethod
    def _gantt_rows_render_signature(
        *,
        rows: list[OverlayRow],
        current_week: float,
        min_week: float,
        max_week: float,
        is_per_truck: bool,
        dark_mode: bool,
    ) -> tuple[object, ...]:
        # Skip expensive gantt rerenders when the visible rows, viewport, and theme have not changed.
        row_signature: list[tuple[object, ...]] = []
        for row in rows:
            windows_signature = tuple(
                (
                    int(stage),
                    round(float(bounds[0]), 4),
                    round(float(bounds[1]), 4),
                )
                for stage, bounds in sorted(row.windows.items(), key=lambda item: int(item[0]))
            )
            row_signature.append(
                (
                    str(row.row_label or ""),
                    windows_signature,
                    int(row.front_position),
                    int(row.back_position),
                    int(row.expected_position),
                    round(float(row.front_week), 4),
                    round(float(row.back_week), 4),
                    round(float(row.expected_week), 4) if row.expected_week is not None else None,
                    round(float(row.latest_due_week), 4),
                    bool(row.released),
                    bool(row.blocked),
                    str(row.blocked_reason or ""),
                    str(row.status_key or ""),
                    bool(row.is_behind),
                    bool(row.is_not_due),
                )
            )
        return (
            tuple(row_signature),
            round(float(current_week), 4),
            round(float(min_week), 4),
            round(float(max_week), 4),
            bool(is_per_truck),
            bool(dark_mode),
        )

    def _populate_gantt_context(
        self,
        *,
        context: dict[str, object],
        rows: list[OverlayRow],
        current_week: float,
        min_week: float,
        max_week: float,
        is_per_truck: bool,
        empty_message: str,
        render_signature: tuple[object, ...] | None = None,
    ) -> None:
        if render_signature is not None and context.get("render_signature") == render_signature:
            return

        if not rows:
            self._set_gantt_message(context, empty_message)
            if render_signature is not None:
                context["render_signature"] = render_signature
            return

        chart_width = 30
        now_idx = self._week_to_chart_index(current_week, min_week, max_week, chart_width)

        png_data = self._render_gantt_chart_png(
            rows=rows,
            current_week=current_week,
            min_week=float(min_week),
            max_week=float(max_week),
            is_per_truck=is_per_truck,
        )
        chart_label = context["chart_label"]
        chart_scroll = context["chart_scroll"]
        table = context["table"]
        if png_data:
            pixmap = QPixmap()
            if pixmap.loadFromData(png_data, "PNG"):
                context["source_pixmap"] = pixmap
                self._set_context_pixmap(context, pixmap)
                chart_scroll.setVisible(True)
                table.setVisible(False)
                if render_signature is not None:
                    context["render_signature"] = render_signature
                return
        chart_label.clear()
        context.pop("source_pixmap", None)
        chart_scroll.setVisible(False)
        table.setVisible(True)

        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            row_label = row.row_label
            windows = row.windows
            scheduled = ["."] * chart_width
            actual = ["."] * chart_width

            for stage, char in ((Stage.LASER, "L"), (Stage.BEND, "B"), (Stage.WELD, "W")):
                bounds = windows.get(stage)
                if bounds is None:
                    continue
                start_week, end_week = bounds
                start_idx = self._week_to_chart_index(start_week, min_week, max_week, chart_width)
                end_idx = self._week_to_chart_index(end_week, min_week, max_week, chart_width)
                if end_idx < start_idx:
                    end_idx = start_idx
                for idx in range(start_idx, end_idx + 1):
                    scheduled[idx] = char

            back_idx = self._week_to_chart_index(row.back_week, min_week, max_week, chart_width)
            front_idx = self._week_to_chart_index(row.front_week, min_week, max_week, chart_width)
            if back_idx != front_idx:
                left_idx = min(back_idx, front_idx)
                right_idx = max(back_idx, front_idx)
                for idx in range(left_idx + 1, right_idx):
                    actual[idx] = "-"
            actual[back_idx] = "o"
            actual[front_idx] = "O"
            if row.is_behind and row.released and not row.blocked:
                target_week = current_week if row.status_key in {"red", "yellow"} else (
                    row.expected_week if row.expected_week is not None else current_week
                )
                target_idx = self._week_to_chart_index(target_week, min_week, max_week, chart_width)
                if target_idx > front_idx:
                    for idx in range(front_idx + 1, target_idx):
                        actual[idx] = ">"

            if scheduled[now_idx] == ".":
                scheduled[now_idx] = "|"
            if actual[now_idx] == ".":
                actual[now_idx] = "|"

            truck_item = QTableWidgetItem(row_label)
            scheduled_item = QTableWidgetItem("".join(scheduled))
            actual_item = QTableWidgetItem("".join(actual))
            truck_item.setFlags(truck_item.flags() & ~Qt.ItemIsEditable)
            scheduled_item.setFlags(scheduled_item.flags() & ~Qt.ItemIsEditable)
            actual_item.setFlags(actual_item.flags() & ~Qt.ItemIsEditable)
            table.setItem(row_index, 0, truck_item)
            table.setItem(row_index, 1, scheduled_item)
            table.setItem(row_index, 2, actual_item)
        if render_signature is not None:
            context["render_signature"] = render_signature

    def _update_gantt_panel(self) -> None:
        if not hasattr(self, "_gantt_context") or self._schedule_insights is None:
            return

        insights = self._schedule_insights
        current_week = float(insights.current_week)
        self._sync_gantt_tabs()
        previous_signature = getattr(self, "_gantt_locked_signature", None)
        previous_total_width = int(getattr(self, "_gantt_locked_total_width", -1))
        previous_total_height = int(getattr(self, "_gantt_locked_total_height", -1))

        rows = build_overlay_rows(
            trucks=list(self._trucks),
            schedule_insights=insights,
            max_rows=max(1, len(self._trucks) * 8),
        )
        parsed_labels = [str(row.row_label or "").split(" | ", 1) for row in rows]
        shared_truck_width = max((len(parts[0].rstrip()) for parts in parsed_labels if parts), default=0)
        shared_kit_width = max((len(parts[1].rstrip()) for parts in parsed_labels if len(parts) > 1), default=0)
        rows = normalize_overlay_row_labels(
            rows,
            truck_width=shared_truck_width,
            kit_width=shared_kit_width,
        )
        min_week, max_week = compute_overlay_viewport(
            rows=rows,
            current_week=current_week,
            forward_horizon_weeks=8.0,
            side_padding_weeks=0.35,
        )
        self._gantt_autosize_signature = (
            round(float(min_week), 4),
            round(float(max_week), 4),
            len(rows),
        )
        self._populate_gantt_context(
            context=self._gantt_context,
            rows=rows,
            current_week=current_week,
            min_week=min_week,
            max_week=max_week,
            is_per_truck=False,
            empty_message="No gantt data.",
            render_signature=self._gantt_rows_render_signature(
                rows=rows,
                current_week=current_week,
                min_week=min_week,
                max_week=max_week,
                is_per_truck=False,
                dark_mode=bool(self._minority_report_mode),
            ),
        )

        rows_by_truck_number: dict[str, list[OverlayRow]] = {}
        for row in rows:
            truck_number = str(row.row_label or "").split("|", 1)[0].strip()
            rows_by_truck_number.setdefault(truck_number, []).append(row)

        for truck in self._trucks:
            key = self._gantt_tab_key(truck)
            context = self._gantt_contexts.get(key)
            if context is None:
                continue
            truck_rows = list(rows_by_truck_number.get(str(truck.truck_number or "").strip(), []))
            self._populate_gantt_context(
                context=context,
                rows=truck_rows,
                current_week=current_week,
                min_week=min_week,
                max_week=max_week,
                is_per_truck=True,
                empty_message=f"{truck.truck_number} has no gantt rows.",
                render_signature=self._gantt_rows_render_signature(
                    rows=truck_rows,
                    current_week=current_week,
                    min_week=min_week,
                    max_week=max_week,
                    is_per_truck=True,
                    dark_mode=bool(self._minority_report_mode),
                ),
            )
        splitter = getattr(self, "_board_gantt_splitter", None)
        main_splitter = getattr(self, "_main_splitter", None)
        total_height = int(splitter.height()) if splitter is not None else -1
        total_width = int(main_splitter.width()) if main_splitter is not None else -1
        if (
            self._gantt_autosize_signature == previous_signature
            and total_width == previous_total_width
            and total_height == previous_total_height
            and int(getattr(self, "_gantt_locked_left_width", 0)) > 0
            and int(getattr(self, "_gantt_locked_height", 0)) > 0
        ):
            self._rescale_gantt_pixmaps()
            return
        self._queue_gantt_pane_autosize()

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
        frame.setFixedHeight(SIGNAL_TILE_HEIGHT)

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

        return {"frame": frame, "title": title_label, "value": value_label, "detail": detail_label}

    def _create_signal_tile(self, title: str) -> dict[str, object]:
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
        frame.setMinimumHeight(78)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        title_label = QLabel(title)
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setStyleSheet("font-size: 12px; font-weight: 700; color: #334155;")
        layout.addWidget(title_label)

        lights_row = QHBoxLayout()
        lights_row.setContentsMargins(0, 4, 0, 0)
        lights_row.setSpacing(10)
        lights_row.setAlignment(Qt.AlignCenter)

        light_widgets: dict[str, QFrame] = {}
        for name in ("red", "yellow", "green"):
            light = QFrame()
            light.setFixedSize(24, 24)
            lights_row.addWidget(light)
            light_widgets[name] = light

        layout.addLayout(lights_row)

        layout.addStretch(1)

        tile: dict[str, object] = {
            "frame": frame,
            "title": title_label,
            "detail": None,
            "signal_lights": light_widgets,
            "signal_state": "off",
            "signal_dark_mode": bool(self._minority_report_mode),
        }
        self._apply_signal_tile_state(tile, "off")
        return tile

    def _apply_signal_tile_state(self, tile: dict[str, object], state: str) -> None:
        lights = tile.get("signal_lights")
        if not isinstance(lights, dict):
            return
        dark = bool(self._minority_report_mode)
        if (
            str(tile.get("signal_state", "off")) == str(state)
            and bool(tile.get("signal_dark_mode", dark)) == dark
        ):
            return

        palette = {
            "red": {
                "active_fill": "#FF6B6B" if dark else "#DC2626",
                "active_border": "#FFD1D1" if dark else "#991B1B",
                "inactive_fill": "rgba(255, 107, 107, 0.28)" if dark else "rgba(220, 38, 38, 0.22)",
                "inactive_border": "rgba(255, 209, 209, 0.65)" if dark else "rgba(153, 27, 27, 0.38)",
            },
            "yellow": {
                "active_fill": "#FFD166" if dark else "#F59E0B",
                "active_border": "#FFE7A3" if dark else "#B45309",
                "inactive_fill": "rgba(255, 209, 102, 0.28)" if dark else "rgba(245, 158, 11, 0.22)",
                "inactive_border": "rgba(255, 231, 163, 0.7)" if dark else "rgba(180, 83, 9, 0.38)",
            },
            "green": {
                "active_fill": "#6DFFB0" if dark else "#16A34A",
                "active_border": "#C8FFE0" if dark else "#166534",
                "inactive_fill": "rgba(109, 255, 176, 0.28)" if dark else "rgba(22, 163, 74, 0.22)",
                "inactive_border": "rgba(200, 255, 224, 0.7)" if dark else "rgba(22, 101, 52, 0.38)",
            },
        }

        for name, widget in lights.items():
            if not isinstance(widget, QFrame):
                continue
            colors = palette.get(name, palette["red"])
            fill = colors["active_fill"] if name == state else colors["inactive_fill"]
            border = colors["active_border"] if name == state else colors["inactive_border"]
            widget.setStyleSheet(
                f"border-radius: 12px; background-color: {fill}; border: 1px solid {border};"
            )

        tile["signal_state"] = state
        tile["signal_dark_mode"] = dark

    @staticmethod
    def _format_late_weeks(value: float) -> str:
        rounded_weeks = max(0, int(float(value) + 0.5))
        unit = "week" if rounded_weeks == 1 else "weeks"
        return f"{rounded_weeks} {unit} late"

    def _set_signal_tile_detail(self, key: str, drivers: tuple[str, ...] | list[str]) -> None:
        tile = self._tile_widgets.get(key)
        if not tile:
            return
        detail_label = tile.get("detail")
        if isinstance(detail_label, QLabel):
            detail_label.setText("")

    def _update_health_strip(self, metrics: DashboardMetrics) -> None:
        self._apply_signal_tile_state(
            self._tile_widgets["laser_buffer"],
            signal_state_for_level(metrics.laser_buffer.level, family="laser"),
        )
        self._set_signal_tile_detail("laser_buffer", metrics.laser_buffer.drivers)
        self._apply_signal_tile_state(
            self._tile_widgets["bend_buffer"],
            signal_state_for_level(metrics.bend_buffer.level, family="brake"),
        )
        self._set_signal_tile_detail("bend_buffer", metrics.bend_buffer.drivers)
        self._apply_signal_tile_state(
            self._tile_widgets["weld_feed_a"],
            signal_state_for_level(metrics.weld_feed_a.level, family="weld"),
        )
        self._set_signal_tile_detail("weld_feed_a", metrics.weld_feed_a.drivers)
        self._apply_signal_tile_state(
            self._tile_widgets["weld_feed_b"],
            signal_state_for_level(metrics.weld_feed_b.level, family="weld"),
        )
        self._set_signal_tile_detail("weld_feed_b", metrics.weld_feed_b.drivers)

    def _update_attention_panel(self, metrics: DashboardMetrics) -> None:
        if self._schedule_insights is None:
            self._attention_list.set_wrapped_items([])
            return
        attention_lines = build_dashboard_attention_lines(
            trucks=self._trucks,
            dashboard_metrics=metrics,
            schedule_insights=self._schedule_insights,
            include_empty_message=True,
        )
        rendered_lines: list[tuple[str, str]] = []
        for item in attention_lines:
            if item.tone == "problem":
                color = "#FF7A7A" if self._minority_report_mode else "#B91C1C"
            elif item.tone == "caution":
                color = "#FFC67A" if self._minority_report_mode else "#A16207"
            elif item.tone == "muted":
                color = "#B7D5EA" if self._minority_report_mode else "#475569"
            else:
                color = "#B7D5EA" if self._minority_report_mode else "#1F2937"
            rendered_lines.append((item.text, color))
        self._attention_list.set_wrapped_items(rendered_lines)

    def _publish_dashboard_snapshot_to_teams(self) -> None:
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

        payload_size = 0
        row_limit = 0
        try:
            snapshot = build_dashboard_publish_snapshot(
                project_root=Path(__file__).resolve().parent,
                trucks=self._trucks,
                schedule_insights=self._schedule_insights,
                dashboard_metrics=self._dashboard_metrics,
            )
            payload, payload_size, row_limit = build_sized_dashboard_publish_payload(
                snapshot=snapshot,
                max_payload_bytes=TEAMS_ADAPTIVE_CARD_MAX_PAYLOAD_BYTES,
            )

            project_root = Path(__file__).resolve().parent
            output_path = write_dashboard_payload(project_root / "_runtime" / "teams_dashboard_card.json", payload)

            status, _body = post_json_webhook(webhook_url, payload)
            self.statusBar().showMessage(
                f"Published dashboard snapshot to Teams ({status}, {payload_size} bytes, {row_limit} rows).",
                5000,
            )
            QMessageBox.information(
                self,
                "Published",
                (
                    "Dashboard snapshot published to Teams.\n"
                    f"Artifacts:\n"
                    f"- Summary HTML: {snapshot.artifacts.summary_html_path}\n"
                    f"- Gantt PNG: {snapshot.artifacts.gantt_png_path or 'not generated'}\n"
                    f"- Status JSON: {snapshot.artifacts.status_json_path}\n"
                    f"Resolved links:\n"
                    f"- Dashboard: {snapshot.artifacts.action_links.get('summary_html_url', '')}\n"
                    f"- Gantt: {snapshot.artifacts.action_links.get('gantt_png_url', '')}\n"
                    f"- JSON: {snapshot.artifacts.action_links.get('status_json_url', '')}\n"
                    f"HTTP status: {status}\n"
                    f"Payload bytes: {payload_size}\n"
                    f"Rows: {row_limit}\n"
                    f"Payload: {output_path}"
                ),
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            body = f"\n\n{detail}" if detail else ""
            QMessageBox.critical(
                self,
                "Publish Failed",
                (
                    f"Webhook HTTP error {exc.code}: {exc.reason}\n"
                    f"Payload bytes: {payload_size}\n"
                    f"Rows: {row_limit}"
                    f"{body}"
                ),
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

    def _publish_gantt_artifacts_only(self) -> None:
        if self._schedule_insights is None:
            self.refresh_view()
            if self._schedule_insights is None:
                QMessageBox.warning(self, "No Data", "No dashboard data is available to publish.")
                return

        try:
            snapshot = build_dashboard_publish_snapshot(
                project_root=Path(__file__).resolve().parent,
                trucks=self._trucks,
                schedule_insights=self._schedule_insights,
                dashboard_metrics=self._dashboard_metrics,
            )
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Gantt Update Failed",
                f"Could not update published gantt: {exc}",
            )
            return

        self.statusBar().showMessage(
            (
                f"Published gantt updated: {snapshot.artifacts.gantt_png_path or 'not generated'}"
                if snapshot.artifacts.gantt_png_path
                else "Published gantt update completed."
            ),
            5000,
        )

    def _current_teams_webhook_url(self) -> str:
        if hasattr(self, "_teams_webhook_input"):
            value = str(self._teams_webhook_input.text() or "").strip()
            if value:
                return value
        return str(DEFAULT_TEAMS_WEBHOOK_URL).strip()





