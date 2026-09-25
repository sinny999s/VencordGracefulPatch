"""
Vencord Graceful Patcher
A standalone, Discord-friendly native auto-patcher for Vencord.
Waits gracefully until Discord closes before applying patches.
Zero external binaries. Zero voice call interruptions.
"""

import os
import sys
import time
import json
import struct
import shutil
import zipfile
import logging
import argparse
import subprocess
import urllib.request
from pathlib import Path

# Base directories
LOCAL_APPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))
APPDATA = os.environ.get("APPDATA", os.path.expanduser(r"~\AppData\Roaming"))
APP_DATA_DIR = Path(LOCAL_APPDATA) / "VencordGracefulPatch"
LOG_FILE = APP_DATA_DIR / "patcher.log"
CONFIG_FILE = Path(__file__).parent / "config.json"
VENCORD_DATA_DIR = Path(APPDATA) / "Vencord"
VENCORD_DIST_DIR = VENCORD_DATA_DIR / "dist"
PATCHER_JS = VENCORD_DIST_DIR / "patcher.js"

DEFAULT_CONFIG = {
    "check_interval_seconds": 10,
    "cooldown_after_close_seconds": 5,
    "show_notifications": True,
    "enable_stereo_patch": True,
    "enable_fake_deafen": True,
    "relaunch_discord_after_patch": False,
    "use_native_patcher": True,
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
    
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
        try:
            backup_log = APP_DATA_DIR / "patcher.log.old"
            if backup_log.exists():
                backup_log.unlink()
            LOG_FILE.rename(backup_log)
        except Exception:
            pass

    handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
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


def create_vencord_asar(patcher_path: str) -> bytes:
    """
    Constructs a 100% byte-exact Electron ASAR archive containing
    package.json and index.js that hooks into Vencord.
    Completely eliminates the need for any external Go/C++ installer.
    """
    package_json = '{\n\t"name": "discord",\n\t"main": "index.js"\n}'
    index_js = f"require({json.dumps(patcher_path)})"

    index_bytes = index_js.encode("utf-8")
    pkg_bytes = package_json.encode("utf-8")

    header = {
        "files": {
            "index.js": {
                "size": len(index_bytes),
                "offset": "0"
            },
            "package.json": {
                "size": len(pkg_bytes),
                "offset": str(len(index_bytes))
            }
        }
    }

    hdr_str = json.dumps(header, separators=(",", ":"))
    hdr_bytes = hdr_str.encode("utf-8")
    hdr_len = len(hdr_bytes)

    data_size = 4
    aligned = (hdr_len + data_size - 1) & ~(data_size - 1)
    hdr_sz = aligned + 8
    obj_sz = aligned + data_size
    diff = aligned - hdr_len

    padded_header = hdr_bytes + (b"0" * diff)
    prefix = struct.pack("<IIII", 4, hdr_sz, obj_sz, hdr_len)

    return prefix + padded_header + index_bytes + pkg_bytes


def ensure_vencord_dist() -> Path:
    """Ensures Vencord's dist/patcher.js is present. Downloads from GitHub if missing."""
    if PATCHER_JS.exists():
        return PATCHER_JS

    logging.info("Vencord dist files missing. Downloading latest build from GitHub...")
    VENCORD_DIST_DIR.mkdir(parents=True, exist_ok=True)

    url = "https://github.com/Vendicated/Vencord/releases/latest/download/browser.zip"
    zip_tmp = APP_DATA_DIR / "browser.zip"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp, open(zip_tmp, "wb") as f:
            f.write(resp.read())

        with zipfile.ZipFile(zip_tmp, "r") as z:
            z.extractall(VENCORD_DIST_DIR)

        if zip_tmp.exists():
            zip_tmp.unlink()

        logging.info("Successfully downloaded and extracted Vencord dist files.")
        return PATCHER_JS
    except Exception as e:
        logging.error(f"Failed to auto-download Vencord dist files: {e}")
        return PATCHER_JS


def get_fake_deafen_payload() -> str:
    """Reads the FakeDeafen+ script from tools/fake_deafen_plus.js."""
    js_file = Path(__file__).parent / "tools" / "fake_deafen_plus.js"
    if js_file.exists():
        return js_file.read_text(encoding="utf-8")
    return ""


def inject_fake_deafen(dist_dir: Path) -> bool:
    """Injects FakeDeafen+ into Vencord's renderer.js so it runs automatically in Discord."""
    renderer_path = dist_dir / "renderer.js"
    if not renderer_path.exists():
        return False
    try:
        content = renderer_path.read_text(encoding="utf-8")
        marker = "/* === VencordGracefulPatch: FakeDeafen+ Integration === */"
        if marker in content:
            return True
        payload = get_fake_deafen_payload()
        if not payload:
            return False
        renderer_path.write_text(content.rstrip() + "\n\n" + marker + "\n" + payload + "\n", encoding="utf-8")
        logging.info("Injected FakeDeafen+ into Vencord renderer.js")
        return True
    except Exception as e:
        logging.error(f"Failed to inject FakeDeafen+: {e}")
        return False


def remove_fake_deafen(dist_dir: Path) -> bool:
    """Removes FakeDeafen+ from Vencord's renderer.js if disabled."""
    renderer_path = dist_dir / "renderer.js"
    if not renderer_path.exists():
        return False
    try:
        content = renderer_path.read_text(encoding="utf-8")
        marker = "/* === VencordGracefulPatch: FakeDeafen+ Integration === */"
        if marker in content:
            clean = content.split(marker)[0].rstrip()
            renderer_path.write_text(clean + "\n", encoding="utf-8")
            logging.info("Removed FakeDeafen+ from renderer.js")
            return True
        return False
    except Exception:
        return False


def patch_voice_index_js(base_path: Path) -> bool:
    """Ensures setOnSpeakingCallback is hooked in discord_voice/index.js for complete speaking ring suppression."""
    for idx_file in base_path.glob("app-*/modules/discord_voice-*/discord_voice/index.js"):
        try:
            content = idx_file.read_text(encoding="utf-8")
            marker = "__fakeDeafenActive"
            if marker in content:
                continue
            target = "setOnSpeakingCallback: (callback) => instance.setOnSpeakingCallback(callback),"
            replacement = (
                "setOnSpeakingCallback: (callback) =>\n"
                "      instance.setOnSpeakingCallback((speaking) => {\n"
                "        if (global.__fakeDeafenActive || (typeof window !== 'undefined' && window.__fakeDeafenActive)) {\n"
                "          callback(0);\n"
                "          return;\n"
                "        }\n"
                "        callback(speaking);\n"
                "      }),"
            )
            if target in content:
                idx_file.write_text(content.replace(target, replacement, 1), encoding="utf-8")
                logging.info(f"Hooked setOnSpeakingCallback in {idx_file.name}")
        except Exception as e:
            logging.error(f"Failed to hook {idx_file}: {e}")
    return True


def patch_version_natively(version_dir: Path) -> bool:
    """
    Applies the Vencord patch natively in Python:
    1. Renames resources/app.asar -> resources/_app.asar
    2. Writes custom 219-byte Vencord loader to resources/app.asar
    """
    resources = version_dir / "resources"
    app_asar = resources / "app.asar"
    orig_asar = resources / "_app.asar"

    if not app_asar.exists():
        logging.warning(f"Cannot patch {version_dir.name}: app.asar does not exist yet.")
        return False

    patcher_path = ensure_vencord_dist()
    if not patcher_path.exists():
        logging.error(f"Cannot patch: {patcher_path} is missing.")
        return False

    try:
        # Step 1: Backup original app.asar
        if not orig_asar.exists():
            app_asar.rename(orig_asar)
            logging.info(f"Backed up original app.asar -> _app.asar ({version_dir.name})")

        # Step 2: Write custom Vencord ASAR
        asar_bytes = create_vencord_asar(str(patcher_path))
        app_asar.write_bytes(asar_bytes)
        logging.info(f"Wrote native Vencord loader to app.asar ({version_dir.name})")
        return True
    except Exception as e:
        logging.error(f"Native patch failed for {version_dir.name}: {e}")
        # Rollback if needed
        if orig_asar.exists() and not app_asar.exists():
            try:
                orig_asar.rename(app_asar)
            except Exception:
                pass
        return False


def unpatch_version_natively(version_dir: Path) -> bool:
    """Restores Discord to stock by deleting loader and renaming _app.asar -> app.asar."""
    resources = version_dir / "resources"
    app_asar = resources / "app.asar"
    orig_asar = resources / "_app.asar"

    if not orig_asar.exists():
        logging.info(f"{version_dir.name} is not patched with Vencord.")
        return True

    try:
        if app_asar.exists():
            app_asar.unlink()
        orig_asar.rename(app_asar)
        logging.info(f"Successfully unpatched {version_dir.name} (restored original app.asar).")
        return True
    except Exception as e:
        logging.error(f"Failed to unpatch {version_dir.name}: {e}")
        return False


def check_branch_status(branch_key: str) -> dict:
    """Inspects a Discord branch directory and checks for unpatched versions."""
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
            "path": d,
            "version": d.name.replace("app-", ""),
            "has_resources": resources.exists(),
            "has_app_asar": app_asar.exists(),
            "has_orig_asar": orig_asar.exists(),
            "is_patched": orig_asar.exists(),
            "ready_for_patch": False
        }

        if app_asar.exists() and not orig_asar.exists():
            ver_info["ready_for_patch"] = True
            result["unpatched_versions"].append(d)

        result["versions"].append(ver_info)

    return result


def get_branch_unpatched_voice(branch_key: str) -> list:
    """Finds any discord_voice.node in this branch that is ready to be patched."""
    try:
        import voice_patcher
        if branch_key not in DISCORD_BRANCHES:
            return []
        branch_folder = Path(LOCAL_APPDATA) / DISCORD_BRANCHES[branch_key]["folder"]
        if not branch_folder.exists():
            return []
        unpatched = []
        for vnode in branch_folder.glob("app-*/modules/discord_voice-*/discord_voice/discord_voice.node"):
            v_info = voice_patcher.inspect_voice_module(vnode)
            if v_info.get("status") in ["UNPATCHED_READY", "UNRECOGNIZED_SIGNATURE"]:
                unpatched.append(vnode)
        return unpatched
    except Exception as e:
        logging.debug(f"Error checking branch voice: {e}")
        return []


def run_single_check(config: dict, waiting_state: dict) -> None:
    """Performs one scan across all monitored Discord branches."""
    monitored = config.get("monitored_branches", ["stable"])
    stereo_enabled = config.get("enable_stereo_patch", True)

    for branch_key in monitored:
        if branch_key not in DISCORD_BRANCHES:
            continue

        status = check_branch_status(branch_key)
        if not status["exists"]:
            continue

        unpatched_dirs = status["unpatched_versions"]
        unpatched_voice = get_branch_unpatched_voice(branch_key) if stereo_enabled else []

        # If neither Vencord nor Voice needs work, reset waiting state and continue
        if not unpatched_dirs and not unpatched_voice:
            if waiting_state.get(branch_key):
                waiting_state[branch_key] = False
            continue

        display_name = status["display_name"]
        is_running = status["is_running"]

        # If Discord is running: DO NOT TOUCH IT. Wait gracefully.
        if is_running:
            if not waiting_state.get(branch_key):
                reasons = []
                if unpatched_dirs:
                    reasons.append(f"Vencord: {', '.join(d.name for d in unpatched_dirs)}")
                if unpatched_voice:
                    reasons.append(f"Stereo Audio: {len(unpatched_voice)} module(s)")
                logging.info(
                    f"[{display_name}] Update pending ({'; '.join(reasons)}). "
                    f"Discord is currently running. Waiting gracefully for Discord to close..."
                )
                waiting_state[branch_key] = True
            continue

        # Discord is closed!
        if waiting_state.get(branch_key):
            cooldown = config.get("cooldown_after_close_seconds", 5)
            logging.info(f"[{display_name}] Discord has closed! Waiting {cooldown}s cooldown for file locks...")
            time.sleep(cooldown)

        # 1. Patch Vencord if needed
        if unpatched_dirs:
            logging.info(f"[{display_name}] Applying native Vencord patch for: {', '.join(d.name for d in unpatched_dirs)}...")
            all_patched = True
            for d in unpatched_dirs:
                if not patch_version_natively(d):
                    all_patched = False

            if all_patched:
                logging.info(f"[{display_name}] Successfully patched all versions with Vencord!")
                if config.get("show_notifications", True):
                    send_windows_notification(
                        "Vencord Auto-Patched",
                        f"{display_name} updated and was successfully patched with Vencord!"
                    )
            else:
                logging.warning(f"[{display_name}] Some Vencord versions could not be patched yet.")

        # Ensure FakeDeafen+ is maintained in Vencord if enabled
        if config.get("enable_fake_deafen", True):
            inject_fake_deafen(VENCORD_DIST_DIR)
            patch_voice_index_js(status["base_path"])
        else:
            remove_fake_deafen(VENCORD_DIST_DIR)

        # 2. Patch Stereo Voice Module if enabled
        if stereo_enabled and unpatched_voice:
            logging.info(f"[{display_name}] Applying dynamic Stereo Voice patch...")
            try:
                import voice_patcher
                for vnode in unpatched_voice:
                    voice_patcher.patch_voice_module(vnode, notify=config.get("show_notifications", True))
            except Exception as ve:
                logging.error(f"Failed to patch voice module: {ve}")

        # Optional relaunch
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


def unpatch_all(config: dict):
    """Unpatches all installed versions across all monitored branches."""
    setup_logging()
    logging.info("Unpatching all Discord installations...")
    monitored = config.get("monitored_branches", ["stable"])

    for branch_key in monitored:
        if branch_key not in DISCORD_BRANCHES:
            continue
        status = check_branch_status(branch_key)
        if not status["exists"]:
            continue

        if status["is_running"]:
            print(f"[!] Please close {status['display_name']} before unpatching.")
            continue

        for v in status["versions"]:
            if v["is_patched"]:
                unpatch_version_natively(v["path"])
                print(f"[OK] Unpatched {status['display_name']} ({v['name']})")

    # Also restore stock voice modules
    try:
        import voice_patcher
        for vnode in voice_patcher.find_voice_modules():
            voice_patcher.unpatch_voice_module(vnode, notify=config.get("show_notifications", True))
    except Exception as e:
        logging.debug(f"Voice unpatch error: {e}")


def daemon_loop():
    """Continuous background loop."""
    setup_logging()
    logging.info("Vencord Graceful Patcher (Native Mode) daemon started.")
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

    print("\n=======================================================")
    print("       VENCORD GRACEFUL PATCHER (NATIVE MODE)          ")
    print("=======================================================\n")
    print(f"Patcher Mode     : Native Python ASAR Injection (Zero Binaries)")
    print(f"Vencord Patcher  : {PATCHER_JS if PATCHER_JS.exists() else 'NOT FOUND'}")
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

        if config.get("enable_stereo_patch", True):
            try:
                import voice_patcher
                branch_folder = Path(LOCAL_APPDATA) / DISCORD_BRANCHES[branch_key]["folder"]
                voice_nodes = list(branch_folder.glob("app-*/modules/discord_voice-*/discord_voice/discord_voice.node"))
                if voice_nodes:
                    v_info = voice_patcher.inspect_voice_module(voice_nodes[0])
                    v_state = v_info.get("status")
                    if v_state == "PATCHED":
                        v_str = f"PATCHED (Stereo 2-Ch / 384kbps) [{v_info.get('profile')}]"
                    elif v_state == "UNPATCHED_READY":
                        v_str = "UNPATCHED (Original Mono - Ready to patch)"
                    elif v_state == "UNRECOGNIZED_SIGNATURE":
                        v_str = "UNRECOGNIZED SIGNATURE (Safe Fail-Safe Active)"
                    else:
                        v_str = str(v_state)
                    print(f"  Voice Engine: {v_str}")
            except Exception:
                pass

        if config.get("enable_fake_deafen", True):
            renderer_path = VENCORD_DIST_DIR / "renderer.js"
            try:
                injected = renderer_path.exists() and "FakeDeafen+ Integration" in renderer_path.read_text(encoding="utf-8")
                print(f"  FakeDeafen+ : {'ACTIVE (Button & F8 / Ctrl+Shift+Q Active)' if injected else 'Not Injected'}")
            except Exception:
                pass
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vencord Graceful Patcher")
    parser.add_argument("--status", action="store_true", help="Print current status and exit")
    parser.add_argument("--check-once", action="store_true", help="Perform a single check/patch and exit")
    parser.add_argument("--unpatch", action="store_true", help="Unpatch Vencord and restore original Discord files")
    parser.add_argument("--daemon", action="store_true", help="Run in continuous background daemon mode")
    args = parser.parse_args()

    cfg = load_config()

    if args.status:
        print_status()
    elif args.unpatch:
        unpatch_all(cfg)
    elif args.check_once:
        setup_logging()
        run_single_check(cfg, {})
    else:
        daemon_loop()
