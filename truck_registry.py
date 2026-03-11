from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from database import FabricationDatabase

CSV_FILENAME = "truck_registry.csv"
REQUIRED_COLUMNS = ("truck_number", "day_zero", "is_active", "notes")


@dataclass
class RegistrySyncResult:
    created_count: int
    updated_count: int
    row_count: int


def ensure_truck_registry_csv(path: Path) -> Path:
    if path.exists():
        return path

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(REQUIRED_COLUMNS))
        writer.writeheader()
    return path


def _parse_bool(value: object) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return text in {"1", "true", "yes", "y", "on"}


def load_truck_registry_rows(path: Path) -> list[dict[str, object]]:
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        fieldnames = [str(name or "").strip() for name in (reader.fieldnames or [])]
        missing = [column for column in REQUIRED_COLUMNS if column not in fieldnames]
        if missing:
            raise ValueError(
                "Truck registry CSV is missing required column(s): " + ", ".join(missing)
            )

        rows: list[dict[str, object]] = []
        for raw in reader:
            truck_number = str(raw.get("truck_number") or "").strip()
            if not truck_number:
                continue
            rows.append(
                {
                    "truck_number": truck_number,
                    "day_zero": str(raw.get("day_zero") or "").strip(),
                    "is_active": _parse_bool(raw.get("is_active")),
                    "notes": str(raw.get("notes") or "").strip(),
                }
            )
    return rows


def sync_truck_registry(database: FabricationDatabase, csv_path: Path) -> RegistrySyncResult:
    ensure_truck_registry_csv(csv_path)
    rows = load_truck_registry_rows(csv_path)
    created_count, updated_count = database.sync_truck_registry(rows)
    return RegistrySyncResult(
        created_count=created_count,
        updated_count=updated_count,
        row_count=len(rows),
    )
