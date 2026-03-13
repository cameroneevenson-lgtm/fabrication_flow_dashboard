from __future__ import annotations

from enum import IntEnum


class Stage(IntEnum):
    RELEASE = 10
    LASER = 20
    BEND = 30
    WELD = 40
    COMPLETE = 50


class FabricationPosition(IntEnum):
    LASER_10 = 10
    LASER_12 = 12
    LASER_14 = 14
    LASER_16 = 16
    LASER_18 = 18
    BEND_20 = 20
    BEND_22 = 22
    BEND_24 = 24
    BEND_26 = 26
    BEND_28 = 28
    WELD_30 = 30
    WELD_32 = 32
    WELD_34 = 34
    WELD_36 = 36
    WELD_38 = 38


STAGE_INFO: dict[Stage, dict[str, str]] = {
    Stage.RELEASE: {"key": "release", "label": "Release"},
    Stage.LASER: {"key": "laser", "label": "Laser"},
    Stage.BEND: {"key": "bend", "label": "Bend"},
    Stage.WELD: {"key": "weld", "label": "Weld"},
    Stage.COMPLETE: {"key": "complete", "label": "Complete"},
}

STAGE_SEQUENCE: tuple[Stage, ...] = (
    Stage.RELEASE,
    Stage.LASER,
    Stage.BEND,
    Stage.WELD,
    Stage.COMPLETE,
)

STAGE_KEY_TO_STAGE: dict[str, Stage] = {
    values["key"]: stage for stage, values in STAGE_INFO.items()
}

UPSTREAM_STAGES: tuple[Stage, ...] = (Stage.RELEASE, Stage.LASER, Stage.BEND)
FABRICATION_STAGES: tuple[Stage, ...] = (Stage.LASER, Stage.BEND, Stage.WELD)
FABRICATION_STAGE_POSITION_SCALE: dict[Stage, tuple[int, ...]] = {
    Stage.LASER: (
        int(FabricationPosition.LASER_10),
        int(FabricationPosition.LASER_12),
        int(FabricationPosition.LASER_14),
        int(FabricationPosition.LASER_16),
        int(FabricationPosition.LASER_18),
    ),
    Stage.BEND: (
        int(FabricationPosition.BEND_20),
        int(FabricationPosition.BEND_22),
        int(FabricationPosition.BEND_24),
        int(FabricationPosition.BEND_26),
        int(FabricationPosition.BEND_28),
    ),
    Stage.WELD: (
        int(FabricationPosition.WELD_30),
        int(FabricationPosition.WELD_32),
        int(FabricationPosition.WELD_34),
        int(FabricationPosition.WELD_36),
        int(FabricationPosition.WELD_38),
    ),
}
FABRICATION_ALLOWED_POSITIONS: tuple[int, ...] = (
    FABRICATION_STAGE_POSITION_SCALE[Stage.LASER]
    + FABRICATION_STAGE_POSITION_SCALE[Stage.BEND]
    + FABRICATION_STAGE_POSITION_SCALE[Stage.WELD]
)


def stage_from_id(value: int | Stage | None, fallback: Stage = Stage.RELEASE) -> Stage:
    try:
        return Stage(int(value))
    except (TypeError, ValueError):
        return fallback


def stage_from_key(value: object) -> Stage | None:
    key = str(value or "").strip().lower()
    if not key:
        return None
    return STAGE_KEY_TO_STAGE.get(key)


def stage_label(value: int | Stage | None) -> str:
    stage = stage_from_id(value)
    return STAGE_INFO[stage]["label"]


def stage_key(value: int | Stage | None) -> str:
    stage = stage_from_id(value)
    return STAGE_INFO[stage]["key"]


def stage_options(stages: tuple[Stage, ...] | None = None) -> list[tuple[int, str]]:
    ordered = stages or STAGE_SEQUENCE
    return [(int(stage), STAGE_INFO[stage]["label"]) for stage in ordered]


def normalize_stage_span(front_stage_id: int | Stage | None, back_stage_id: int | Stage | None) -> tuple[int, int]:
    front = stage_from_id(front_stage_id)
    back = stage_from_id(back_stage_id)

    if front == Stage.COMPLETE:
        return (int(Stage.COMPLETE), int(Stage.COMPLETE))

    if back > front:
        back = front
    return (int(front), int(back))
