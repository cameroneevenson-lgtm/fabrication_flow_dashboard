from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from database import FabricationDatabase
from main_window import MainWindow
from sample_data import build_sample_trucks


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Fabrication Flow Dashboard")

    base_dir = Path(__file__).resolve().parent
    database = FabricationDatabase(base_dir / "fabrication_flow.db")
    database.initialize()

    # Sample-first startup: if the database is empty, seed a small in-code dataset.
    if not database.has_trucks():
        database.seed_sample_data(build_sample_trucks())

    window = MainWindow(database=database)
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
