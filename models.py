from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from stages import Stage

RELEASE_STATES = ["not_released", "released"]
KIT_NAME_CANONICAL_BY_LOWER = {
    "body": "Body",
    "pumphouse": "Pumphouse",
    "console": "Console",
    "console pack": "Console",
    "interior": "Interior",
    "interior pack": "Interior",
    "exterior": "Exterior",
    "exterior pack": "Exterior",
}


@dataclass
class Truck:
    id: Optional[int]
    truck_number: str
    client: str = ""
    notes: str = ""
    is_visible: bool = True
    build_order: int = 0
    planned_start_date: str = ""
    created_at: str = ""
    updated_at: str = ""
    kits: list["TruckKit"] = field(default_factory=list)


@dataclass
class KitTemplate:
    id: Optional[int]
    kit_name: str
    kit_order: int
    is_main_kit: bool
    is_active: bool = True


@dataclass
class TruckKit:
    id: Optional[int]
    truck_id: Optional[int]
    kit_template_id: Optional[int]
    parent_kit_id: Optional[int]
    kit_name: str
    kit_order: int
    is_main_kit: bool
    release_state: str = "not_released"
    released_at: str = ""
    blocked: bool = False
    blocked_reason: str = ""
    front_stage_id: int = int(Stage.RELEASE)
    back_stage_id: int = int(Stage.RELEASE)
    front_position: int = 10
    back_position: int = 10
    blocker: str = ""
    pdf_links: str = ""
    is_active: bool = True
    created_at: str = ""
    updated_at: str = ""


def canonicalize_kit_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return KIT_NAME_CANONICAL_BY_LOWER.get(text.lower(), text)


def first_pdf_link(raw_text: str) -> str:
    for part in str(raw_text).replace(";", "\n").splitlines():
        clean = part.strip().strip('"')
        if clean:
            return clean
    return ""


DEFAULT_KIT_TEMPLATES = [
    KitTemplate(
        id=None,
        kit_name="Body",
        kit_order=1,
        is_main_kit=True,
        is_active=True,
    ),
    KitTemplate(
        id=None,
        kit_name="Pumphouse",
        kit_order=2,
        is_main_kit=False,
        is_active=True,
    ),
    KitTemplate(
        id=None,
        kit_name="Console",
        kit_order=3,
        is_main_kit=False,
        is_active=True,
    ),
    KitTemplate(
        id=None,
        kit_name="Interior",
        kit_order=4,
        is_main_kit=False,
        is_active=True,
    ),
    KitTemplate(
        id=None,
        kit_name="Exterior",
        kit_order=5,
        is_main_kit=False,
        is_active=True,
    ),
]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
