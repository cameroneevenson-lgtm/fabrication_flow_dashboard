from __future__ import annotations

import os
import re
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QMessageBox,
    QWidget,
)

from database import FabricationDatabase
from models import RELEASE_STATES, Truck, TruckKit, pdf_link
from stages import (
    FABRICATION_STAGE_POSITION_SCALE,
    normalize_stage_span,
    stage_from_id,
    stage_options,
)


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
        self._pdf_links_input = QLineEdit(pdf_link(kit.pdf_links))
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
        return pdf_link(self._pdf_links_input.text())

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
