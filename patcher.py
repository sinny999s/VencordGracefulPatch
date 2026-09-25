"""
Vencord Graceful Patcher
A Discord-friendly background auto-patcher for Vencord.
Waits gracefully until Discord closes before applying patches.
Zero voice call interruptions. Zero corrupted updates.
"""

import os
import sys
import time
import json
import logging
import argparse
import subprocess
import urllib.request
from pathlib import Path
from datetime import datetime

# Base directories
LOCAL_APPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))
APPDATA = os.environ.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming"))
APP_DATA_DIR = Path(LOCAL_APPDATA) / "VencordGracefulPatch"
LOG_FILE = APP_DATA_DIR / "patcher.log"
CONFIG_FILE = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "check_interval_seconds": 10,
    "cooldown_after_close_seconds": 5,
    "show_notifications": True,
    "relaunch_discord_after_patch": False,
    "custom_installer_path": "",
    "monitored_branches": ["stable", "ptb", "canary", "development"]
}

DISCORD_BRANCHES = {
    "stable": {
        "folder": "Discord",
        "process": "Discord.exe",
        "display_name": "Discord Stable",
        "cli_branch": "stable"
    },
    "ptb": {
        "folder": "DiscordPTB",
        "process": "DiscordPTB.exe",
        "display_name": "Discord PTB",
        "cli_branch": "ptb"
    },
    "canary": {
        "folder": "DiscordCanary",
        "process": "DiscordCanary.exe",
        "display_name": "Discord Canary",
        "cli_branch": "canary"
    },
    "development": {
        "folder": "DiscordDevelopment",
        "process": "DiscordDevelopment.exe",
        "display_name": "Discord Development",
        "cli_branch": "development"
    }
}

# Windows creation flag to suppress CMD popups
CREATE_NO_WINDOW = 0x08000000


def setup_logging():
    """Sets up logging to both file and console."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    # Cap log size to ~2MB if needed
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
        try:
            backup_log = APP_DATA_DIR / "patcher.log.old"
            if backup_log.exists():
                backup_log.unlink()
            LOG_FILE.rename(backup_log)
        except Exception:
            pass

    handlers = [
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers
    )


def load_config() -> dict:
    """Loads configuration with fallback defaults."""
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
                cfg.update(user_cfg)
        except Exception as e:
            logging.warning(f"Failed to load config.json: {e}. Using defaults.")
    return cfg


def send_windows_notification(title: str, message: str):
    """Sends a native Windows toast notification without external dependencies."""
    ps_script = f"""
    try {{
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $textNodes = $template.GetElementsByTagName('text')
        $textNodes.Item(0).AppendChild($template.CreateTextNode('{title}')) | Out-Null
        $textNodes.Item(1).AppendChild($template.CreateTextNode('{message}')) | Out-Null
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Vencord Graceful Patcher')
        $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
        $notifier.Show($toast)
    }} catch {{}}
    """
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW
        )
    except Exception as e:
        logging.debug(f"Failed to send toast notification: {e}")


def is_process_running(process_name: str) -> bool:
    """Checks if a process is currently running on Windows."""
    try:
        cmd = ["tasklist", "/NH", "/FI", f"IMAGENAME eq {process_name}"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW
        )
        return process_name.lower() in result.stdout.lower()
    except Exception as e:
        logging.error(f"Error checking process {process_name}: {e}")
        return False


def get_version_tuple(dir_name: str) -> tuple:
    """Extracts numeric tuple from 'app-1.0.9259' -> (1, 0, 9259)."""
    try:
        parts = dir_name.replace("app-", "").split(".")
        return tuple(int(p) for p in parts)
    except Exception:
        return (0, 0, 0)


def find_installer(custom_path: str = "") -> str:
    """Locates or downloads a working Vencord installer."""
    candidates = []

    if custom_path and os.path.isfile(custom_path):
        candidates.append(custom_path)

    # 1. Local repository folder
    script_dir = Path(__file__).parent
    candidates.append(str(script_dir / "VencordInstallerCli.exe"))
    candidates.append(str(script_dir / "vencordinstaller.exe"))
    candidates.append(str(script_dir / "VencordInstaller.exe"))

    # 2. Existing BetterVencordPatch or AppData locations
    candidates.append(os.path.join(LOCAL_APPDATA, "BetterVencordPatch", "vencordinstaller.exe"))
    candidates.append(str(APP_DATA_DIR / "VencordInstallerCli.exe"))

    for c in candidates:
        if os.path.isfile(c):
            return c

    # 3. If not found, download official VencordInstallerCli.exe from GitHub
    logging.info("No Vencord installer found locally. Downloading official VencordInstallerCli.exe...")
    download_url = "https://github.com/Vencord/Installer/releases/latest/download/VencordInstallerCli.exe"
    target_path = APP_DATA_DIR / "VencordInstallerCli.exe"

    try:
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(
            download_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req) as resp, open(target_path, "wb") as out_file:
            out_file.write(resp.read())
        logging.info(f"Downloaded VencordInstallerCli.exe to {target_path}")
        return str(target_path)
    except Exception as e:
        logging.error(f"Failed to auto-download Vencord installer: {e}")
        return ""


def check_branch_status(branch_key: str) -> dict:
    """
    Inspects a Discord branch directory and checks for unpatched versions.
    Returns details: {
        'exists': bool,
        'branch': str,
        'display_name': str,
        'base_path': Path,
        'process': str,
        'is_running': bool,
        'versions': list of dicts,
        'unpatched_versions': list of version names needing patch
    }
    """
    info = DISCORD_BRANCHES[branch_key]
    base_path = Path(LOCAL_APPDATA) / info["folder"]
    result = {
        "exists": base_path.exists(),
        "branch": branch_key,
        "display_name": info["display_name"],
        "base_path": base_path,
        "process": info["process"],
        "is_running": False,
        "versions": [],
        "unpatched_versions": []
    }

    if not base_path.exists():
        return result

    result["is_running"] = is_process_running(info["process"])

    app_dirs = [d for d in base_path.iterdir() if d.is_dir() and d.name.startswith("app-")]
    app_dirs.sort(key=lambda d: get_version_tuple(d.name))

    for d in app_dirs:
        resources = d / "resources"
        app_asar = resources / "app.asar"
        orig_asar = resources / "_app.asar"

        ver_info = {
            "name": d.name,
            "version": d.name.replace("app-", ""),
            "has_resources": resources.exists(),
            "has_app_asar": app_asar.exists(),
            "has_orig_asar": orig_asar.exists(),
            "is_patched": orig_asar.exists(),
            "ready_for_patch": False
        }

        # An update is fully written by Discord and ready to be patched when:
        # 1. resources/app.asar exists (the clean Discord file)
        # 2. resources/_app.asar does NOT exist yet (Vencord hasn't patched it yet)
        if app_asar.exists() and not orig_asar.exists():
            ver_info["ready_for_patch"] = True
            result["unpatched_versions"].append(d.name)

        result["versions"].append(ver_info)

    return result


def apply_patch(installer_path: str, branch_key: str, location_flag: str = "") -> bool:
    """Executes the Vencord installer quietly without a console window."""
    branch_info = DISCORD_BRANCHES[branch_key]
    cli_branch = branch_info["cli_branch"]

    cmd = [installer_path, "-install", "-branch", cli_branch]
    if location_flag:
        cmd.extend(["-location", location_flag])

    logging.info(f"Running patch command: {' '.join(cmd)}")
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=30
        )
        if res.returncode == 0:
            logging.info("Installer exited successfully with code 0.")
            return True
        else:
            logging.warning(f"Installer returned code {res.returncode}. Stderr: {res.stderr.strip()} Stdout: {res.stdout.strip()}")
            # Some versions return 0 or do not print; verify through file inspection
            return True
    except subprocess.TimeoutExpired:
        logging.error("Installer timed out after 30 seconds.")
        return False
    except Exception as e:
        logging.error(f"Failed to execute installer: {e}")
        return False


def run_single_check(config: dict, waiting_state: dict) -> None:
    """Performs one scan across all monitored Discord branches."""
    installer_path = find_installer(config.get("custom_installer_path", ""))
    if not installer_path:
        logging.warning("No installer executable found. Skipping patch cycle.")
        return

    monitored = config.get("monitored_branches", ["stable"])

    for branch_key in monitored:
        if branch_key not in DISCORD_BRANCHES:
            continue

        status = check_branch_status(branch_key)
        if not status["exists"] or not status["unpatched_versions"]:
            # If it was previously waiting and is now patched
            if waiting_state.get(branch_key):
                waiting_state[branch_key] = False
            continue

        unpatched = status["unpatched_versions"]
        display_name = status["display_name"]
        is_running = status["is_running"]

        # If Discord is running: DO NOT KILL IT. Wait gracefully.
        if is_running:
            if not waiting_state.get(branch_key):
                logging.info(
                    f"[{display_name}] New update detected ({', '.join(unpatched)}). "
                    f"Discord is currently running. Waiting gracefully for Discord to close..."
                )
                waiting_state[branch_key] = True
            continue

        # Discord is closed!
        if waiting_state.get(branch_key):
            cooldown = config.get("cooldown_after_close_seconds", 5)
            logging.info(f"[{display_name}] Discord has closed! Waiting {cooldown}s cooldown for file locks...")
            time.sleep(cooldown)

        logging.info(f"[{display_name}] Discord is closed. Applying Vencord patch for: {', '.join(unpatched)}...")
        success = apply_patch(installer_path, branch_key)

        # Verify patch status
        post_status = check_branch_status(branch_key)
        newly_unpatched = post_status["unpatched_versions"]

        if not newly_unpatched:
            logging.info(f"[{display_name}] Successfully patched with Vencord!")
            if config.get("show_notifications", True):
                send_windows_notification(
                    "Vencord Auto-Patched",
                    f"{display_name} updated and was successfully patched with Vencord!"
                )
            if config.get("relaunch_discord_after_patch", False):
                try:
                    update_exe = status["base_path"] / "Update.exe"
                    if update_exe.exists():
                        subprocess.Popen(
                            [str(update_exe), "--processStart", status["process"]],
                            creationflags=CREATE_NO_WINDOW
                        )
                except Exception as e:
                    logging.error(f"Failed to relaunch Discord: {e}")
            waiting_state[branch_key] = False
        else:
            logging.warning(
                f"[{display_name}] Patch applied, but some versions are still unpatched: {newly_unpatched}"
            )


def daemon_loop():
    """Continuous background loop."""
    setup_logging()
    logging.info("Vencord Graceful Patcher daemon started.")
    config = load_config()
    waiting_state = {}

    interval = max(5, config.get("check_interval_seconds", 10))

    while True:
        try:
            run_single_check(config, waiting_state)
        except Exception as e:
            logging.error(f"Unexpected error in daemon loop: {e}", exc_info=True)

        time.sleep(interval)


def print_status():
    """Prints human-readable status for all branches in terminal."""
    setup_logging()
    config = load_config()
    installer = find_installer(config.get("custom_installer_path", ""))

    print("\n=======================================================")
    print("           VENCORD GRACEFUL PATCHER STATUS             ")
    print("=======================================================\n")
    print(f"Installer Binary : {installer if installer else 'NOT FOUND'}")
    print(f"Log File Location: {LOG_FILE}")
    print(f"Check Interval   : {config.get('check_interval_seconds', 10)}s\n")

    for branch_key in config.get("monitored_branches", ["stable"]):
        if branch_key not in DISCORD_BRANCHES:
            continue
        status = check_branch_status(branch_key)
        print(f"[{status['display_name']}]")
        if not status["exists"]:
            print("  Status: Not installed\n")
            continue

        print(f"  Path   : {status['base_path']}")
        print(f"  Running: {'YES (Will wait for close)' if status['is_running'] else 'NO (Safe to patch)'}")
        print("  Installed Versions:")
        for v in status["versions"]:
            state = "PATCHED (Vencord active)" if v["is_patched"] else "UNPATCHED (Needs Vencord)"
            if not v["has_app_asar"]:
                state = "DOWNLOADING / INCOMPLETE"
            print(f"    - {v['name']}: {state}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vencord Graceful Patcher")
    parser.add_argument("--status", action="store_true", help="Print current status and exit")
    parser.add_argument("--check-once", action="store_true", help="Perform a single check/patch and exit")
    parser.add_argument("--daemon", action="store_true", help="Run in continuous background daemon mode")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.check_once:
        setup_logging()
        cfg = load_config()
        run_single_check(cfg, {})
    else:
        daemon_loop()
