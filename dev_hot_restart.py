from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from typing import Dict, Iterable, List, Tuple

RUNTIME_DIRNAME = "_runtime"
IGNORE_DIR_NAMES = {
    ".git",
    "__pycache__",
    RUNTIME_DIRNAME,
}
IGNORE_DIR_PREFIXES = (
    ".venv",
    ".venv_broken_",
    ".venv_store_",
)
WATCH_EXTENSIONS = {".py"}
_LOCK_HANDLE = None


def _is_ignored_dir(name: str) -> bool:
    if name in IGNORE_DIR_NAMES:
        return True
    return any(name.startswith(p) for p in IGNORE_DIR_PREFIXES)


def _iter_watch_files(root: str) -> Iterable[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _is_ignored_dir(d)]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in WATCH_EXTENSIONS:
                continue
            yield os.path.join(dirpath, fn)


def _snapshot(root: str) -> Dict[str, Tuple[int, int]]:
    out: Dict[str, Tuple[int, int]] = {}
    for path in _iter_watch_files(root):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        out[path] = (int(stat.st_mtime_ns), int(stat.st_size))
    return out


def _diff_paths(prev: Dict[str, Tuple[int, int]], cur: Dict[str, Tuple[int, int]]) -> List[str]:
    changed: List[str] = []
    prev_keys = set(prev.keys())
    cur_keys = set(cur.keys())
    for path in sorted(prev_keys ^ cur_keys):
        changed.append(path)
    for path in sorted(prev_keys & cur_keys):
        if prev[path] != cur[path]:
            changed.append(path)
    return changed


def _spawn_app(py_exe: str, app_py: str, app_args: List[str], cwd: str) -> subprocess.Popen:
    gui_py = py_exe
    try:
        if os.path.basename(py_exe).lower() == "python.exe":
            candidate = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
            if os.path.exists(candidate):
                gui_py = candidate
    except Exception:
        gui_py = py_exe

    cmd = [gui_py, app_py, *app_args]
    env = os.environ.copy()
    env["FFD_HOT_RELOAD_ACTIVE"] = "1"
    return subprocess.Popen(cmd, cwd=cwd, env=env)


def _terminate_process(proc: subprocess.Popen, timeout_sec: float = 6.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        return
    started = time.time()
    while proc.poll() is None and (time.time() - started) < timeout_sec:
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass


def _resolve_handshake_paths(root: str) -> Tuple[str, str]:
    req = os.path.join(root, RUNTIME_DIRNAME, "hot_reload_request.json")
    resp = os.path.join(root, RUNTIME_DIRNAME, "hot_reload_response.json")
    return os.path.normpath(req), os.path.normpath(resp)


def _safe_remove(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _clear_reload_handshake(req_path: str, resp_path: str) -> None:
    _safe_remove(req_path)
    _safe_remove(resp_path)


def _write_reload_request(
    req_path: str,
    request_id: str,
    root: str,
    changed_paths: List[str],
    decision_timeout_sec: float,
) -> None:
    os.makedirs(os.path.dirname(req_path) or ".", exist_ok=True)
    rel_paths: list[str] = []
    for path in changed_paths or []:
        try:
            rel_paths.append(os.path.relpath(path, root))
        except Exception:
            rel_paths.append(str(path))

    payload = {
        "request_id": str(request_id),
        "ts_epoch": float(time.time()),
        "decision_timeout_sec": float(max(1.0, decision_timeout_sec)),
        "change_count": int(len(changed_paths or [])),
        "files": rel_paths[:20],
    }
    with open(req_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def _read_reload_response(resp_path: str) -> Dict[str, str]:
    if not resp_path or not os.path.exists(resp_path):
        return {}
    try:
        with open(resp_path, "r", encoding="utf-8") as handle:
            obj = json.load(handle)
        if not isinstance(obj, dict):
            return {}
        return {
            "request_id": str(obj.get("request_id") or "").strip(),
            "action": str(obj.get("action") or "").strip().lower(),
        }
    except Exception:
        return {}


def _warn_and_ask_restart(exit_code: int) -> bool:
    try:
        title = "Fabrication Flow Dashboard - Hot Reload"
        msg = (
            f"The app process exited with code {exit_code}.\n\n"
            "Press Retry to restart, or Cancel to exit."
        )
        MB_RETRYCANCEL = 0x00000005
        MB_ICONWARNING = 0x00000030
        IDRETRY = 4
        result = ctypes.windll.user32.MessageBoxW(0, msg, title, MB_RETRYCANCEL | MB_ICONWARNING)
        return int(result) == IDRETRY
    except Exception:
        return False


def _warn_and_ask_hot_relaunch(changed_count: int, timeout_sec: float) -> bool:
    try:
        title = "Fabrication Flow Dashboard - Hot Reload Decision"
        msg = (
            f"{int(max(0, changed_count))} file(s) changed.\n\n"
            "No in-app decision was received.\n"
            "Press Retry to restart now, or Cancel to keep this session.\n\n"
            f"Auto-restart in {int(max(1.0, timeout_sec))} seconds."
        )

        MB_RETRYCANCEL = 0x00000005
        MB_ICONWARNING = 0x00000030
        MB_TOPMOST = 0x00040000
        IDRETRY = 4
        IDTIMEOUT = 32000

        timed_fn = getattr(ctypes.windll.user32, "MessageBoxTimeoutW", None)
        if timed_fn is not None:
            result = timed_fn(
                0,
                msg,
                title,
                MB_RETRYCANCEL | MB_ICONWARNING | MB_TOPMOST,
                0,
                int(max(1.0, timeout_sec) * 1000),
            )
            if int(result) == IDRETRY:
                return True
            if int(result) in (0, 2):
                return False
            if int(result) == IDTIMEOUT:
                return True

        result = ctypes.windll.user32.MessageBoxW(
            0,
            msg,
            title,
            MB_RETRYCANCEL | MB_ICONWARNING | MB_TOPMOST,
        )
        return int(result) == IDRETRY
    except Exception:
        return True


def _acquire_single_instance_lock(root: str):
    try:
        key = hashlib.sha1(os.path.normpath(root).lower().encode("utf-8")).hexdigest()
        name = f"Global\\FABRICATION_FLOW_DASHBOARD_HOT_{key}"
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        if not handle:
            return None
        ERROR_ALREADY_EXISTS = 183
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            ctypes.windll.kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Dev hot-restart launcher for Fabrication Flow Dashboard.")
    parser.add_argument("app_args", nargs=argparse.REMAINDER, help="Arguments forwarded to app.py")
    parser.add_argument("--interval", type=float, default=0.6, help="Polling interval in seconds.")
    parser.add_argument("--debounce", type=float, default=5.0, help="Quiet-window delay before restart.")
    parser.add_argument(
        "--min-uptime",
        type=float,
        default=1.2,
        help="Minimum app uptime before hot-restart can trigger.",
    )
    parser.add_argument(
        "--decision-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for in-app Accept/Reject before forcing reload.",
    )
    ns = parser.parse_args()

    root = os.path.dirname(os.path.abspath(__file__))
    global _LOCK_HANDLE
    _LOCK_HANDLE = _acquire_single_instance_lock(root)
    if _LOCK_HANDLE is None:
        print("Hot restart launcher is already running for this project. Exiting duplicate launch.")
        return 0

    app_py = os.path.join(root, "app.py")
    py_exe = sys.executable
    app_args = list(ns.app_args or [])
    if app_args and app_args[0] == "--":
        app_args = app_args[1:]

    print("Hot restart launcher running.")
    print(f"Python: {py_exe}")
    try:
        pyw = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
        if os.path.exists(pyw):
            print(f"AppPy:  {pyw}")
    except Exception:
        pass
    print(f"Main:   {app_py}")
    print(f"Args:   {app_args}")
    print(f"Decision timeout: {max(1.0, float(ns.decision_timeout)):.1f}s")
    print("Watching .py files. Press Ctrl+C to stop.")

    req_path, resp_path = _resolve_handshake_paths(root)
    _clear_reload_handshake(req_path, resp_path)

    prev = _snapshot(root)
    proc = _spawn_app(py_exe, app_py, app_args, cwd=root)
    last_spawn_at = time.time()
    pending_restart = False
    awaiting_user_decision = False
    current_request_id = ""
    request_posted_at = 0.0
    last_change_at = 0.0
    pending_changes: List[str] = []
    decision_timeout = max(1.0, float(ns.decision_timeout))

    try:
        while True:
            if proc.poll() is not None:
                if pending_restart and not awaiting_user_decision:
                    proc = _spawn_app(py_exe, app_py, app_args, cwd=root)
                    last_spawn_at = time.time()
                    pending_restart = False
                    last_change_at = 0.0
                    pending_changes = []
                    continue

                _clear_reload_handshake(req_path, resp_path)
                rc = int(proc.returncode or 0)
                if rc == 0:
                    return 0
                print(f"App exited with code {rc}.")
                if _warn_and_ask_restart(rc):
                    proc = _spawn_app(py_exe, app_py, app_args, cwd=root)
                    last_spawn_at = time.time()
                    pending_restart = False
                    awaiting_user_decision = False
                    current_request_id = ""
                    request_posted_at = 0.0
                    last_change_at = 0.0
                    pending_changes = []
                    continue
                return rc

            time.sleep(max(0.2, float(ns.interval)))
            now = time.time()
            cur = _snapshot(root)
            changed = _diff_paths(prev, cur)
            prev = cur

            if changed:
                pending_restart = True
                last_change_at = now
                pending_changes = sorted(set(pending_changes).union(changed))
                short = [os.path.relpath(path, root) for path in changed[:4]]
                suffix = " ..." if len(changed) > 4 else ""
                print(f"Change detected ({len(changed)}): {', '.join(short)}{suffix}")

            quiet_for = (now - last_change_at) if pending_restart else 0.0
            uptime = now - last_spawn_at
            if pending_restart and quiet_for >= max(0.1, float(ns.debounce)) and uptime >= max(
                0.2, float(ns.min_uptime)
            ):
                if not awaiting_user_decision:
                    current_request_id = str(int(time.time() * 1000))
                    _write_reload_request(
                        req_path,
                        current_request_id,
                        root,
                        pending_changes,
                        decision_timeout_sec=decision_timeout,
                    )
                    awaiting_user_decision = True
                    request_posted_at = now
                    batch_count = len(pending_changes)
                    print(
                        f"Hot-reload request posted after {quiet_for:.1f}s quiet "
                        f"({batch_count} file(s) batched). Waiting for in-app accept/reject "
                        f"(auto-reload in {decision_timeout:.0f}s)..."
                    )

                waited_for = max(0.0, now - request_posted_at)
                if awaiting_user_decision and request_posted_at > 0.0 and waited_for >= decision_timeout:
                    batch_count = len(pending_changes)
                    print(f"No in-app decision after {decision_timeout:.0f}s ({batch_count} file(s)).")
                    if _warn_and_ask_hot_relaunch(batch_count, decision_timeout):
                        print(f"Hot relaunch accepted in launcher; restarting app ({batch_count} file(s)).")
                        _terminate_process(proc)
                        proc = _spawn_app(py_exe, app_py, app_args, cwd=root)
                        last_spawn_at = time.time()
                    else:
                        print("Hot relaunch rejected in launcher; keeping current session.")
                    pending_restart = False
                    awaiting_user_decision = False
                    current_request_id = ""
                    request_posted_at = 0.0
                    last_change_at = 0.0
                    pending_changes = []
                    _clear_reload_handshake(req_path, resp_path)
                    continue

                resp = _read_reload_response(resp_path)
                if resp.get("request_id", "") != current_request_id:
                    continue

                action = resp.get("action", "")
                if action == "accept":
                    batch_count = len(pending_changes)
                    print(f"Reload accepted ({batch_count} file(s)); restarting app...")
                    _terminate_process(proc)
                    proc = _spawn_app(py_exe, app_py, app_args, cwd=root)
                    last_spawn_at = time.time()
                    pending_restart = False
                    awaiting_user_decision = False
                    current_request_id = ""
                    request_posted_at = 0.0
                    last_change_at = 0.0
                    pending_changes = []
                    _clear_reload_handshake(req_path, resp_path)
                elif action == "reject":
                    print("Reload rejected in app; keeping current session.")
                    pending_restart = False
                    awaiting_user_decision = False
                    current_request_id = ""
                    request_posted_at = 0.0
                    last_change_at = 0.0
                    pending_changes = []
                    _clear_reload_handshake(req_path, resp_path)
    except KeyboardInterrupt:
        print("\nStopping hot restart launcher.")
        _clear_reload_handshake(req_path, resp_path)
        _terminate_process(proc)
        return 0
    except Exception as exc:
        print(f"Hot restart launcher error: {exc}")
        _clear_reload_handshake(req_path, resp_path)
        _terminate_process(proc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
