from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from database import FabricationDatabase
from main_window import MainWindow


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

    window = MainWindow(database=database)
    window.show()
    _place_window_on_second_screen(app, window)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
