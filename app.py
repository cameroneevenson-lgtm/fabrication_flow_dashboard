from __future__ import annotations

import sys
import os
import ctypes
from pathlib import Path

from PySide6.QtCore import QTimer
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
    window.setFixedSize(geometry.size())
    window.move(geometry.topLeft())


def _bring_window_to_front(window: MainWindow) -> None:
    window.raise_()
    window.activateWindow()
    try:
        hwnd = int(window.winId())
        if hwnd:
            user32 = ctypes.windll.user32
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
    except Exception:
        # Best-effort focus; Qt raise/activate above is still applied.
        pass


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
    _bring_window_to_front(window)
    QTimer.singleShot(120, lambda: _bring_window_to_front(window))

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
