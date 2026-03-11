from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from stages import Stage

RELEASE_STATES = ["not_released", "released"]


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
    front_stage_id: int = int(Stage.RELEASE)
    back_stage_id: int = int(Stage.RELEASE)
    blocker: str = ""
    pdf_links: str = ""
    is_active: bool = True
    created_at: str = ""
    updated_at: str = ""


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
        kit_name="Console Pack",
        kit_order=3,
        is_main_kit=False,
        is_active=True,
    ),
    KitTemplate(
        id=None,
        kit_name="Interior Pack",
        kit_order=4,
        is_main_kit=False,
        is_active=True,
    ),
    KitTemplate(
        id=None,
        kit_name="Exterior Pack",
        kit_order=5,
        is_main_kit=False,
        is_active=True,
    ),
]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
