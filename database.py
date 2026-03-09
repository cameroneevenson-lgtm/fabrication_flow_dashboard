from __future__ import annotations

import sqlite3
from pathlib import Path

from models import DEFAULT_KIT_TEMPLATES, Truck, TruckKit, now_iso


class FabricationDatabase:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS Truck (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    truck_number TEXT NOT NULL UNIQUE,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS KitTemplate (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kit_name TEXT NOT NULL,
                    kit_order INTEGER NOT NULL,
                    is_main_kit INTEGER NOT NULL DEFAULT 0,
                    default_magnitude TEXT NOT NULL CHECK(default_magnitude IN ('small', 'medium', 'large')),
                    is_active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS TruckKit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    truck_id INTEGER NOT NULL,
                    kit_template_id INTEGER,
                    parent_kit_id INTEGER,
                    kit_name TEXT NOT NULL,
                    kit_order INTEGER NOT NULL,
                    is_main_kit INTEGER NOT NULL DEFAULT 0,
                    magnitude TEXT NOT NULL CHECK(magnitude IN ('small', 'medium', 'large')),
                    release_state TEXT NOT NULL CHECK(release_state IN ('not_released', 'partial', 'released')),
                    current_stage TEXT NOT NULL CHECK(current_stage IN ('release', 'laser', 'bend', 'weld', 'welded')),
                    blocker TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(truck_id) REFERENCES Truck(id),
                    FOREIGN KEY(kit_template_id) REFERENCES KitTemplate(id),
                    FOREIGN KEY(parent_kit_id) REFERENCES TruckKit(id)
                );

                CREATE INDEX IF NOT EXISTS idx_truckkit_truck ON TruckKit(truck_id);
                CREATE INDEX IF NOT EXISTS idx_truckkit_active ON TruckKit(is_active);
                """
            )
            self._ensure_default_templates(connection)
            connection.commit()

    def has_trucks(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM Truck").fetchone()
            return bool(row["count"])

    def create_truck(self, truck_number: str, notes: str = "") -> int:
        clean_truck_number = truck_number.strip()
        if not clean_truck_number:
            raise ValueError("truck_number cannot be empty")

        now_value = now_iso()
        with self._connect() as connection:
            truck_cursor = connection.execute(
                """
                INSERT INTO Truck (truck_number, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (clean_truck_number, notes.strip(), now_value, now_value),
            )
            truck_id = int(truck_cursor.lastrowid)

            templates = connection.execute(
                """
                SELECT id, kit_name, kit_order, is_main_kit, default_magnitude
                FROM KitTemplate
                WHERE is_active = 1
                ORDER BY kit_order, id
                """
            ).fetchall()

            for template in templates:
                connection.execute(
                    """
                    INSERT INTO TruckKit (
                        truck_id,
                        kit_template_id,
                        parent_kit_id,
                        kit_name,
                        kit_order,
                        is_main_kit,
                        magnitude,
                        release_state,
                        current_stage,
                        blocker,
                        is_active,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, NULL, ?, ?, ?, ?, 'not_released', 'release', '', 1, ?, ?)
                    """,
                    (
                        truck_id,
                        int(template["id"]),
                        template["kit_name"],
                        int(template["kit_order"]),
                        int(template["is_main_kit"]),
                        template["default_magnitude"],
                        now_value,
                        now_value,
                    ),
                )

            connection.commit()
        return truck_id

    def get_kits_for_truck(self, truck_id: int, active_only: bool = True) -> list[TruckKit]:
        where_clause = "AND is_active = 1" if active_only else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id,
                    truck_id,
                    kit_template_id,
                    parent_kit_id,
                    kit_name,
                    kit_order,
                    is_main_kit,
                    magnitude,
                    release_state,
                    current_stage,
                    blocker,
                    is_active,
                    created_at,
                    updated_at
                FROM TruckKit
                WHERE truck_id = ? {where_clause}
                ORDER BY kit_order, id
                """,
                (truck_id,),
            ).fetchall()

        return [self._row_to_kit(row) for row in rows]

    def update_truck_kit(
        self,
        kit_id: int,
        release_state: str,
        current_stage: str,
        magnitude: str,
        blocker: str,
        is_active: bool,
    ) -> None:
        now_value = now_iso()
        with self._connect() as connection:
            truck_row = connection.execute(
                "SELECT truck_id FROM TruckKit WHERE id = ?",
                (kit_id,),
            ).fetchone()
            if not truck_row:
                return

            connection.execute(
                """
                UPDATE TruckKit
                SET release_state = ?,
                    current_stage = ?,
                    magnitude = ?,
                    blocker = ?,
                    is_active = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    release_state,
                    current_stage,
                    magnitude,
                    blocker.strip(),
                    int(is_active),
                    now_value,
                    kit_id,
                ),
            )

            connection.execute(
                "UPDATE Truck SET updated_at = ? WHERE id = ?",
                (now_value, int(truck_row["truck_id"])),
            )
            connection.commit()

    def load_trucks_with_kits(self, active_only: bool = True) -> list[Truck]:
        with self._connect() as connection:
            truck_rows = connection.execute(
                """
                SELECT id, truck_number, notes, created_at, updated_at
                FROM Truck
                ORDER BY id
                """
            ).fetchall()

            trucks: list[Truck] = [
                Truck(
                    id=int(row["id"]),
                    truck_number=row["truck_number"],
                    notes=row["notes"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    kits=[],
                )
                for row in truck_rows
            ]

            by_truck_id = {truck.id: truck for truck in trucks if truck.id is not None}
            where_clause = "WHERE is_active = 1" if active_only else ""

            kit_rows = connection.execute(
                f"""
                SELECT
                    id,
                    truck_id,
                    kit_template_id,
                    parent_kit_id,
                    kit_name,
                    kit_order,
                    is_main_kit,
                    magnitude,
                    release_state,
                    current_stage,
                    blocker,
                    is_active,
                    created_at,
                    updated_at
                FROM TruckKit
                {where_clause}
                ORDER BY truck_id, kit_order, id
                """
            ).fetchall()

        for row in kit_rows:
            truck = by_truck_id.get(int(row["truck_id"]))
            if not truck:
                continue
            truck.kits.append(self._row_to_kit(row))

        return trucks

    def seed_sample_data(self, sample_trucks: list[Truck]) -> None:
        if self.has_trucks():
            return

        for sample_truck in sample_trucks:
            truck_id = self.create_truck(
                truck_number=sample_truck.truck_number,
                notes=sample_truck.notes,
            )
            created_kits = self.get_kits_for_truck(truck_id=truck_id, active_only=False)
            kits_by_name = {kit.kit_name: kit for kit in created_kits}

            for sample_kit in sample_truck.kits:
                target_kit = kits_by_name.get(sample_kit.kit_name)
                if not target_kit or target_kit.id is None:
                    continue
                self.update_truck_kit(
                    kit_id=target_kit.id,
                    release_state=sample_kit.release_state,
                    current_stage=sample_kit.current_stage,
                    magnitude=sample_kit.magnitude,
                    blocker=sample_kit.blocker,
                    is_active=sample_kit.is_active,
                )

    def _ensure_default_templates(self, connection: sqlite3.Connection) -> None:
        row = connection.execute("SELECT COUNT(*) AS count FROM KitTemplate").fetchone()
        if row["count"] > 0:
            return

        for template in DEFAULT_KIT_TEMPLATES:
            connection.execute(
                """
                INSERT INTO KitTemplate (
                    kit_name,
                    kit_order,
                    is_main_kit,
                    default_magnitude,
                    is_active
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    template.kit_name,
                    template.kit_order,
                    int(template.is_main_kit),
                    template.default_magnitude,
                    int(template.is_active),
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _row_to_kit(row: sqlite3.Row) -> TruckKit:
        return TruckKit(
            id=int(row["id"]),
            truck_id=int(row["truck_id"]),
            kit_template_id=int(row["kit_template_id"]) if row["kit_template_id"] is not None else None,
            parent_kit_id=int(row["parent_kit_id"]) if row["parent_kit_id"] is not None else None,
            kit_name=row["kit_name"],
            kit_order=int(row["kit_order"]),
            is_main_kit=bool(row["is_main_kit"]),
            magnitude=row["magnitude"],
            release_state=row["release_state"],
            current_stage=row["current_stage"],
            blocker=row["blocker"] or "",
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
