from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from models import DEFAULT_KIT_TEMPLATES, Truck, TruckKit, canonicalize_kit_name, now_iso
from stages import (
    FABRICATION_ALLOWED_POSITIONS,
    FABRICATION_STAGE_POSITION_SCALE,
    STAGE_SEQUENCE,
    Stage,
    normalize_stage_span,
    stage_from_id,
)

VALID_STAGE_IDS_SQL = ", ".join(str(int(stage)) for stage in STAGE_SEQUENCE)
OVERLAY_ALLOWED_POSITIONS = FABRICATION_ALLOWED_POSITIONS
OVERLAY_ALLOWED_POSITIONS_SQL = ", ".join(str(value) for value in OVERLAY_ALLOWED_POSITIONS)


class FabricationDatabase:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            if not self._schema_is_current(connection):
                self._drop_schema(connection)
            self._create_schema(connection)
            self._ensure_overlay_columns(connection)
            self._ensure_default_templates(connection)
            self._rename_legacy_pack_names(connection)
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")

    def has_trucks(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM Truck").fetchone()
            return bool(row["count"])

    def wipe_database(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            self._drop_schema(connection)
            self._create_schema(connection)
            self._ensure_overlay_columns(connection)
            self._ensure_default_templates(connection)
            self._rename_legacy_pack_names(connection)
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")

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
                        released_at,
                        blocked,
                        blocked_reason,
                        front_stage_id,
                        back_stage_id,
                        front_position,
                        back_position,
                        keep_tail_at_head,
                        blocker,
                        pdf_links,
                        is_active,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, NULL, ?, ?, ?, 'not_released', '', 0, '', ?, ?, ?, ?, 1, '', '', 1, ?, ?)
                    """,
                    (
                        truck_id,
                        int(template["id"]),
                        template["kit_name"],
                        int(template["kit_order"]),
                        int(template["is_main_kit"]),
                        int(Stage.RELEASE),
                        int(Stage.RELEASE),
                        10,
                        10,
                        now_value,
                        now_value,
                    ),
                )

            connection.commit()
        return truck_id

    def sync_truck_registry(self, rows: list[dict[str, object]]) -> tuple[int, int]:
        if not rows:
            return (0, 0)

        now_value = now_iso()
        created_count = 0
        updated_count = 0
        with self._connect() as connection:
            existing_rows = connection.execute(
                """
                SELECT id, truck_number, notes, is_visible, planned_start_date, build_order
                FROM Truck
                """
            ).fetchall()
            by_truck_number = {
                str(row["truck_number"]).strip(): row
                for row in existing_rows
                if str(row["truck_number"]).strip()
            }
            max_build_order = max((int(row["build_order"] or 0) for row in existing_rows), default=0)
            templates = connection.execute(
                """
                SELECT id, kit_name, kit_order, is_main_kit
                FROM KitTemplate
                WHERE is_active = 1
                ORDER BY kit_order, id
                """
            ).fetchall()

            for row in rows:
                truck_number = str(row.get("truck_number") or "").strip()
                if not truck_number:
                    continue

                planned_start_date = self._normalize_iso_date(str(row.get("day_zero") or ""))
                notes = str(row.get("notes") or "").strip()
                is_visible = int(bool(row.get("is_active", True)))

                existing = by_truck_number.get(truck_number)
                if existing:
                    changed = (
                        str(existing["planned_start_date"] or "").strip() != planned_start_date
                        or str(existing["notes"] or "").strip() != notes
                        or int(existing["is_visible"] or 0) != is_visible
                    )
                    if not changed:
                        continue

                    connection.execute(
                        """
                        UPDATE Truck
                        SET planned_start_date = ?,
                            notes = ?,
                            is_visible = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            planned_start_date,
                            notes,
                            is_visible,
                            now_value,
                            int(existing["id"]),
                        ),
                    )
                    updated_count += 1
                    continue

                max_build_order += 1
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
                    VALUES (?, '', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        truck_number,
                        notes,
                        is_visible,
                        max_build_order,
                        planned_start_date,
                        now_value,
                        now_value,
                    ),
                )
                truck_id = int(truck_cursor.lastrowid)

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
                            released_at,
                            blocked,
                            blocked_reason,
                        front_stage_id,
                        back_stage_id,
                        front_position,
                        back_position,
                        keep_tail_at_head,
                        blocker,
                        pdf_links,
                        is_active,
                        created_at,
                        updated_at
                    )
                        VALUES (?, ?, NULL, ?, ?, ?, 'not_released', '', 0, '', ?, ?, ?, ?, 1, '', '', 1, ?, ?)
                        """,
                        (
                            truck_id,
                            int(template["id"]),
                            template["kit_name"],
                            int(template["kit_order"]),
                            int(template["is_main_kit"]),
                            int(Stage.RELEASE),
                            int(Stage.RELEASE),
                            10,
                            10,
                            now_value,
                            now_value,
                        ),
                    )

                created_count += 1

            connection.commit()

        return (created_count, updated_count)

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
                    released_at,
                    blocked,
                    blocked_reason,
                    front_stage_id,
                    back_stage_id,
                    front_position,
                    back_position,
                    keep_tail_at_head,
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
        front_stage_id: int,
        back_stage_id: int,
        blocker: str,
        is_active: bool,
        pdf_links: str | None = None,
        *,
        released_at: str | None = None,
        blocked: bool | None = None,
        blocked_reason: str | None = None,
        front_position: int | None = None,
        back_position: int | None = None,
        keep_tail_at_head: bool | None = None,
    ) -> None:
        normalized_release_state = self._normalize_release_state(release_state)
        normalized_front, normalized_back = normalize_stage_span(
            front_stage_id=front_stage_id,
            back_stage_id=back_stage_id,
        )
        if normalized_release_state == "not_released" and normalized_front > int(Stage.RELEASE):
            normalized_release_state = "released"

        normalized_blocked, normalized_blocked_reason = self._normalize_blocked_state(
            blocked=blocked,
            blocked_reason=blocked_reason,
            blocker=blocker,
        )

        now_value = now_iso()
        with self._connect() as connection:
            kit_row = connection.execute(
                """
                SELECT
                    truck_id,
                    released_at,
                    front_position,
                    back_position,
                    keep_tail_at_head
                FROM TruckKit
                WHERE id = ?
                """,
                (kit_id,),
            ).fetchone()
            if not kit_row:
                return
            effective_front_position = front_position
            effective_back_position = back_position
            if effective_front_position is None:
                effective_front_position = kit_row["front_position"]
            if effective_back_position is None:
                effective_back_position = kit_row["back_position"]
            normalized_keep_tail_at_head = (
                bool(kit_row["keep_tail_at_head"])
                if keep_tail_at_head is None
                else bool(keep_tail_at_head)
            )
            normalized_front_position, normalized_back_position = self._normalize_position_span(
                front_position=effective_front_position,
                back_position=effective_back_position,
                front_stage_id=normalized_front,
                back_stage_id=normalized_back,
            )
            if normalized_keep_tail_at_head:
                normalized_back = normalized_front
                normalized_back_position = normalized_front_position
            current_released_at = str(kit_row["released_at"] or "").strip()
            if normalized_release_state == "released":
                if released_at is None:
                    normalized_released_at = self._normalize_iso_date(current_released_at)
                else:
                    normalized_released_at = self._normalize_iso_date(str(released_at))
            else:
                normalized_released_at = ""

            connection.execute(
                """
                UPDATE TruckKit
                SET release_state = ?,
                    released_at = ?,
                    blocked = ?,
                    blocked_reason = ?,
                    front_stage_id = ?,
                    back_stage_id = ?,
                    front_position = ?,
                    back_position = ?,
                    keep_tail_at_head = ?,
                    blocker = ?,
                    pdf_links = COALESCE(?, pdf_links),
                    is_active = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized_release_state,
                    normalized_released_at,
                    int(normalized_blocked),
                    normalized_blocked_reason,
                    normalized_front,
                    normalized_back,
                    normalized_front_position,
                    normalized_back_position,
                    int(normalized_keep_tail_at_head),
                    normalized_blocked_reason,
                    pdf_links.strip() if isinstance(pdf_links, str) else None,
                    int(is_active),
                    now_value,
                    int(kit_id),
                ),
            )

            connection.execute(
                "UPDATE Truck SET updated_at = ? WHERE id = ?",
                (now_value, int(kit_row["truck_id"])),
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
                    released_at,
                    blocked,
                    blocked_reason,
                    front_stage_id,
                    back_stage_id,
                    front_position,
                    back_position,
                    keep_tail_at_head,
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

    def _schema_is_current(self, connection: sqlite3.Connection) -> bool:
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        table_names = {str(row["name"]) for row in table_rows}
        required_tables = {"Truck", "KitTemplate", "TruckKit"}
        if not required_tables.issubset(table_names):
            return False

        truckkit_columns = {
            str(row["name"]).strip().lower()
            for row in connection.execute("PRAGMA table_info(TruckKit)").fetchall()
        }
        required_truckkit_columns = {
            "id",
            "truck_id",
            "kit_template_id",
            "parent_kit_id",
            "kit_name",
            "kit_order",
            "is_main_kit",
            "release_state",
            "front_stage_id",
            "back_stage_id",
            "blocker",
            "pdf_links",
            "is_active",
            "created_at",
            "updated_at",
        }
        if not required_truckkit_columns.issubset(truckkit_columns):
            return False
        if "current_stage" in truckkit_columns:
            return False

        return True

    @staticmethod
    def _drop_schema(connection: sqlite3.Connection) -> None:
        connection.execute("DROP TABLE IF EXISTS TruckKit")
        connection.execute("DROP TABLE IF EXISTS Truck")
        connection.execute("DROP TABLE IF EXISTS KitTemplate")

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            f"""
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
                released_at TEXT NOT NULL DEFAULT '',
                blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0, 1)),
                blocked_reason TEXT NOT NULL DEFAULT '',
                front_stage_id INTEGER NOT NULL CHECK(front_stage_id IN ({VALID_STAGE_IDS_SQL})),
                back_stage_id INTEGER NOT NULL CHECK(back_stage_id IN ({VALID_STAGE_IDS_SQL})),
                front_position INTEGER NOT NULL DEFAULT 10 CHECK(front_position IN ({OVERLAY_ALLOWED_POSITIONS_SQL})),
                back_position INTEGER NOT NULL DEFAULT 10 CHECK(back_position IN ({OVERLAY_ALLOWED_POSITIONS_SQL})),
                keep_tail_at_head INTEGER NOT NULL DEFAULT 1 CHECK(keep_tail_at_head IN (0, 1)),
                blocker TEXT NOT NULL DEFAULT '',
                pdf_links TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(front_stage_id >= back_stage_id),
                CHECK(front_position >= back_position),
                CHECK((blocked = 0 AND TRIM(blocked_reason) = '') OR blocked = 1),
                FOREIGN KEY(truck_id) REFERENCES Truck(id),
                FOREIGN KEY(kit_template_id) REFERENCES KitTemplate(id),
                FOREIGN KEY(parent_kit_id) REFERENCES TruckKit(id)
            );

            CREATE INDEX IF NOT EXISTS idx_truckkit_truck ON TruckKit(truck_id);
            CREATE INDEX IF NOT EXISTS idx_truckkit_active ON TruckKit(is_active);
            """
        )

    @staticmethod
    def _truckkit_position_scale_is_current(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'TruckKit'"
        ).fetchone()
        if not row:
            return False
        table_sql = str(row["sql"] or "")
        required_markers = ("front_position IN", "back_position IN", "12", "14", "16", "22", "24", "26", "32", "34", "36")
        return all(marker in table_sql for marker in required_markers)

    @staticmethod
    def _rebuild_truckkit_for_position_scale(connection: sqlite3.Connection) -> None:
        connection.executescript(
            f"""
            CREATE TABLE TruckKit_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                truck_id INTEGER NOT NULL,
                kit_template_id INTEGER,
                parent_kit_id INTEGER,
                kit_name TEXT NOT NULL,
                kit_order INTEGER NOT NULL,
                is_main_kit INTEGER NOT NULL DEFAULT 0,
                release_state TEXT NOT NULL CHECK(release_state IN ('not_released', 'released')),
                released_at TEXT NOT NULL DEFAULT '',
                blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0, 1)),
                blocked_reason TEXT NOT NULL DEFAULT '',
                front_stage_id INTEGER NOT NULL CHECK(front_stage_id IN ({VALID_STAGE_IDS_SQL})),
                back_stage_id INTEGER NOT NULL CHECK(back_stage_id IN ({VALID_STAGE_IDS_SQL})),
                front_position INTEGER NOT NULL DEFAULT 10 CHECK(front_position IN ({OVERLAY_ALLOWED_POSITIONS_SQL})),
                back_position INTEGER NOT NULL DEFAULT 10 CHECK(back_position IN ({OVERLAY_ALLOWED_POSITIONS_SQL})),
                keep_tail_at_head INTEGER NOT NULL DEFAULT 1 CHECK(keep_tail_at_head IN (0, 1)),
                blocker TEXT NOT NULL DEFAULT '',
                pdf_links TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(front_stage_id >= back_stage_id),
                CHECK(front_position >= back_position),
                CHECK((blocked = 0 AND TRIM(blocked_reason) = '') OR blocked = 1),
                FOREIGN KEY(truck_id) REFERENCES Truck(id),
                FOREIGN KEY(kit_template_id) REFERENCES KitTemplate(id),
                FOREIGN KEY(parent_kit_id) REFERENCES TruckKit(id)
            );

            INSERT INTO TruckKit_new (
                id, truck_id, kit_template_id, parent_kit_id, kit_name, kit_order, is_main_kit,
                release_state, released_at, blocked, blocked_reason, front_stage_id, back_stage_id,
                front_position, back_position, keep_tail_at_head, blocker, pdf_links, is_active, created_at, updated_at
            )
            SELECT
                id,
                truck_id,
                kit_template_id,
                parent_kit_id,
                kit_name,
                kit_order,
                is_main_kit,
                release_state,
                released_at,
                blocked,
                blocked_reason,
                front_stage_id,
                back_stage_id,
                CASE
                    WHEN mapped_front < mapped_back THEN mapped_back
                    ELSE mapped_front
                END AS front_position,
                mapped_back AS back_position,
                CASE
                    WHEN front_stage_id = back_stage_id AND mapped_front = mapped_back THEN 1
                    ELSE 0
                END AS keep_tail_at_head,
                blocker,
                pdf_links,
                is_active,
                created_at,
                updated_at
            FROM (
                SELECT
                    *,
                    CASE
                        WHEN front_position IN ({OVERLAY_ALLOWED_POSITIONS_SQL}) THEN front_position
                        WHEN front_position = 35 THEN 34
                        WHEN front_position = 25 THEN 24
                        WHEN front_position = 15 THEN 14
                        WHEN front_stage_id >= {int(Stage.WELD)} THEN 34
                        WHEN front_stage_id = {int(Stage.BEND)} THEN 24
                        WHEN front_stage_id = {int(Stage.LASER)} THEN 14
                        ELSE 10
                    END AS mapped_front,
                    CASE
                        WHEN back_position IN ({OVERLAY_ALLOWED_POSITIONS_SQL}) THEN back_position
                        WHEN back_position = 35 THEN 34
                        WHEN back_position = 25 THEN 24
                        WHEN back_position = 15 THEN 14
                        WHEN back_stage_id = front_stage_id THEN
                            CASE
                                WHEN front_position IN ({OVERLAY_ALLOWED_POSITIONS_SQL}) THEN front_position
                                WHEN front_position = 35 THEN 34
                                WHEN front_position = 25 THEN 24
                                WHEN front_position = 15 THEN 14
                                WHEN front_stage_id >= {int(Stage.WELD)} THEN 34
                                WHEN front_stage_id = {int(Stage.BEND)} THEN 24
                                WHEN front_stage_id = {int(Stage.LASER)} THEN 14
                                ELSE 10
                            END
                        WHEN back_stage_id >= {int(Stage.WELD)} THEN 30
                        WHEN back_stage_id = {int(Stage.BEND)} THEN 20
                        ELSE 10
                    END AS mapped_back
                FROM TruckKit
            ) staged;

            DROP TABLE TruckKit;
            ALTER TABLE TruckKit_new RENAME TO TruckKit;

            CREATE INDEX IF NOT EXISTS idx_truckkit_truck ON TruckKit(truck_id);
            CREATE INDEX IF NOT EXISTS idx_truckkit_active ON TruckKit(is_active);
            """
        )

    @staticmethod
    def _ensure_overlay_columns(connection: sqlite3.Connection) -> None:
        existing_columns = {
            str(row["name"]).strip().lower()
            for row in connection.execute("PRAGMA table_info(TruckKit)").fetchall()
        }
        added_keep_tail_column = False

        if "released_at" not in existing_columns:
            connection.execute("ALTER TABLE TruckKit ADD COLUMN released_at TEXT NOT NULL DEFAULT ''")
        if "blocked" not in existing_columns:
            connection.execute("ALTER TABLE TruckKit ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        if "blocked_reason" not in existing_columns:
            connection.execute("ALTER TABLE TruckKit ADD COLUMN blocked_reason TEXT NOT NULL DEFAULT ''")
        if "front_position" not in existing_columns:
            connection.execute("ALTER TABLE TruckKit ADD COLUMN front_position INTEGER NOT NULL DEFAULT 10")
        if "back_position" not in existing_columns:
            connection.execute("ALTER TABLE TruckKit ADD COLUMN back_position INTEGER NOT NULL DEFAULT 10")
        if "keep_tail_at_head" not in existing_columns:
            connection.execute("ALTER TABLE TruckKit ADD COLUMN keep_tail_at_head INTEGER NOT NULL DEFAULT 1")
            added_keep_tail_column = True
        if not FabricationDatabase._truckkit_position_scale_is_current(connection):
            FabricationDatabase._rebuild_truckkit_for_position_scale(connection)

        # Keep old blocker text and new blocked fields aligned so existing records migrate cleanly.
        connection.execute(
            """
            UPDATE TruckKit
            SET blocked = CASE
                WHEN TRIM(COALESCE(blocked_reason, '')) <> '' THEN 1
                WHEN TRIM(COALESCE(blocker, '')) <> '' THEN 1
                ELSE 0
            END
            """
        )
        connection.execute(
            """
            UPDATE TruckKit
            SET blocked_reason = CASE
                WHEN blocked = 0 THEN ''
                WHEN TRIM(COALESCE(blocked_reason, '')) <> '' THEN TRIM(blocked_reason)
                WHEN TRIM(COALESCE(blocker, '')) <> '' THEN TRIM(blocker)
                ELSE 'Blocked'
            END
            """
        )
        connection.execute(
            """
            UPDATE TruckKit
            SET blocker = CASE
                WHEN blocked = 0 THEN ''
                WHEN TRIM(COALESCE(blocked_reason, '')) <> '' THEN TRIM(blocked_reason)
                WHEN TRIM(COALESCE(blocker, '')) <> '' THEN TRIM(blocker)
                ELSE 'Blocked'
            END
            """
        )
        connection.execute(
            f"""
            UPDATE TruckKit
            SET front_position = CASE
                WHEN front_stage_id >= {int(Stage.WELD)} AND front_position IN (30, 32, 34, 36, 38) THEN front_position
                WHEN front_stage_id = {int(Stage.BEND)} AND front_position IN (20, 22, 24, 26, 28) THEN front_position
                WHEN front_stage_id = {int(Stage.LASER)} AND front_position IN (10, 12, 14, 16, 18) THEN front_position
                WHEN front_stage_id = {int(Stage.RELEASE)} AND front_position = 10 THEN front_position
                WHEN front_stage_id >= {int(Stage.WELD)} THEN 34
                WHEN front_stage_id = {int(Stage.BEND)} THEN 24
                WHEN front_stage_id = {int(Stage.LASER)} THEN 14
                ELSE 10
            END
            """
        )
        connection.execute(
            f"""
            UPDATE TruckKit
            SET back_position = CASE
                WHEN back_stage_id >= {int(Stage.WELD)} AND back_position IN (30, 32, 34, 36, 38) THEN back_position
                WHEN back_stage_id = {int(Stage.BEND)} AND back_position IN (20, 22, 24, 26, 28) THEN back_position
                WHEN back_stage_id = {int(Stage.LASER)} AND back_position IN (10, 12, 14, 16, 18) THEN back_position
                WHEN back_stage_id = {int(Stage.RELEASE)} AND back_position = 10 THEN back_position
                WHEN back_stage_id = front_stage_id THEN front_position
                WHEN back_stage_id >= {int(Stage.WELD)} THEN 30
                WHEN back_stage_id = {int(Stage.BEND)} THEN 20
                ELSE 10
            END
            """
        )
        connection.execute(
            """
            UPDATE TruckKit
            SET front_position = CASE
                WHEN front_position < back_position THEN back_position
                ELSE front_position
            END
            """
        )
        connection.execute(
            """
            UPDATE TruckKit
            SET released_at = CASE
                WHEN release_state = 'released' THEN TRIM(COALESCE(released_at, ''))
                ELSE ''
            END
            """
        )
        if added_keep_tail_column:
            connection.execute(
                """
                UPDATE TruckKit
                SET keep_tail_at_head = CASE
                    WHEN front_stage_id = back_stage_id AND front_position = back_position THEN 1
                    ELSE 0
                END
                """
            )
        connection.execute(
            """
            UPDATE TruckKit
            SET keep_tail_at_head = CASE
                WHEN keep_tail_at_head NOT IN (0, 1) THEN 1
                ELSE keep_tail_at_head
            END
            """
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
    def _rename_legacy_pack_names(connection: sqlite3.Connection) -> None:
        rename_pairs = (
            ("Console Pack", "Console"),
            ("Interior Pack", "Interior"),
            ("Exterior Pack", "Exterior"),
        )
        for old_name, new_name in rename_pairs:
            connection.execute(
                "UPDATE KitTemplate SET kit_name = ? WHERE kit_name = ?",
                (new_name, old_name),
            )
            connection.execute(
                "UPDATE TruckKit SET kit_name = ? WHERE kit_name = ?",
                (new_name, old_name),
            )

    @staticmethod
    def _normalize_release_state(release_state: str) -> str:
        clean_value = str(release_state or "").strip().lower()
        if clean_value not in {"not_released", "released"}:
            return "not_released"
        return clean_value

    @staticmethod
    def _normalize_blocked_state(
        *,
        blocked: bool | None,
        blocked_reason: str | None,
        blocker: str | None,
    ) -> tuple[bool, str]:
        blocker_text = str(blocker or "").strip()
        reason_text = str(blocked_reason or "").strip()
        normalized_blocked = bool(blocked) if blocked is not None else bool(reason_text or blocker_text)
        if not normalized_blocked:
            return (False, "")
        normalized_reason = reason_text or blocker_text or "Blocked"
        return (True, normalized_reason)

    @staticmethod
    def _normalize_position_value(value: int | None) -> int | None:
        if value is None:
            return None
        try:
            clean = int(value)
        except (TypeError, ValueError):
            return None
        if clean not in OVERLAY_ALLOWED_POSITIONS:
            return None
        return clean

    @staticmethod
    def _position_matches_stage(position: int, stage_id: int | Stage | None) -> bool:
        stage = stage_from_id(stage_id)
        if stage == Stage.RELEASE:
            return int(position) == 10
        if stage >= Stage.WELD:
            return int(position) in FABRICATION_STAGE_POSITION_SCALE[Stage.WELD]
        stage_positions = FABRICATION_STAGE_POSITION_SCALE.get(stage)
        if stage_positions is None:
            return False
        return int(position) in stage_positions

    @staticmethod
    def _default_front_position_for_stage(stage_id: int | Stage | None) -> int:
        return FabricationDatabase._entry_position_for_stage(stage_id)

    @staticmethod
    def _default_back_position_for_stage(stage_id: int | Stage | None) -> int:
        stage = stage_from_id(stage_id)
        if stage >= Stage.WELD:
            return 30
        if stage == Stage.BEND:
            return 20
        return 10

    @classmethod
    def _entry_position_for_stage(cls, stage_id: int | Stage | None) -> int:
        return cls._default_back_position_for_stage(stage_id)

    @classmethod
    def _normalize_position_span(
        cls,
        *,
        front_position: int | None,
        back_position: int | None,
        front_stage_id: int | Stage | None,
        back_stage_id: int | Stage | None,
    ) -> tuple[int, int]:
        normalized_front = cls._normalize_position_value(front_position)
        normalized_back = cls._normalize_position_value(back_position)
        front_stage = stage_from_id(front_stage_id)
        back_stage = stage_from_id(back_stage_id)

        if normalized_front is None or not cls._position_matches_stage(normalized_front, front_stage):
            normalized_front = cls._default_front_position_for_stage(front_stage_id)
        if normalized_back is None or not cls._position_matches_stage(normalized_back, back_stage):
            if back_stage == front_stage:
                normalized_back = normalized_front
            else:
                normalized_back = cls._default_back_position_for_stage(back_stage)

        if normalized_front < normalized_back:
            normalized_front = normalized_back
        return (normalized_front, normalized_back)

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

    @classmethod
    def _row_to_kit(cls, row: sqlite3.Row) -> TruckKit:
        front_stage = stage_from_id(row["front_stage_id"])
        back_stage = stage_from_id(row["back_stage_id"])
        front_stage_id, back_stage_id = normalize_stage_span(
            front_stage_id=front_stage,
            back_stage_id=back_stage,
        )

        release_state = cls._normalize_release_state(row["release_state"])
        if release_state == "not_released" and front_stage_id > int(Stage.RELEASE):
            release_state = "released"

        row_columns = {str(key).strip().lower() for key in row.keys()}
        blocked_flag = bool(row["blocked"]) if "blocked" in row_columns else None
        blocked_reason_raw = row["blocked_reason"] if "blocked_reason" in row_columns else row["blocker"]
        blocked, blocked_reason = cls._normalize_blocked_state(
            blocked=blocked_flag,
            blocked_reason=str(blocked_reason_raw or ""),
            blocker=str(row["blocker"] or ""),
        )
        front_position, back_position = cls._normalize_position_span(
            front_position=row["front_position"] if "front_position" in row_columns else None,
            back_position=row["back_position"] if "back_position" in row_columns else None,
            front_stage_id=front_stage_id,
            back_stage_id=back_stage_id,
        )
        keep_tail_at_head = (
            bool(row["keep_tail_at_head"])
            if "keep_tail_at_head" in row_columns
            else (front_stage_id == back_stage_id and front_position == back_position)
        )
        released_at = cls._normalize_iso_date(row["released_at"] if "released_at" in row_columns else "")
        if release_state != "released":
            released_at = ""

        return TruckKit(
            id=int(row["id"]),
            truck_id=int(row["truck_id"]),
            kit_template_id=int(row["kit_template_id"]) if row["kit_template_id"] is not None else None,
            parent_kit_id=int(row["parent_kit_id"]) if row["parent_kit_id"] is not None else None,
            kit_name=canonicalize_kit_name(str(row["kit_name"] or "")),
            kit_order=int(row["kit_order"]),
            is_main_kit=bool(row["is_main_kit"]),
            release_state=release_state,
            released_at=released_at,
            blocked=blocked,
            blocked_reason=blocked_reason,
            front_stage_id=front_stage_id,
            back_stage_id=back_stage_id,
            front_position=front_position,
            back_position=back_position,
            keep_tail_at_head=keep_tail_at_head,
            blocker=blocked_reason if blocked else "",
            pdf_links=row["pdf_links"] or "",
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
