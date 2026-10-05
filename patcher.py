"""
Vencord Graceful Patcher
A standalone, Discord-friendly native auto-patcher for Vencord.
Waits gracefully until Discord closes before applying patches.
Zero external binaries. Zero voice call interruptions.

Usage:
    python patcher.py --daemon      Run the background service (what the Startup launcher does)
    python patcher.py --status      Print the current state of every Discord branch
    python patcher.py --check-once  Run a single check/patch pass and exit
    python patcher.py --unpatch     Stop the service and restore Discord to stock
    python patcher.py --stop        Stop the background service
"""

import os
import re
import sys
import time
import json
import struct
import shutil
import hashlib
import logging
import argparse
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional

from patcher_common import (
    IS_WINDOWS,
    CREATE_NO_WINDOW,
    LOCAL_APPDATA,
    APPDATA,
    APP_DATA_DIR,
    LOG_FILE,
    DISCORD_BRANCHES,
    send_windows_notification,
    get_running_processes,
    load_state,
    save_state,
    atomic_write_bytes,
    atomic_write_text,
    acquire_single_instance,
    is_daemon_running,
    stop_daemon_processes,
)
import voice_patcher

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
FAKE_DEAFEN_JS = SCRIPT_DIR / "tools" / "fake_deafen_plus.js"

VENCORD_DATA_DIR = Path(APPDATA) / "Vencord"
VENCORD_DIST_DIR = VENCORD_DATA_DIR / "dist"
PATCHER_JS = VENCORD_DIST_DIR / "patcher.js"
RENDERER_JS = VENCORD_DIST_DIR / "renderer.js"

# Vencord publishes its desktop build as individual files on the rolling "devbuild" release.
VENCORD_RELEASE_BASE = "https://github.com/Vendicated/Vencord/releases/download/devbuild/"
VENCORD_DIST_FILES = ["patcher.js", "preload.js", "renderer.js", "renderer.css"]

DOWNLOAD_RETRY_SECONDS = 300      # do not hammer GitHub when offline
PATCH_FAILURE_BACKOFF_SECONDS = 60  # pause a branch after a failed patch attempt
MIN_CHECK_INTERVAL_SECONDS = 5

FAKE_DEAFEN_MARKER_PREFIX = "/* === VencordGracefulPatch: FakeDeafen+ Integration"

DEFAULT_CONFIG = {
    "check_interval_seconds": 10,
    "cooldown_after_close_seconds": 5,
    "show_notifications": True,
    "enable_stereo_patch": True,
    "enable_fake_deafen": True,
    "relaunch_discord_after_patch": False,
    "monitored_branches": ["stable", "ptb", "canary", "development"],
}
_BOOL_KEYS = ("show_notifications", "enable_stereo_patch", "enable_fake_deafen", "relaunch_discord_after_patch")


# ---------------------------------------------------------------------------
# Logging & configuration
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    """Sets up logging to the log file (and the console when one is attached)."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
            backup_log = APP_DATA_DIR / "patcher.log.old"
            if backup_log.exists():
                backup_log.unlink()
            LOG_FILE.rename(backup_log)
    except Exception:
        pass

    handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if sys.stdout is not None:  # pythonw.exe has no console
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def _as_int(value, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def load_config(path: Optional[Path] = None) -> dict:
    """Loads configuration with fallback defaults and basic validation."""
    path = Path(path or CONFIG_FILE)
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
            else:
                logging.warning("config.json must contain a JSON object. Using defaults.")
        except Exception as e:
            logging.warning(f"Failed to load config.json: {e}. Using defaults.")

    cfg["check_interval_seconds"] = _as_int(cfg.get("check_interval_seconds"), 10, MIN_CHECK_INTERVAL_SECONDS)
    cfg["cooldown_after_close_seconds"] = _as_int(cfg.get("cooldown_after_close_seconds"), 5, 0)
    for key in _BOOL_KEYS:
        cfg[key] = bool(cfg.get(key, DEFAULT_CONFIG[key]))

    branches = cfg.get("monitored_branches")
    if not isinstance(branches, list):
        branches = list(DEFAULT_CONFIG["monitored_branches"])
    valid = []
    for b in branches:
        key = str(b).lower().strip()
        if key in DISCORD_BRANCHES:
            if key not in valid:
                valid.append(key)
        else:
            logging.warning(f"Unknown branch {b!r} in monitored_branches (valid: {', '.join(DISCORD_BRANCHES)}).")
    cfg["monitored_branches"] = valid
    return cfg


def config_signature(path: Optional[Path] = None):
    """Cheap fingerprint of config.json so the daemon can hot-reload it."""
    try:
        st = Path(path or CONFIG_FILE).stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Vencord loader (ASAR) & dist files
# ---------------------------------------------------------------------------

def create_vencord_asar(patcher_path: str) -> bytes:
    """
    Constructs a byte-exact Electron ASAR archive containing package.json and
    an index.js that hands control to Vencord's patcher.js.
    """
    package_json = '{\n\t"name": "discord",\n\t"main": "index.js"\n}'
    index_js = f"require({json.dumps(patcher_path)})"

    index_bytes = index_js.encode("utf-8")
    pkg_bytes = package_json.encode("utf-8")

    header = {
        "files": {
            "index.js": {"size": len(index_bytes), "offset": "0"},
            "package.json": {"size": len(pkg_bytes), "offset": str(len(index_bytes))},
        }
    }

    hdr_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    hdr_len = len(hdr_bytes)

    aligned = (hdr_len + 3) & ~3           # pickle payloads are 4-byte aligned
    hdr_sz = aligned + 8                   # size of the header pickle
    obj_sz = aligned + 4                   # size of the string pickle payload
    padded_header = hdr_bytes + b"\0" * (aligned - hdr_len)

    prefix = struct.pack("<IIII", 4, hdr_sz, obj_sz, hdr_len)
    return prefix + padded_header + index_bytes + pkg_bytes


def dist_is_valid(dist_dir: Optional[Path] = None) -> bool:
    """True when every Vencord desktop dist file is present and patcher.js looks genuine."""
    dist_dir = Path(dist_dir or VENCORD_DIST_DIR)
    try:
        for name in VENCORD_DIST_FILES:
            if (dist_dir / name).stat().st_size == 0:
                return False
        return b"_app.asar" in (dist_dir / "patcher.js").read_bytes()
    except OSError:
        return False


def download_file(url: str, dest: Path, timeout: int = 60) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "VencordGracefulPatch"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
    if dest.stat().st_size == 0:
        raise ValueError(f"Downloaded file is empty: {url}")


_dist_download = {"last_attempt": None}


def ensure_vencord_dist() -> bool:
    """
    Ensures Vencord's desktop dist files exist. Downloads the latest Vencord
    build from GitHub if they are missing. Returns True when usable.
    """
    if dist_is_valid():
        return True

    now = time.monotonic()
    last = _dist_download["last_attempt"]
    if last is not None and now - last < DOWNLOAD_RETRY_SECONDS:
        return False
    _dist_download["last_attempt"] = now

    logging.info("Vencord dist files are missing or incomplete. Downloading the latest Vencord build from GitHub...")
    staged = []
    try:
        VENCORD_DIST_DIR.mkdir(parents=True, exist_ok=True)
        for name in VENCORD_DIST_FILES:
            tmp = VENCORD_DIST_DIR / (name + ".download")
            download_file(VENCORD_RELEASE_BASE + name, tmp)
            staged.append((tmp, VENCORD_DIST_DIR / name))

        patcher_tmp = next(t for t, final in staged if final.name == "patcher.js")
        if b"_app.asar" not in patcher_tmp.read_bytes():
            raise ValueError("Downloaded patcher.js does not look like the Vencord desktop patcher.")

        for tmp, final in staged:
            os.replace(tmp, final)
        logging.info(f"Successfully downloaded Vencord dist files to {VENCORD_DIST_DIR}")
    except Exception as e:
        logging.error(f"Failed to download Vencord: {e}. Will retry in {DOWNLOAD_RETRY_SECONDS // 60} minutes.")
        for tmp, _ in staged:
            try:
                tmp.unlink()
            except OSError:
                pass
        return False

    return dist_is_valid()


# ---------------------------------------------------------------------------
# FakeDeafen+ integration (appended to Vencord's renderer.js)
# ---------------------------------------------------------------------------

_payload_cache = {"sig": None, "text": "", "hash": ""}
_renderer_cache = {}
_warned_once = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned_once:
        _warned_once.add(key)
        logging.warning(message)


def get_fake_deafen_payload(js_file: Optional[Path] = None) -> str:
    """Reads tools/fake_deafen_plus.js (cached by mtime)."""
    js_file = Path(js_file or FAKE_DEAFEN_JS)
    try:
        st = js_file.stat()
    except OSError:
        return ""
    sig = (str(js_file), st.st_size, st.st_mtime_ns)
    if _payload_cache["sig"] != sig:
        try:
            text = js_file.read_text(encoding="utf-8").strip()
        except Exception as e:
            logging.error(f"Could not read {js_file}: {e}")
            return ""
        _payload_cache.update(sig=sig, text=text, hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])
    return _payload_cache["text"]


def fake_deafen_marker(payload: str) -> str:
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{FAKE_DEAFEN_MARKER_PREFIX} [sha256:{digest}] === */"


def renderer_injection_state(renderer_path: Path, payload: str) -> str:
    """Returns 'missing', 'current' or 'stale' for the FakeDeafen+ block in renderer.js."""
    renderer_path = Path(renderer_path)
    try:
        st = renderer_path.stat()
    except OSError:
        return "missing"
    sig = (st.st_size, st.st_mtime_ns, fake_deafen_marker(payload))
    cached = _renderer_cache.get(str(renderer_path))
    if cached and cached[0] == sig:
        return cached[1]

    try:
        content = renderer_path.read_text(encoding="utf-8")
    except Exception as e:
        logging.error(f"Could not read {renderer_path}: {e}")
        return "missing"

    if fake_deafen_marker(payload) in content:
        state = "current"
    elif FAKE_DEAFEN_MARKER_PREFIX in content:
        state = "stale"
    else:
        state = "missing"
    _renderer_cache[str(renderer_path)] = (sig, state)
    return state


def _strip_injection(content: str) -> str:
    idx = content.find(FAKE_DEAFEN_MARKER_PREFIX)
    return content if idx == -1 else content[:idx].rstrip() + "\n"


def inject_fake_deafen(dist_dir: Path, payload: Optional[str] = None) -> bool:
    """Appends (or refreshes) FakeDeafen+ at the end of Vencord's renderer.js."""
    renderer_path = Path(dist_dir) / "renderer.js"
    if not renderer_path.exists():
        return False
    payload = payload if payload is not None else get_fake_deafen_payload()
    if not payload:
        _warn_once("payload", f"FakeDeafen+ payload not found at {FAKE_DEAFEN_JS}; skipping injection.")
        return False
    try:
        content = _strip_injection(renderer_path.read_text(encoding="utf-8"))
        new_content = content.rstrip() + "\n\n" + fake_deafen_marker(payload) + "\n" + payload + "\n"
        atomic_write_text(renderer_path, new_content)
        _renderer_cache.pop(str(renderer_path), None)
        logging.info("Injected FakeDeafen+ into Vencord renderer.js (takes effect on next Discord start).")
        return True
    except Exception as e:
        logging.error(f"Failed to inject FakeDeafen+: {e}")
        return False


def remove_fake_deafen(dist_dir: Path) -> bool:
    """Removes FakeDeafen+ from Vencord's renderer.js. Returns True if something was removed."""
    renderer_path = Path(dist_dir) / "renderer.js"
    if not renderer_path.exists():
        return False
    try:
        content = renderer_path.read_text(encoding="utf-8")
        if FAKE_DEAFEN_MARKER_PREFIX not in content:
            return False
        atomic_write_text(renderer_path, _strip_injection(content))
        _renderer_cache.pop(str(renderer_path), None)
        logging.info("Removed FakeDeafen+ from Vencord renderer.js.")
        return True
    except Exception as e:
        logging.error(f"Failed to remove FakeDeafen+: {e}")
        return False


def sync_fake_deafen(enabled: bool, dist_dir: Optional[Path] = None) -> None:
    """
    Keeps renderer.js in the desired state every tick. This is what survives
    Vencord's own self-updater overwriting renderer.js, and lets config changes
    or payload updates apply without waiting for the next Discord update.
    """
    dist_dir = Path(dist_dir or VENCORD_DIST_DIR)
    renderer_path = dist_dir / "renderer.js"
    if not renderer_path.exists():
        return
    payload = get_fake_deafen_payload() if enabled else ""
    state = renderer_injection_state(renderer_path, payload)
    if enabled:
        if payload and state != "current":
            inject_fake_deafen(dist_dir, payload)
        elif not payload:
            _warn_once("payload", f"FakeDeafen+ payload not found at {FAKE_DEAFEN_JS}; skipping injection.")
    elif state != "missing":
        remove_fake_deafen(dist_dir)


_VOICE_INDEX_HOOK_RE = re.compile(
    r"setOnSpeakingCallback:\s*\(callback\)\s*=>\s*instance\.setOnSpeakingCallback\(\(speaking\)\s*=>\s*\{[\s\S]*?callback\(speaking\);\s*\}\),"
)


def restore_stock_voice_index_js(base_path: Path) -> None:
    """
    Legacy clean-up: an earlier build of this tool hooked discord_voice/index.js.
    That hook broke speaking detection, so make sure it is gone.
    """
    for idx_file in Path(base_path).glob("app-*/modules/discord_voice-*/discord_voice/index.js"):
        try:
            content = idx_file.read_text(encoding="utf-8")
            if "__fakeDeafenActive" not in content:
                continue
            cleaned = _VOICE_INDEX_HOOK_RE.sub(
                "setOnSpeakingCallback: (callback) => instance.setOnSpeakingCallback(callback),", content
            )
            if cleaned != content:
                atomic_write_text(idx_file, cleaned)
                logging.info(f"Restored stock setOnSpeakingCallback in {idx_file}")
        except Exception as e:
            logging.error(f"Failed to check {idx_file}: {e}")


# ---------------------------------------------------------------------------
# Native Vencord patch / unpatch
# ---------------------------------------------------------------------------

def is_vencord_loader(asar_path: Path) -> bool:
    """Checks if an asar file is a Vencord loader rather than stock Discord."""
    asar_path = Path(asar_path)
    try:
        # A Vencord loader is a tiny pointer (< 10 KB) that requires patcher.js
        if not asar_path.is_file() or asar_path.stat().st_size > 10240:
            return False
        content = asar_path.read_bytes().lower()
        return b"patcher.js" in content or b"vencord" in content
    except Exception:
        return False


def patch_version_natively(version_dir: Path) -> bool:
    """
    Applies the Vencord patch natively in Python:
    1. Renames resources/app.asar -> resources/_app.asar
    2. Writes the tiny Vencord loader to resources/app.asar
    """
    version_dir = Path(version_dir)
    resources = version_dir / "resources"
    app_asar = resources / "app.asar"
    orig_asar = resources / "_app.asar"

    if not app_asar.exists():
        logging.warning(f"Cannot patch {version_dir.name}: app.asar does not exist yet.")
        return False

    if not ensure_vencord_dist():
        logging.error(f"Cannot patch {version_dir.name}: Vencord dist files are unavailable.")
        return False

    renamed = False
    try:
        if is_vencord_loader(app_asar) and orig_asar.exists():
            logging.info(f"{version_dir.name} is already patched.")
            return True

        if not is_vencord_loader(app_asar):
            # app.asar is stock Discord: it MUST become _app.asar. A stale _app.asar
            # (e.g. left behind by a folder migration) is replaced so _app.asar is
            # always the genuine stock asar of this exact version.
            if orig_asar.exists():
                orig_asar.unlink()
            app_asar.rename(orig_asar)
            renamed = True
            logging.info(f"Backed up stock app.asar -> _app.asar ({version_dir.name})")
        elif not orig_asar.exists():
            _warn_once(f"no-backup:{version_dir}",
                       f"Cannot patch {version_dir.name}: app.asar is already a loader but _app.asar is missing. "
                       "Reinstall Discord to repair this version.")
            return False

        atomic_write_bytes(app_asar, create_vencord_asar(str(PATCHER_JS)))
        logging.info(f"Wrote native Vencord loader to app.asar ({version_dir.name})")
        return True
    except Exception as e:
        logging.error(f"Native patch failed for {version_dir.name}: {e}")
        if renamed and orig_asar.exists() and not app_asar.exists():
            try:
                orig_asar.rename(app_asar)
                logging.info(f"Rolled back {version_dir.name} to stock app.asar.")
            except Exception as rb:
                logging.error(f"Rollback failed for {version_dir.name}: {rb}")
        return False


def unpatch_version_natively(version_dir: Path) -> bool:
    """Restores Discord to stock by deleting the loader and renaming _app.asar -> app.asar."""
    version_dir = Path(version_dir)
    resources = version_dir / "resources"
    app_asar = resources / "app.asar"
    orig_asar = resources / "_app.asar"

    if not orig_asar.exists():
        logging.info(f"{version_dir.name} is not patched with Vencord.")
        return True

    try:
        if app_asar.exists():
            if not is_vencord_loader(app_asar):
                logging.warning(f"{version_dir.name}: app.asar is not a Vencord loader; leaving it untouched.")
                return False
            app_asar.unlink()
        orig_asar.rename(app_asar)
        logging.info(f"Successfully unpatched {version_dir.name} (restored original app.asar).")
        return True
    except Exception as e:
        logging.error(f"Failed to unpatch {version_dir.name}: {e}")
        return False


# ---------------------------------------------------------------------------
# Branch inspection
# ---------------------------------------------------------------------------

def get_version_tuple(dir_name: str) -> tuple:
    """Extracts a numeric tuple from 'app-1.0.9259' -> (1, 0, 9259)."""
    try:
        return tuple(int(p) for p in dir_name.replace("app-", "").split("."))
    except Exception:
        return (0, 0, 0)


def check_branch_status(branch_key: str) -> dict:
    """Inspects a Discord branch directory and lists installed / unpatched versions."""
    info = DISCORD_BRANCHES[branch_key]
    base_path = Path(LOCAL_APPDATA) / info["folder"]
    result = {
        "exists": base_path.is_dir(),
        "branch": branch_key,
        "display_name": info["display_name"],
        "base_path": base_path,
        "process": info["process"],
        "versions": [],
        "unpatched_versions": [],
    }
    if not result["exists"]:
        return result

    try:
        app_dirs = [d for d in base_path.iterdir() if d.is_dir() and d.name.startswith("app-")]
    except OSError as e:
        logging.error(f"Cannot list {base_path}: {e}")
        return result
    app_dirs.sort(key=lambda d: get_version_tuple(d.name))

    for d in app_dirs:
        resources = d / "resources"
        app_asar = resources / "app.asar"
        orig_asar = resources / "_app.asar"
        is_patched = is_vencord_loader(app_asar) and orig_asar.exists()

        ver_info = {
            "name": d.name,
            "path": d,
            "version": d.name.replace("app-", ""),
            "has_app_asar": app_asar.exists(),
            "has_orig_asar": orig_asar.exists(),
            "is_patched": is_patched,
            "ready_for_patch": app_asar.exists() and not is_patched,
        }
        if ver_info["ready_for_patch"]:
            result["unpatched_versions"].append(d)
        result["versions"].append(ver_info)

    return result


def get_branch_voice_status(branch_key: str) -> dict:
    """Classifies every discord_voice.node of a branch as ready / unrecognized / patched."""
    status = {"ready": [], "unrecognized": [], "patched": []}
    branch_folder = Path(LOCAL_APPDATA) / DISCORD_BRANCHES[branch_key]["folder"]
    if not branch_folder.is_dir():
        return status
    try:
        for vnode in branch_folder.glob(voice_patcher.VOICE_NODE_GLOB):
            v_info = voice_patcher.inspect_voice_module(vnode)
            state = v_info.get("status")
            if state == voice_patcher.STATUS_READY:
                status["ready"].append(vnode)
            elif state == voice_patcher.STATUS_UNRECOGNIZED:
                status["unrecognized"].append(v_info)
            elif state == voice_patcher.STATUS_PATCHED:
                status["patched"].append(vnode)
    except Exception as e:
        logging.debug(f"Error checking voice modules for {branch_key}: {e}")
    return status


def notify_unrecognized_voice(infos: list, config: dict, runtime: dict) -> None:
    """Fail-safe alert, sent once per unrecognized voice build (persisted across restarts)."""
    state = runtime.setdefault("state", {})
    seen = state.setdefault("notified_voice_signatures", [])
    changed = False
    for info in infos:
        md5 = info.get("md5")
        if not md5 or md5 in seen:
            continue
        logging.warning(
            f"[FAIL-SAFE] Unrecognized voice engine signature in {info.get('path')} (md5 {md5}). "
            "Leaving it stock so Discord cannot crash. Add a matching profile to audio_patterns.json to enable stereo."
        )
        if config.get("show_notifications", True):
            send_windows_notification(
                "Discord Audio Notice",
                "Discord updated to a new voice build. Stereo pattern not recognized; running safely in stock mode.",
            )
        seen.append(md5)
        changed = True
    if changed:
        del seen[:-20]
        save_state(state)


# ---------------------------------------------------------------------------
# Main check loop
# ---------------------------------------------------------------------------

def new_runtime() -> dict:
    return {"waiting": {}, "backoff": {}, "state": load_state()}


def run_single_check(config: dict, runtime: Optional[dict] = None) -> None:
    """Performs one scan across all monitored Discord branches."""
    runtime = runtime if runtime is not None else new_runtime()
    waiting = runtime.setdefault("waiting", {})
    backoff = runtime.setdefault("backoff", {})
    stereo_enabled = config.get("enable_stereo_patch", True)
    notify = config.get("show_notifications", True)

    # FakeDeafen+ lives in Vencord's renderer.js, which Vencord's own updater may
    # overwrite at any time, so keep it in sync independently of Discord updates.
    sync_fake_deafen(config.get("enable_fake_deafen", True))

    pending = []
    for branch_key in config.get("monitored_branches", []):
        if branch_key not in DISCORD_BRANCHES:
            continue
        status = check_branch_status(branch_key)
        if not status["exists"]:
            continue

        voice = get_branch_voice_status(branch_key) if stereo_enabled else {"ready": [], "unrecognized": []}
        if voice["unrecognized"]:
            notify_unrecognized_voice(voice["unrecognized"], config, runtime)

        if not status["unpatched_versions"] and not voice["ready"]:
            waiting.pop(branch_key, None)
            backoff.pop(branch_key, None)
            continue
        pending.append((status, voice["ready"]))

    if not pending:
        return

    running = get_running_processes()
    if running is None:
        _warn_once("snapshot", "Could not read the process list; leaving Discord untouched until it works again.")
        return

    for status, ready_voice in pending:
        branch_key = status["branch"]
        display_name = status["display_name"]
        unpatched_dirs = status["unpatched_versions"]

        # Discord is running: DO NOT TOUCH IT. Wait gracefully.
        if status["process"].lower() in running:
            if not waiting.get(branch_key):
                reasons = []
                if unpatched_dirs:
                    reasons.append(f"Vencord: {', '.join(d.name for d in unpatched_dirs)}")
                if ready_voice:
                    reasons.append(f"Stereo Audio: {len(ready_voice)} module(s)")
                logging.info(
                    f"[{display_name}] Update pending ({'; '.join(reasons)}). "
                    "Discord is currently running. Waiting gracefully for Discord to close..."
                )
                waiting[branch_key] = True
            continue

        last_failure = backoff.get(branch_key)
        if last_failure is not None and time.monotonic() - last_failure < PATCH_FAILURE_BACKOFF_SECONDS:
            continue

        if waiting.get(branch_key):
            cooldown = config.get("cooldown_after_close_seconds", 5)
            logging.info(f"[{display_name}] Discord has closed! Waiting {cooldown}s cooldown for file locks...")
            time.sleep(cooldown)
            running = get_running_processes()
            if running is None or status["process"].lower() in running:
                logging.info(f"[{display_name}] Discord was started again during the cooldown. Waiting...")
                continue

        patched_any = False
        failed = False

        # 1. Vencord
        if unpatched_dirs:
            logging.info(f"[{display_name}] Applying native Vencord patch for: {', '.join(d.name for d in unpatched_dirs)}...")
            results = [patch_version_natively(d) for d in unpatched_dirs]
            if all(results):
                patched_any = True
                logging.info(f"[{display_name}] Successfully patched all versions with Vencord!")
                if notify:
                    send_windows_notification(
                        "Vencord Auto-Patched",
                        f"{display_name} updated and was successfully patched with Vencord!",
                    )
            else:
                failed = True
                logging.warning(f"[{display_name}] Some versions could not be patched yet; will retry.")

        # 2. Stereo voice module
        if stereo_enabled and ready_voice:
            logging.info(f"[{display_name}] Applying dynamic Stereo Voice patch...")
            for vnode in ready_voice:
                if voice_patcher.patch_voice_module(vnode, notify=notify, running=running):
                    patched_any = True
                else:
                    failed = True

        restore_stock_voice_index_js(status["base_path"])

        if failed:
            backoff[branch_key] = time.monotonic()
        else:
            backoff.pop(branch_key, None)

        if patched_any and config.get("relaunch_discord_after_patch", False):
            relaunch_discord(status)

        waiting[branch_key] = False


def relaunch_discord(status: dict) -> None:
    try:
        update_exe = status["base_path"] / "Update.exe"
        if not update_exe.exists():
            return
        kwargs = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}
        subprocess.Popen([str(update_exe), "--processStart", status["process"]], **kwargs)
        logging.info(f"[{status['display_name']}] Relaunched Discord.")
    except Exception as e:
        logging.error(f"Failed to relaunch Discord: {e}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def stop_daemon(quiet: bool = False) -> bool:
    """Stops any running background daemon. Returns True if one was stopped."""
    pids = stop_daemon_processes()
    for _ in range(20):
        if not is_daemon_running():
            break
        time.sleep(0.25)
    if pids:
        msg = f"Stopped background patcher (PID {', '.join(str(p) for p in pids)})."
        logging.info(msg)
        if not quiet:
            print(f"[OK] {msg}")
    elif not quiet:
        print("[i] No background patcher was running.")
    return bool(pids)


def unpatch_all(config: dict) -> bool:
    """Stops the daemon and restores every Discord installation to stock."""
    logging.info("Unpatching all Discord installations...")
    stop_daemon(quiet=True)

    running = get_running_processes()
    all_ok = True
    for branch_key in DISCORD_BRANCHES:
        status = check_branch_status(branch_key)
        if not status["exists"]:
            continue
        if running is None or status["process"].lower() in running:
            print(f"[!] Please close {status['display_name']} before unpatching.")
            all_ok = False
            continue
        for v in status["versions"]:
            if not v["is_patched"]:
                continue
            if unpatch_version_natively(v["path"]):
                print(f"[OK] Unpatched {status['display_name']} ({v['name']})")
            else:
                print(f"[!] Could not unpatch {status['display_name']} ({v['name']}); see log.")
                all_ok = False

    if remove_fake_deafen(VENCORD_DIST_DIR):
        print("[OK] Removed FakeDeafen+ from Vencord.")

    voice_patcher.unpatch_all_installed_branches(notify=config.get("show_notifications", True))

    print()
    print("Discord is back to stock. The background patcher has been stopped;")
    print("run install.bat again whenever you want automatic patching back.")
    return all_ok


def daemon_loop() -> None:
    """Continuous background loop."""
    if not acquire_single_instance():
        # A daemon that is being replaced may still be shutting down.
        for _ in range(5):
            time.sleep(1)
            if acquire_single_instance():
                break
        else:
            logging.info("Another instance of the background patcher is already running. Exiting.")
            return

    logging.info(f"Vencord Graceful Patcher daemon started (PID {os.getpid()}).")
    config = load_config()
    cfg_sig = config_signature()
    runtime = new_runtime()

    while True:
        new_sig = config_signature()
        if new_sig != cfg_sig:
            cfg_sig = new_sig
            config = load_config()
            logging.info("config.json changed; configuration reloaded.")

        try:
            run_single_check(config, runtime)
        except Exception as e:
            logging.error(f"Unexpected error in daemon loop: {e}", exc_info=True)

        time.sleep(config.get("check_interval_seconds", 10))


def print_status() -> None:
    """Prints human-readable status for all branches in the terminal."""
    config = load_config()
    running = get_running_processes()
    payload = get_fake_deafen_payload()

    print("\n=======================================================")
    print("       VENCORD GRACEFUL PATCHER (NATIVE MODE)          ")
    print("=======================================================\n")
    print(f"Background Service: {'RUNNING' if is_daemon_running() else 'NOT RUNNING (run install.bat)'}")
    print(f"Vencord Files     : {VENCORD_DIST_DIR if dist_is_valid() else 'MISSING (auto-downloaded on next patch)'}")
    if config["enable_fake_deafen"]:
        state = renderer_injection_state(RENDERER_JS, payload) if RENDERER_JS.exists() else "missing"
        fd = {"current": "ACTIVE (button in call bar, F8 / Ctrl+Shift+Q)",
              "stale": "OUTDATED (refreshed automatically by the service)",
              "missing": "Not injected yet (done automatically by the service)"}[state]
    else:
        fd = "Disabled in config.json"
    print(f"FakeDeafen+       : {fd}")
    print(f"Stereo Patch      : {'Enabled' if config['enable_stereo_patch'] else 'Disabled in config.json'}")
    print(f"Log File          : {LOG_FILE}")
    print(f"Check Interval    : {config['check_interval_seconds']}s\n")

    for branch_key in config["monitored_branches"]:
        status = check_branch_status(branch_key)
        print(f"[{status['display_name']}]")
        if not status["exists"]:
            print("  Status: Not installed\n")
            continue

        if running is None:
            run_str = "UNKNOWN (could not read process list)"
        elif status["process"].lower() in running:
            run_str = "YES (Will wait for close)"
        else:
            run_str = "NO (Safe to patch)"
        print(f"  Path   : {status['base_path']}")
        print(f"  Running: {run_str}")
        print("  Installed Versions:")
        if not status["versions"]:
            print("    (none)")
        for v in status["versions"]:
            if not v["has_app_asar"]:
                state = "DOWNLOADING / INCOMPLETE"
            elif v["is_patched"]:
                state = "PATCHED (Vencord active)"
            else:
                state = "UNPATCHED (Needs Vencord)"
            print(f"    - {v['name']}: {state}")

        if config["enable_stereo_patch"]:
            # versions are already sorted oldest -> newest; report the newest module
            voice_nodes = []
            for v in status["versions"]:
                voice_nodes.extend(sorted(v["path"].glob("modules/discord_voice-*/discord_voice/discord_voice.node")))
            for vnode in voice_nodes[-1:]:
                v_info = voice_patcher.inspect_voice_module(vnode)
                v_state = v_info.get("status")
                if v_state == voice_patcher.STATUS_PATCHED:
                    v_str = f"PATCHED (Stereo 2-Ch / 384kbps) [{v_info.get('profile')}]"
                elif v_state == voice_patcher.STATUS_READY:
                    v_str = "UNPATCHED (Original Mono - Ready to patch)"
                elif v_state == voice_patcher.STATUS_UNRECOGNIZED:
                    v_str = "UNRECOGNIZED SIGNATURE (Safe Fail-Safe Active)"
                else:
                    v_str = str(v_state)
                print(f"  Voice Engine: {v_str}")
        print()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Vencord Graceful Patcher")
    parser.add_argument("--status", action="store_true", help="Print current status and exit")
    parser.add_argument("--check-once", action="store_true", help="Perform a single check/patch and exit")
    parser.add_argument("--unpatch", action="store_true", help="Stop the service and restore original Discord files")
    parser.add_argument("--stop", action="store_true", help="Stop the background service")
    parser.add_argument("--daemon", action="store_true", help="Run in continuous background daemon mode")
    args = parser.parse_args(argv)

    setup_logging()
    cfg = load_config()

    if args.status:
        print_status()
    elif args.stop:
        stop_daemon()
    elif args.unpatch:
        return 0 if unpatch_all(cfg) else 1
    elif args.check_once:
        run_single_check(cfg, new_runtime())
    else:
        daemon_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
