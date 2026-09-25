"""
Discord Voice Stereo & High-Bitrate Native Patcher
===================================================
Dynamically finds and patches WebRTC Opus channel and bitrate configurations
in discord_voice.node without relying on brittle fixed memory offsets or
abandoned third-party binaries.

Includes fail-safe verification and automatic desktop notifications.
"""

import sys
import os
import json
import shutil
import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Optional, Tuple, List, Dict

CREATE_NO_WINDOW = 0x08000000

SCRIPT_DIR = Path(__file__).resolve().parent
PATTERNS_FILE = SCRIPT_DIR / "audio_patterns.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)


def send_windows_notification(title: str, message: str):
    """Sends a native Windows toast notification without external dependencies."""
    ps_script = f"""
    try {{
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $textNodes = $template.GetElementsByTagName('text')
        $textNodes.Item(0).AppendChild($template.CreateTextNode('{title}')) | Out-Null
        $textNodes.Item(1).AppendChild($template.CreateTextNode('{message}')) | Out-Null
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Discord Stereo Patcher')
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
        logging.debug(f"Failed to send toast: {e}")


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


def load_patterns() -> List[Dict]:
    """Loads pattern definitions from audio_patterns.json."""
    if not PATTERNS_FILE.exists():
        logging.error(f"Patterns file not found at {PATTERNS_FILE}")
        return []
    try:
        with open(PATTERNS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("profiles", [])
    except Exception as e:
        logging.error(f"Failed to load {PATTERNS_FILE}: {e}")
        return []


def find_voice_modules() -> List[Path]:
    """Finds all discord_voice.node files in %LOCALAPPDATA%\\Discord*."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return []

    modules = []
    base_dirs = ["Discord", "DiscordPTB", "DiscordCanary", "DiscordDevelopment"]
    for b in base_dirs:
        discord_root = Path(local_app_data) / b
        if not discord_root.exists():
            continue
        app_dirs = [d for d in discord_root.glob("app-*") if d.is_dir()]
        app_dirs.sort(key=lambda d: [int(x) for x in d.name.replace("app-", "").split(".") if x.isdigit()], reverse=True)
        
        for app_dir in app_dirs:
            voice_nodes = list(app_dir.rglob("discord_voice.node"))
            if voice_nodes:
                modules.append(voice_nodes[0])
                break

    return modules


def inspect_voice_module(node_path: Path) -> Dict:
    """Inspects a discord_voice.node file to determine its patch status."""
    if not node_path.exists():
        return {"status": "NOT_FOUND", "path": str(node_path)}

    try:
        data = node_path.read_bytes()
    except Exception as e:
        return {"status": "ERROR", "error": str(e), "path": str(node_path)}

    md5_hash = hashlib.md5(data).hexdigest()
    file_size = len(data)
    bak_path = node_path.with_suffix(".node.bak")
    has_backup = bak_path.exists()

    profiles = load_patterns()
    for prof in profiles:
        search_bytes = bytes.fromhex(prof["search_hex"].replace(" ", ""))
        replace_bytes = bytes.fromhex(prof["replace_hex"].replace(" ", ""))
        expected = prof.get("expected_matches", 1)

        # Check if already patched
        replace_matches = []
        pos = 0
        while True:
            pos = data.find(replace_bytes, pos)
            if pos == -1:
                break
            replace_matches.append(pos)
            pos += len(replace_bytes)

        if len(replace_matches) == expected:
            return {
                "status": "PATCHED",
                "profile": prof["name"],
                "matches": [hex(m) for m in replace_matches],
                "size": file_size,
                "md5": md5_hash,
                "has_backup": has_backup,
                "path": str(node_path)
            }

        # Check if unpatched and ready
        search_matches = []
        pos = 0
        while True:
            pos = data.find(search_bytes, pos)
            if pos == -1:
                break
            search_matches.append(pos)
            pos += len(search_bytes)

        if len(search_matches) == expected:
            return {
                "status": "UNPATCHED_READY",
                "profile": prof["name"],
                "matches": [hex(m) for m in search_matches],
                "size": file_size,
                "md5": md5_hash,
                "has_backup": has_backup,
                "path": str(node_path)
            }

    return {
        "status": "UNRECOGNIZED_SIGNATURE",
        "size": file_size,
        "md5": md5_hash,
        "has_backup": has_backup,
        "path": str(node_path)
    }


_notified_signatures = set()


def patch_voice_module(node_path: Path, notify: bool = True) -> bool:
    """
    Safely patches discord_voice.node using dynamic signature matching.
    If the pattern does not match with 100% confidence, safely aborts
    and sends a notification without modifying the file.
    """
    if not node_path.exists():
        logging.warning(f"Voice module does not exist: {node_path}")
        return False

    # Check if Discord is running
    for proc in ["Discord.exe", "DiscordCanary.exe", "DiscordPTB.exe", "DiscordDevelopment.exe"]:
        if is_process_running(proc):
            logging.warning(f"Cannot patch: {proc} is currently running. Close Discord first.")
            return False

    info = inspect_voice_module(node_path)
    status = info.get("status")
    path_key = f"{node_path}_{info.get('md5', '')}"

    if status == "PATCHED":
        logging.info(f"Already patched: {node_path.name} ({info.get('profile')})")
        if path_key in _notified_signatures:
            _notified_signatures.discard(path_key)
        return True

    if status == "UNRECOGNIZED_SIGNATURE":
        logging.warning(f"[FAIL-SAFE] Pattern signature not recognized in {node_path}")
        logging.warning("Aborting patch to ensure Discord stability. File left completely stock.")
        if notify and path_key not in _notified_signatures:
            _notified_signatures.add(path_key)
            send_windows_notification(
                "Discord Audio Notice",
                "Discord updated to a new voice build. Stereo pattern not recognized; running safely in stock mode."
            )
        return False

    if status == "UNPATCHED_READY":
        profile_name = info.get("profile")
        profiles = [p for p in load_patterns() if p["name"] == profile_name]
        if not profiles:
            logging.error("Matched profile not found in configuration.")
            return False
        prof = profiles[0]

        search_bytes = bytes.fromhex(prof["search_hex"].replace(" ", ""))
        replace_bytes = bytes.fromhex(prof["replace_hex"].replace(" ", ""))

        try:
            data = bytearray(node_path.read_bytes())
            bak_path = node_path.with_suffix(".node.bak")

            # Create backup if not already present
            if not bak_path.exists():
                shutil.copy2(node_path, bak_path)
                logging.info(f"Created stock backup: {bak_path}")

            # Apply patch
            matches = [int(x, 16) for x in info["matches"]]
            for offset in matches:
                data[offset:offset + len(replace_bytes)] = replace_bytes

            # Write patched file
            node_path.write_bytes(data)
            logging.info(f"Successfully patched {len(matches)} locations in {node_path.name}")

            if notify:
                send_windows_notification(
                    "Discord Stereo Audio Active",
                    "True Stereo (2-Channel / 384kbps) successfully enabled for Discord."
                )
            return True
        except Exception as e:
            logging.error(f"Failed to write patched file: {e}")
            return False

    return False


def unpatch_voice_module(node_path: Path, notify: bool = True) -> bool:
    """Restores the original stock discord_voice.node from backup."""
    if not node_path.exists():
        logging.warning(f"Voice module does not exist: {node_path}")
        return False

    for proc in ["Discord.exe", "DiscordCanary.exe", "DiscordPTB.exe", "DiscordDevelopment.exe"]:
        if is_process_running(proc):
            logging.warning(f"Cannot unpatch: {proc} is currently running. Close Discord first.")
            return False

    bak_path = node_path.with_suffix(".node.bak")
    if bak_path.exists():
        try:
            shutil.copy2(bak_path, node_path)
            logging.info(f"Successfully restored stock {node_path.name} from backup.")
            if notify:
                send_windows_notification(
                    "Discord Audio Reverted",
                    "Stock mono voice module successfully restored."
                )
            return True
        except Exception as e:
            logging.error(f"Failed to restore backup: {e}")
            return False
    else:
        logging.warning(f"No backup file found at {bak_path}")
        return False


def patch_all_installed_branches(notify: bool = True):
    """Finds and patches all installed Discord branches."""
    modules = find_voice_modules()
    if not modules:
        logging.info("No discord_voice.node modules found on system.")
        return

    for m in modules:
        logging.info(f"Processing voice module: {m}")
        patch_voice_module(m, notify=notify)


def print_status():
    """Prints status of all detected Discord voice modules."""
    modules = find_voice_modules()
    if not modules:
        print("No Discord voice modules found.")
        return

    print("\n" + "=" * 65)
    print(" DISCORD VOICE MODULE STEREO STATUS")
    print("=" * 65)
    for m in modules:
        info = inspect_voice_module(m)
        status = info.get("status")
        print(f"\nLocation: {m}")
        print(f"File Size: {info.get('size', 0):,} bytes | MD5: {info.get('md5', 'N/A')}")
        print(f"Stock Backup (.bak): {'EXISTS' if info.get('has_backup') else 'None'}")
        if status == "PATCHED":
            print(f"Status:   [+] PATCHED (Stereo 2-Channel & 384kbps Active)")
            print(f"Profile:  {info.get('profile')}")
            print(f"Offsets:  {', '.join(info.get('matches', []))}")
        elif status == "UNPATCHED_READY":
            print(f"Status:   [-] UNPATCHED (Original Mono - Ready to Patch)")
            print(f"Profile:  {info.get('profile')}")
            print(f"Offsets:  {', '.join(info.get('matches', []))}")
        elif status == "UNRECOGNIZED_SIGNATURE":
            print(f"Status:   [!] UNRECOGNIZED SIGNATURE (Safe Fail-Safe Active)")
            print("Note:     Discord update changed voice assembly. Left stock to prevent crashes.")
        else:
            print(f"Status:   {status}")
    print("\n" + "=" * 65 + "\n")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in ["--status", "-s"]:
            print_status()
        elif arg in ["--patch", "-p"]:
            patch_all_installed_branches(notify=True)
        elif arg in ["--unpatch", "-u"]:
            for m in find_voice_modules():
                unpatch_voice_module(m, notify=True)
        else:
            print(f"Unknown argument: {arg}")
            print("Usage: python voice_patcher.py [--status | --patch | --unpatch]")
    else:
        print_status()
