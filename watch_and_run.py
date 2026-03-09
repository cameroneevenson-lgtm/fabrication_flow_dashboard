from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

POLL_INTERVAL_SECONDS = 1.0
EXCLUDED_DIRS = {".git", ".venv", "__pycache__"}


def iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        yield path


def snapshot_files(root: Path) -> dict[Path, float]:
    state: dict[Path, float] = {}
    for path in iter_python_files(root):
        try:
            state[path] = path.stat().st_mtime
        except OSError:
            continue
    return state


def changed_files(previous: dict[Path, float], current: dict[Path, float]) -> list[Path]:
    changed: list[Path] = []
    all_paths = set(previous) | set(current)
    for path in sorted(all_paths):
        if previous.get(path) != current.get(path):
            changed.append(path)
    return changed


def start_app(project_root: Path) -> subprocess.Popen:
    print("Starting app.py")
    return subprocess.Popen([sys.executable, "app.py"], cwd=project_root)


def stop_app(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def main() -> int:
    project_root = Path(__file__).resolve().parent
    previous_state = snapshot_files(project_root)
    process = start_app(project_root)

    print("Watching .py files for changes...")
    try:
        while True:
            time.sleep(POLL_INTERVAL_SECONDS)

            if process.poll() is not None:
                process = start_app(project_root)
                previous_state = snapshot_files(project_root)
                continue

            current_state = snapshot_files(project_root)
            changes = changed_files(previous_state, current_state)
            if not changes:
                continue

            print("Detected changes:")
            for path in changes[:8]:
                print(f" - {path.relative_to(project_root)}")
            if len(changes) > 8:
                print(f" - ... and {len(changes) - 8} more")

            stop_app(process)
            process = start_app(project_root)
            previous_state = current_state
    except KeyboardInterrupt:
        print("Stopping watcher...")
    finally:
        stop_app(process)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
