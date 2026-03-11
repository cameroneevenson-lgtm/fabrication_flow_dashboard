from __future__ import annotations

import sys
import os
from pathlib import Path

from PySide6.QtWidgets import QApplication

from database import FabricationDatabase
from main_window import MainWindow
from truck_registry import CSV_FILENAME, sync_truck_registry


def _place_window_on_second_screen(app: QApplication, window: MainWindow) -> None:
    screens = app.screens()
    if not screens:
        return

    target_screen = screens[1] if len(screens) > 1 else screens[0]
    handle = window.windowHandle()
    if handle is not None:
        handle.setScreen(target_screen)

    geometry = target_screen.availableGeometry()
    width = min(window.width(), geometry.width())
    height = min(window.height(), geometry.height())
    window.resize(width, height)

    x = geometry.x() + max(0, (geometry.width() - width) // 2)
    y = geometry.y() + max(0, (geometry.height() - height) // 2)
    window.move(x, y)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Fabrication Flow Dashboard")

    base_dir = Path(__file__).resolve().parent
    database = FabricationDatabase(base_dir / "fabrication_flow.db")
    database.initialize()
    try:
        sync_truck_registry(database=database, csv_path=base_dir / CSV_FILENAME)
    except (OSError, ValueError) as exc:
        print(f"Truck registry sync skipped: {exc}")

    hot_reload_active = os.environ.get("FFD_HOT_RELOAD_ACTIVE") == "1"
    window = MainWindow(
        database=database,
        hot_reload_active=hot_reload_active,
        runtime_dir=base_dir,
    )
    window.show()
    _place_window_on_second_screen(app, window)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
