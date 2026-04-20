from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from database import FabricationDatabase
from main_window import MainWindow
from truck_registry import CSV_FILENAME, sync_truck_registry


def place_window_on_preferred_screen(app: QApplication, window: MainWindow) -> None:
    screens = app.screens()
    if not screens:
        return

    target_screen = screens[1] if len(screens) > 1 else screens[0]
    handle = window.windowHandle()
    if handle is not None:
        handle.setScreen(target_screen)

    geometry = target_screen.availableGeometry()
    window.setFixedSize(geometry.size())
    window.move(geometry.topLeft())


def bring_window_to_front(window: MainWindow) -> None:
    window.raise_()
    window.activateWindow()
    try:
        hwnd = int(window.winId())
        if hwnd:
            user32 = ctypes.windll.user32
            sw_restore = 9
            user32.ShowWindow(hwnd, sw_restore)
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def build_database(base_dir: Path) -> FabricationDatabase:
    database = FabricationDatabase(base_dir / "fabrication_flow.db")
    database.initialize()
    try:
        sync_truck_registry(database=database, csv_path=base_dir / CSV_FILENAME)
    except (OSError, ValueError) as exc:
        print(f"Truck registry sync skipped: {exc}")
    return database


def build_main_window(base_dir: Path, database: FabricationDatabase) -> MainWindow:
    return MainWindow(
        database=database,
        hot_reload_active=os.environ.get("FFD_HOT_RELOAD_ACTIVE") == "1",
        runtime_dir=base_dir,
    )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Fabrication Flow Dashboard")

    base_dir = Path(__file__).resolve().parent
    database = build_database(base_dir)
    window = build_main_window(base_dir, database)
    window.show()
    place_window_on_preferred_screen(app, window)
    bring_window_to_front(window)
    QTimer.singleShot(120, lambda: bring_window_to_front(window))

    return app.exec()
