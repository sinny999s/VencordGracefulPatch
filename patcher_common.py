"""
Shared helpers for Vencord Graceful Patcher.

Everything here is standard-library only and safe to import on any platform
(Windows-only calls are guarded so the unit tests can run elsewhere).
"""

import os
import json
import logging
import subprocess
from pathlib import Path
from typing import Optional, Set

IS_WINDOWS = os.name == "nt"

# Windows creation flag to suppress CMD popups when spawning helpers
CREATE_NO_WINDOW = 0x08000000
_SPAWN_KWARGS = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}

LOCAL_APPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))
APPDATA = os.environ.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming"))
APP_DATA_DIR = Path(LOCAL_APPDATA) / "VencordGracefulPatch"
LOG_FILE = APP_DATA_DIR / "patcher.log"
STATE_FILE = APP_DATA_DIR / "state.json"

APP_NAME = "Vencord Graceful Patcher"
DAEMON_MUTEX_NAME = "VencordGracefulPatchDaemon"

DISCORD_BRANCHES = {
    "stable": {
        "folder": "Discord",
        "process": "Discord.exe",
        "display_name": "Discord Stable",
    },
    "ptb": {
        "folder": "DiscordPTB",
        "process": "DiscordPTB.exe",
        "display_name": "Discord PTB",
    },
    "canary": {
        "folder": "DiscordCanary",
        "process": "DiscordCanary.exe",
        "display_name": "Discord Canary",
    },
    "development": {
        "folder": "DiscordDevelopment",
        "process": "DiscordDevelopment.exe",
        "display_name": "Discord Development",
    },
}

# Maps the %LOCALAPPDATA% folder name to the process that owns it
FOLDER_TO_PROCESS = {b["folder"]: b["process"] for b in DISCORD_BRANCHES.values()}
ALL_DISCORD_PROCESSES = [b["process"] for b in DISCORD_BRANCHES.values()]


def _ps_quote(text: str) -> str:
    """Escapes a string for use inside a single-quoted PowerShell literal."""
    return str(text).replace("'", "''")


def send_windows_notification(title: str, message: str, app_name: str = APP_NAME) -> None:
    """Sends a native Windows toast notification without external dependencies."""
    if not IS_WINDOWS:
        logging.debug(f"[toast] {title}: {message}")
        return

    ps_script = f"""
    try {{
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $textNodes = $template.GetElementsByTagName('text')
        $textNodes.Item(0).AppendChild($template.CreateTextNode('{_ps_quote(title)}')) | Out-Null
        $textNodes.Item(1).AppendChild($template.CreateTextNode('{_ps_quote(message)}')) | Out-Null
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{_ps_quote(app_name)}')
        $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
        $notifier.Show($toast)
    }} catch {{}}
    """
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_script],
            capture_output=True,
            timeout=30,
            **_SPAWN_KWARGS,
        )
    except Exception as e:
        logging.debug(f"Failed to send toast notification: {e}")


def get_running_processes() -> Optional[Set[str]]:
    """
    Returns the lowercase image names of all running processes using a single
    `tasklist` call. Returns None when the snapshot cannot be taken, so callers
    can treat "unknown" as "Discord may be running" and leave files alone.
    """
    if not IS_WINDOWS:
        return set()
    try:
        result = subprocess.run(
            ["tasklist", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            timeout=30,
            **_SPAWN_KWARGS,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tasklist exited with {result.returncode}")
    except Exception as e:
        logging.error(f"Could not list running processes: {e}")
        return None

    names = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        # CSV rows look like: "Discord.exe","1234","Console","1","123,456 K"
        names.add(line[1:].split('"', 1)[0].lower())
    return names


def is_process_running(process_name: str, snapshot: Optional[Set[str]] = None) -> bool:
    """
    Checks whether a process is running, optionally against an existing snapshot.
    When no snapshot can be taken the answer is True (assume running) so that
    nothing is ever patched underneath a live Discord.
    """
    if snapshot is None:
        snapshot = get_running_processes()
    if snapshot is None:
        return True
    return process_name.lower() in snapshot


def discord_processes_for_path(path: Path) -> list:
    """
    Returns the Discord process name(s) that own a file inside a Discord install
    (e.g. ...\\DiscordCanary\\app-1.0.1\\... -> ["DiscordCanary.exe"]).
    Falls back to every Discord process when the branch cannot be determined.
    """
    parts = Path(path).parts
    for i in range(len(parts) - 1):
        if parts[i + 1].lower().startswith("app-") and parts[i] in FOLDER_TO_PROCESS:
            return [FOLDER_TO_PROCESS[parts[i]]]
    return list(ALL_DISCORD_PROCESSES)


def load_state() -> dict:
    """Loads the small persisted state file (notification debounce etc.)."""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logging.debug(f"Could not read state file: {e}")
    return {}


def save_state(state: dict) -> None:
    try:
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logging.debug(f"Could not write state file: {e}")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Writes a file via a temporary sibling and an atomic rename."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Single-instance guard (named mutex, auto-released when the process exits)
# ---------------------------------------------------------------------------

_ERROR_ALREADY_EXISTS = 183
_held_mutex = None


def _kernel32():
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    return k32, ctypes


def acquire_single_instance(name: str = DAEMON_MUTEX_NAME) -> bool:
    """
    Tries to become the single running daemon. Returns False if another
    instance already holds the mutex. On non-Windows platforms always True.
    """
    global _held_mutex
    if not IS_WINDOWS:
        return True
    try:
        k32, ctypes = _kernel32()
        handle = k32.CreateMutexW(None, False, name)
        if not handle:
            return True  # could not create a mutex at all; do not block startup
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            k32.CloseHandle(handle)
            return False
        _held_mutex = handle
        return True
    except Exception as e:
        logging.debug(f"Single-instance check failed: {e}")
        return True


def is_daemon_running(name: str = DAEMON_MUTEX_NAME) -> bool:
    """Checks (without holding it) whether the daemon mutex is currently owned."""
    if not IS_WINDOWS:
        return False
    if _held_mutex is not None:
        return True
    try:
        k32, ctypes = _kernel32()
        handle = k32.CreateMutexW(None, False, name)
        if not handle:
            return False
        exists = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
        k32.CloseHandle(handle)
        return exists
    except Exception:
        return False


def stop_daemon_processes(exclude_pid: Optional[int] = None) -> list:
    """
    Terminates every python/pythonw process running `patcher.py --daemon`.
    Uses PowerShell CIM (wmic is no longer shipped on current Windows).
    Returns the list of PIDs that were terminated.
    """
    if not IS_WINDOWS:
        return []
    exclude_pid = exclude_pid if exclude_pid is not None else os.getpid()
    ps_script = (
        "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" | "
        "Where-Object { $_.CommandLine -like '*patcher.py*' -and $_.CommandLine -like '*--daemon*' "
        f"-and $_.ProcessId -ne {int(exclude_pid)} }} | "
        "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Output $_.ProcessId } catch {} }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=60,
            **_SPAWN_KWARGS,
        )
        pids = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids
    except Exception as e:
        logging.error(f"Failed to stop background patcher: {e}")
        return []
