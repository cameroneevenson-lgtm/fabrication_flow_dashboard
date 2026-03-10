from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from models import DEFAULT_KIT_TEMPLATES, Truck, TruckKit, now_iso


class FabricationDatabase:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def initialize(self) -> None:
        with self._connect() as connection:
            # Disable FK enforcement for schema-shape upgrades (table rebuilds).
            connection.commit()
            connection.execute("PRAGMA foreign_keys = OFF")
            try:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS Truck (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        truck_number TEXT NOT NULL UNIQUE,
                        client TEXT NOT NULL DEFAULT '',
                        notes TEXT NOT NULL DEFAULT '',
                        is_visible INTEGER NOT NULL DEFAULT 1,
                        build_order INTEGER NOT NULL DEFAULT 0,
                        planned_start_date TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS KitTemplate (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kit_name TEXT NOT NULL,
                        kit_order INTEGER NOT NULL,
                        is_main_kit INTEGER NOT NULL DEFAULT 0,
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
                        release_state TEXT NOT NULL CHECK(release_state IN ('not_released', 'released')),
                        current_stage TEXT NOT NULL CHECK(current_stage IN ('release', 'laser', 'bend', 'weld', 'complete')),
                        blocker TEXT NOT NULL DEFAULT '',
                        pdf_links TEXT NOT NULL DEFAULT '',
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
                self._ensure_truck_columns(connection)
                self._ensure_kittemplate_schema(connection)
                self._ensure_truckkit_columns(connection)
                self._ensure_truckkit_schema(connection)
                self._normalize_release_states(connection)
                self._normalize_stage_values(connection)
                self._ensure_default_templates(connection)
                connection.commit()
            finally:
                connection.commit()
                connection.execute("PRAGMA foreign_keys = ON")

    def has_trucks(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM Truck").fetchone()
            return bool(row["count"])

    def wipe_database(self) -> None:
        # Preferred path: wipe rows in-place so we avoid Windows file-lock issues.
        try:
            self._wipe_database_in_place()
            return
        except sqlite3.Error:
            # Fall back to file delete/recreate if in-place wipe cannot complete.
            pass

        db_file = Path(self.db_path)
        if db_file.exists():
            db_file.unlink()
        self.initialize()

    def create_truck(
        self,
        truck_number: str,
        client: str = "",
        notes: str = "",
        planned_start_date: str = "",
        build_order: int | None = None,
    ) -> int:
        clean_truck_number = truck_number.strip()
        if not clean_truck_number:
            raise ValueError("truck_number cannot be empty")
        clean_client = str(client or "").strip()
        clean_planned_start_date = self._normalize_iso_date(planned_start_date)

        now_value = now_iso()
        with self._connect() as connection:
            next_build_order = build_order
            if next_build_order is None or int(next_build_order) <= 0:
                order_row = connection.execute(
                    "SELECT COALESCE(MAX(build_order), 0) + 1 AS next_order FROM Truck"
                ).fetchone()
                next_build_order = int(order_row["next_order"]) if order_row else 1

            truck_cursor = connection.execute(
                """
                INSERT INTO Truck (
                    truck_number,
                    client,
                    notes,
                    is_visible,
                    build_order,
                    planned_start_date,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_truck_number,
                    clean_client,
                    notes.strip(),
                    1,
                    int(next_build_order),
                    clean_planned_start_date,
                    now_value,
                    now_value,
                ),
            )
            truck_id = int(truck_cursor.lastrowid)

            templates = connection.execute(
                """
                SELECT id, kit_name, kit_order, is_main_kit
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
                        release_state,
                        current_stage,
                        blocker,
                        pdf_links,
                        is_active,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, NULL, ?, ?, ?, 'not_released', 'release', '', '', 1, ?, ?)
                    """,
                    (
                        truck_id,
                        int(template["id"]),
                        template["kit_name"],
                        int(template["kit_order"]),
                        int(template["is_main_kit"]),
                        now_value,
                        now_value,
                    ),
                )

            connection.commit()
        return truck_id

    def update_truck_plans(self, plans: list[tuple[int, int, str, str, bool]]) -> None:
        if not plans:
            return

        now_value = now_iso()
        with self._connect() as connection:
            for truck_id, build_order, planned_start_date, client, is_visible in plans:
                connection.execute(
                    """
                    UPDATE Truck
                    SET build_order = ?,
                        planned_start_date = ?,
                        client = ?,
                        is_visible = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        max(1, int(build_order)),
                        self._normalize_iso_date(planned_start_date),
                        str(client or "").strip(),
                        int(bool(is_visible)),
                        now_value,
                        int(truck_id),
                    ),
                )
            connection.commit()

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
                    release_state,
                    current_stage,
                    blocker,
                    pdf_links,
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
        blocker: str,
        is_active: bool,
        pdf_links: str | None = None,
    ) -> None:
        normalized_release_state = self._normalize_release_state(release_state)
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
                    blocker = ?,
                    pdf_links = COALESCE(?, pdf_links),
                    is_active = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized_release_state,
                    current_stage,
                    blocker.strip(),
                    pdf_links.strip() if isinstance(pdf_links, str) else None,
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
                SELECT id, truck_number, client, notes, is_visible, build_order, planned_start_date, created_at, updated_at
                FROM Truck
                ORDER BY build_order, id
                """
            ).fetchall()

            trucks: list[Truck] = [
                Truck(
                    id=int(row["id"]),
                    truck_number=row["truck_number"],
                    client=row["client"] or "",
                    notes=row["notes"],
                    is_visible=bool(row["is_visible"]),
                    build_order=int(row["build_order"] or 0),
                    planned_start_date=row["planned_start_date"] or "",
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
                    release_state,
                    current_stage,
                    blocker,
                    pdf_links,
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
                    is_active
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    template.kit_name,
                    template.kit_order,
                    int(template.is_main_kit),
                    int(template.is_active),
                ),
            )

    @staticmethod
    def _ensure_truckkit_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"]).strip().lower()
            for row in connection.execute("PRAGMA table_info(TruckKit)").fetchall()
        }
        if "pdf_links" not in columns:
            connection.execute("ALTER TABLE TruckKit ADD COLUMN pdf_links TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _ensure_kittemplate_schema(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"]).strip().lower()
            for row in connection.execute("PRAGMA table_info(KitTemplate)").fetchall()
        }
        if "default_magnitude" not in columns:
            return

        connection.execute("DROP TABLE IF EXISTS KitTemplate_new")
        connection.execute(
            """
            CREATE TABLE KitTemplate_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kit_name TEXT NOT NULL,
                kit_order INTEGER NOT NULL,
                is_main_kit INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        connection.execute(
            """
            INSERT INTO KitTemplate_new (
                id,
                kit_name,
                kit_order,
                is_main_kit,
                is_active
            )
            SELECT
                id,
                kit_name,
                kit_order,
                is_main_kit,
                is_active
            FROM KitTemplate
            """
        )
        connection.execute("DROP TABLE KitTemplate")
        connection.execute("ALTER TABLE KitTemplate_new RENAME TO KitTemplate")

    @staticmethod
    def _ensure_truckkit_schema(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"]).strip().lower()
            for row in connection.execute("PRAGMA table_info(TruckKit)").fetchall()
        }
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'TruckKit'"
        ).fetchone()
        table_sql = str(row["sql"] or "").lower() if row else ""

        has_magnitude_column = "magnitude" in columns
        has_complete = "'complete'" in table_sql
        has_welded = "'welded'" in table_sql
        needs_rebuild = has_magnitude_column or has_welded or not has_complete
        if not needs_rebuild:
            return

        connection.execute("DROP TABLE IF EXISTS TruckKit_new")
        connection.execute(
            """
            CREATE TABLE TruckKit_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                truck_id INTEGER NOT NULL,
                kit_template_id INTEGER,
                parent_kit_id INTEGER,
                kit_name TEXT NOT NULL,
                kit_order INTEGER NOT NULL,
                is_main_kit INTEGER NOT NULL DEFAULT 0,
                release_state TEXT NOT NULL CHECK(release_state IN ('not_released', 'released')),
                current_stage TEXT NOT NULL CHECK(current_stage IN ('release', 'laser', 'bend', 'weld', 'complete')),
                blocker TEXT NOT NULL DEFAULT '',
                pdf_links TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(truck_id) REFERENCES Truck(id),
                FOREIGN KEY(kit_template_id) REFERENCES KitTemplate(id),
                FOREIGN KEY(parent_kit_id) REFERENCES TruckKit(id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO TruckKit_new (
                id,
                truck_id,
                kit_template_id,
                parent_kit_id,
                kit_name,
                kit_order,
                is_main_kit,
                release_state,
                current_stage,
                blocker,
                pdf_links,
                is_active,
                created_at,
                updated_at
            )
            SELECT
                id,
                truck_id,
                kit_template_id,
                parent_kit_id,
                kit_name,
                kit_order,
                is_main_kit,
                CASE
                    WHEN LOWER(TRIM(release_state)) = 'partial' THEN 'released'
                    WHEN LOWER(TRIM(release_state)) = 'released' THEN 'released'
                    ELSE 'not_released'
                END,
                CASE
                    WHEN LOWER(TRIM(current_stage)) = 'welded' THEN 'complete'
                    ELSE current_stage
                END,
                blocker,
                COALESCE(pdf_links, ''),
                is_active,
                created_at,
                updated_at
            FROM TruckKit
            """
        )
        connection.execute("DROP TABLE TruckKit")
        connection.execute("ALTER TABLE TruckKit_new RENAME TO TruckKit")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_truckkit_truck ON TruckKit(truck_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_truckkit_active ON TruckKit(is_active)")

    @staticmethod
    def _ensure_truck_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"]).strip().lower()
            for row in connection.execute("PRAGMA table_info(Truck)").fetchall()
        }
        if "build_order" not in columns:
            connection.execute("ALTER TABLE Truck ADD COLUMN build_order INTEGER NOT NULL DEFAULT 0")
        if "planned_start_date" not in columns:
            connection.execute("ALTER TABLE Truck ADD COLUMN planned_start_date TEXT NOT NULL DEFAULT ''")
        if "client" not in columns:
            connection.execute("ALTER TABLE Truck ADD COLUMN client TEXT NOT NULL DEFAULT ''")
        if "is_visible" not in columns:
            connection.execute("ALTER TABLE Truck ADD COLUMN is_visible INTEGER NOT NULL DEFAULT 1")

        rows = connection.execute(
            "SELECT id, build_order, planned_start_date, notes FROM Truck ORDER BY id"
        ).fetchall()
        existing_orders = [int(row["build_order"] or 0) for row in rows if int(row["build_order"] or 0) > 0]
        next_order = (max(existing_orders) + 1) if existing_orders else 1
        for row in rows:
            current = int(row["build_order"] or 0)
            if current > 0:
                new_order = current
            else:
                new_order = next_order
                next_order += 1

            normalized_date = FabricationDatabase._normalize_iso_date(row["planned_start_date"] or "")
            if not normalized_date:
                notes = str(row["notes"] or "")
                prefix = "Calendar Day Zero:"
                if notes.startswith(prefix):
                    normalized_date = FabricationDatabase._normalize_iso_date(
                        notes[len(prefix):].strip()
                    )

            connection.execute(
                """
                UPDATE Truck
                SET build_order = ?,
                    planned_start_date = ?
                WHERE id = ?
                """,
                (new_order, normalized_date, int(row["id"])),
            )

    @staticmethod
    def _normalize_release_states(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE TruckKit SET release_state = 'released' WHERE release_state = 'partial'"
        )

    @staticmethod
    def _normalize_stage_values(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE TruckKit SET current_stage = 'complete' WHERE LOWER(TRIM(current_stage)) = 'welded'"
        )

    @staticmethod
    def _normalize_release_state(release_state: str) -> str:
        clean_value = str(release_state or "").strip().lower()
        if clean_value == "partial":
            return "released"
        if clean_value not in {"not_released", "released"}:
            return "not_released"
        return clean_value

    @staticmethod
    def _normalize_iso_date(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return ""
        return parsed.strftime("%Y-%m-%d")

    def _connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _wipe_database_in_place(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DELETE FROM TruckKit")
            connection.execute("DELETE FROM Truck")
            connection.execute("DELETE FROM KitTemplate")
            connection.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('Truck', 'KitTemplate', 'TruckKit')"
            )
            connection.execute("PRAGMA foreign_keys = ON")
            self._ensure_default_templates(connection)
            self._ensure_truckkit_columns(connection)
            connection.commit()

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
            release_state=FabricationDatabase._normalize_release_state(row["release_state"]),
            current_stage=row["current_stage"],
            blocker=row["blocker"] or "",
            pdf_links=row["pdf_links"] or "",
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
