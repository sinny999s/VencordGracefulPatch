"""
Discord Voice Stereo & High-Bitrate Native Patcher
===================================================
Dynamically finds and patches WebRTC Opus channel and bitrate configurations
in discord_voice.node without relying on brittle fixed memory offsets or
abandoned third-party binaries.

Includes fail-safe verification, stock backups and desktop notifications.

Usage (standalone):  python voice_patcher.py [--status | --patch | --unpatch]
"""

import sys
import json
import shutil
import hashlib
import logging
from pathlib import Path
from typing import Optional, List, Dict, Set

from patcher_common import (
    LOCAL_APPDATA,
    DISCORD_BRANCHES,
    send_windows_notification,
    get_running_processes,
    discord_processes_for_path,
    atomic_write_bytes,
    is_daemon_running,
)

SCRIPT_DIR = Path(__file__).resolve().parent
PATTERNS_FILE = SCRIPT_DIR / "audio_patterns.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"

VOICE_NODE_GLOB = "app-*/modules/discord_voice-*/discord_voice/discord_voice.node"
BACKUP_SUFFIX = ".node.bak"

STATUS_PATCHED = "PATCHED"
STATUS_READY = "UNPATCHED_READY"
STATUS_UNRECOGNIZED = "UNRECOGNIZED_SIGNATURE"


# ---------------------------------------------------------------------------
# Pattern profiles
# ---------------------------------------------------------------------------

_patterns_cache = {"sig": None, "profiles": []}


def _parse_hex(value: str) -> bytes:
    return bytes.fromhex(str(value).replace(" ", ""))


def load_patterns(patterns_file: Path = None) -> List[Dict]:
    """
    Loads and validates pattern profiles from audio_patterns.json.
    Each returned profile carries pre-parsed `search` / `replace` bytes.
    Invalid profiles are skipped with an error so one typo can never make the
    patcher write garbage into discord_voice.node.
    """
    patterns_file = Path(patterns_file or PATTERNS_FILE)
    if not patterns_file.exists():
        logging.error(f"Patterns file not found at {patterns_file}")
        return []

    try:
        st = patterns_file.stat()
        sig = (str(patterns_file), st.st_size, st.st_mtime_ns)
        if _patterns_cache["sig"] == sig:
            return _patterns_cache["profiles"]

        with open(patterns_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logging.error(f"Failed to load {patterns_file}: {e}")
        return []

    profiles = []
    for raw in data.get("profiles", []):
        try:
            name = str(raw["name"])
            search = _parse_hex(raw["search_hex"])
            replace = _parse_hex(raw["replace_hex"])
            expected = int(raw.get("expected_matches", 1))
            if not search or len(search) != len(replace):
                raise ValueError("search_hex and replace_hex must be the same non-zero length")
            if search == replace:
                raise ValueError("search_hex and replace_hex are identical")
            if expected < 1:
                raise ValueError("expected_matches must be >= 1")
            profiles.append({
                "name": name,
                "description": raw.get("description", ""),
                "search": search,
                "replace": replace,
                "expected_matches": expected,
            })
        except Exception as e:
            logging.error(f"Skipping invalid pattern profile {raw.get('name', '?')!r}: {e}")

    _patterns_cache["sig"] = sig
    _patterns_cache["profiles"] = profiles
    return profiles


def _find_all(data: bytes, needle: bytes) -> List[int]:
    """Returns all non-overlapping offsets of `needle` in `data`."""
    offsets = []
    pos = 0
    while True:
        pos = data.find(needle, pos)
        if pos == -1:
            return offsets
        offsets.append(pos)
        pos += len(needle)


# ---------------------------------------------------------------------------
# Discovery & inspection
# ---------------------------------------------------------------------------

def _version_key(app_dir: Path) -> list:
    return [int(x) for x in app_dir.name.replace("app-", "").split(".") if x.isdigit()]


def find_voice_modules(all_versions: bool = False, local_appdata: str = None) -> List[Path]:
    """
    Finds discord_voice.node files for every installed Discord branch.
    By default only the newest app-* folder of each branch is returned;
    `all_versions=True` returns every module found.
    """
    root = Path(local_appdata or LOCAL_APPDATA)
    modules = []
    for branch in DISCORD_BRANCHES.values():
        discord_root = root / branch["folder"]
        if not discord_root.exists():
            continue
        app_dirs = sorted(
            (d for d in discord_root.glob("app-*") if d.is_dir()),
            key=_version_key,
            reverse=True,
        )
        for app_dir in app_dirs:
            found = sorted(app_dir.glob("modules/discord_voice-*/discord_voice/discord_voice.node"))
            if not found:
                continue
            modules.extend(found)
            if not all_versions:
                break
    return modules


def backup_path_for(node_path: Path) -> Path:
    return Path(node_path).with_suffix(BACKUP_SUFFIX)


# Cache of inspection results keyed by path -> (size, mtime_ns, patterns-id, result)
_inspect_cache: Dict[str, tuple] = {}


def inspect_voice_module(node_path: Path, use_cache: bool = True) -> Dict:
    """
    Inspects a discord_voice.node file to determine its patch status.
    Results are cached by file size/mtime so the background daemon does not
    hash a multi-megabyte binary on every tick.
    """
    node_path = Path(node_path)
    if not node_path.exists():
        return {"status": "NOT_FOUND", "path": str(node_path)}

    profiles = load_patterns()
    try:
        st = node_path.stat()
    except Exception as e:
        return {"status": "ERROR", "error": str(e), "path": str(node_path)}

    key = str(node_path)
    sig = (st.st_size, st.st_mtime_ns, _patterns_cache["sig"])
    has_backup = backup_path_for(node_path).exists()

    if use_cache and key in _inspect_cache and _inspect_cache[key][0] == sig:
        cached = dict(_inspect_cache[key][1])
        cached["has_backup"] = has_backup
        return cached

    try:
        data = node_path.read_bytes()
    except Exception as e:
        return {"status": "ERROR", "error": str(e), "path": str(node_path)}

    base = {
        "size": len(data),
        "md5": hashlib.md5(data).hexdigest(),
        "has_backup": has_backup,
        "path": str(node_path),
    }

    result = None
    for prof in profiles:
        expected = prof["expected_matches"]

        replaced = _find_all(data, prof["replace"])
        if len(replaced) == expected:
            result = dict(base, status=STATUS_PATCHED, profile=prof["name"],
                          matches=[hex(m) for m in replaced])
            break

        found = _find_all(data, prof["search"])
        if len(found) == expected:
            result = dict(base, status=STATUS_READY, profile=prof["name"],
                          matches=[hex(m) for m in found])
            break

    if result is None:
        result = dict(base, status=STATUS_UNRECOGNIZED)

    _inspect_cache[key] = (sig, dict(result))
    return result


def _blocking_processes(node_path: Path, running: Optional[Set[str]]) -> List[str]:
    """
    Returns the Discord process(es) owning this module that are currently running.
    If the process list cannot be read, every owning process counts as blocking.
    """
    if running is None:
        running = get_running_processes()
    owners = discord_processes_for_path(node_path)
    if running is None:
        return owners
    return [p for p in owners if p.lower() in running]


# ---------------------------------------------------------------------------
# Patch / unpatch
# ---------------------------------------------------------------------------

def patch_voice_module(node_path: Path, notify: bool = True, running: Optional[Set[str]] = None) -> bool:
    """
    Safely patches discord_voice.node using dynamic signature matching.
    If the pattern does not match with 100% confidence the file is left stock.
    Returns True when the module is (now) patched.
    """
    node_path = Path(node_path)
    if not node_path.exists():
        logging.warning(f"Voice module does not exist: {node_path}")
        return False

    blocking = _blocking_processes(node_path, running)
    if blocking:
        logging.warning(f"Cannot patch voice module: {', '.join(blocking)} is currently running.")
        return False

    info = inspect_voice_module(node_path)
    status = info.get("status")

    if status == STATUS_PATCHED:
        logging.info(f"Already patched: {node_path.name} ({info.get('profile')})")
        return True

    if status != STATUS_READY:
        if status == STATUS_UNRECOGNIZED:
            logging.warning(f"[FAIL-SAFE] Pattern signature not recognized in {node_path}. File left stock.")
        else:
            logging.warning(f"Cannot patch {node_path}: {status} {info.get('error', '')}".rstrip())
        return False

    prof = next((p for p in load_patterns() if p["name"] == info.get("profile")), None)
    if prof is None:
        logging.error("Matched profile not found in configuration.")
        return False

    try:
        data = bytearray(node_path.read_bytes())
        offsets = [int(x, 16) for x in info["matches"]]
        search, replace = prof["search"], prof["replace"]

        # Re-verify every offset against the bytes we are about to overwrite
        for off in offsets:
            if bytes(data[off:off + len(search)]) != search:
                logging.error(f"Signature moved while patching {node_path.name}; aborting without changes.")
                return False

        bak_path = backup_path_for(node_path)
        if not bak_path.exists():
            shutil.copy2(node_path, bak_path)
            logging.info(f"Created stock backup: {bak_path}")

        for off in offsets:
            data[off:off + len(replace)] = replace

        atomic_write_bytes(node_path, bytes(data))
        _inspect_cache.pop(str(node_path), None)
        logging.info(f"Successfully patched {len(offsets)} location(s) in {node_path.name} [{prof['name']}]")

        if notify:
            send_windows_notification(
                "Discord Stereo Audio Active",
                "True Stereo (2-Channel / 384kbps) successfully enabled for Discord.",
            )
        return True
    except Exception as e:
        logging.error(f"Failed to write patched voice module: {e}")
        return False


def unpatch_voice_module(node_path: Path, notify: bool = True, running: Optional[Set[str]] = None) -> bool:
    """Restores the original stock discord_voice.node from its backup."""
    node_path = Path(node_path)
    if not node_path.exists():
        logging.warning(f"Voice module does not exist: {node_path}")
        return False

    bak_path = backup_path_for(node_path)
    if not bak_path.exists():
        logging.info(f"No stock backup found for {node_path.name}; nothing to restore.")
        return False

    blocking = _blocking_processes(node_path, running)
    if blocking:
        logging.warning(f"Cannot unpatch voice module: {', '.join(blocking)} is currently running.")
        return False

    try:
        if bak_path.stat().st_size == 0:
            logging.error(f"Backup {bak_path} is empty; refusing to restore it.")
            return False
        atomic_write_bytes(node_path, bak_path.read_bytes())
        _inspect_cache.pop(str(node_path), None)
        logging.info(f"Successfully restored stock {node_path.name} from backup.")
        if notify:
            send_windows_notification(
                "Discord Audio Reverted",
                "Stock mono voice module successfully restored.",
            )
        return True
    except Exception as e:
        logging.error(f"Failed to restore backup: {e}")
        return False


def patch_all_installed_branches(notify: bool = True) -> None:
    """Finds and patches the newest voice module of every installed Discord branch."""
    modules = find_voice_modules()
    if not modules:
        logging.info("No discord_voice.node modules found on this system.")
        return
    running = get_running_processes()
    for m in modules:
        logging.info(f"Processing voice module: {m}")
        patch_voice_module(m, notify=notify, running=running)


def unpatch_all_installed_branches(notify: bool = True) -> None:
    """Restores every backed-up voice module across all branches and versions."""
    modules = find_voice_modules(all_versions=True)
    if not modules:
        logging.info("No discord_voice.node modules found on this system.")
        return
    running = get_running_processes()
    for m in modules:
        unpatch_voice_module(m, notify=notify, running=running)


def _stereo_enabled_in_config() -> bool:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return bool(json.load(f).get("enable_stereo_patch", True))
    except Exception:
        return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_status() -> None:
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
        if status == STATUS_PATCHED:
            print("Status:   [+] PATCHED (Stereo 2-Channel & 384kbps Active)")
            print(f"Profile:  {info.get('profile')}")
            print(f"Offsets:  {', '.join(info.get('matches', []))}")
        elif status == STATUS_READY:
            print("Status:   [-] UNPATCHED (Original Mono - Ready to Patch)")
            print(f"Profile:  {info.get('profile')}")
            print(f"Offsets:  {', '.join(info.get('matches', []))}")
        elif status == STATUS_UNRECOGNIZED:
            print("Status:   [!] UNRECOGNIZED SIGNATURE (Safe Fail-Safe Active)")
            print("Note:     Discord update changed voice assembly. Left stock to prevent crashes.")
        else:
            print(f"Status:   {status} {info.get('error', '')}".rstrip())
    print("\n" + "=" * 65 + "\n")


def main(argv: List[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    arg = argv[1].lower() if len(argv) > 1 else "--status"
    if arg in ("--status", "-s"):
        print_status()
    elif arg in ("--patch", "-p"):
        patch_all_installed_branches(notify=True)
    elif arg in ("--unpatch", "-u"):
        unpatch_all_installed_branches(notify=True)
        if is_daemon_running() and _stereo_enabled_in_config():
            print("\n[!] The background patcher is running with enable_stereo_patch = true,")
            print("    so stereo will be re-applied the next time Discord is closed.")
            print("    Set \"enable_stereo_patch\": false in config.json to keep stock mono.")
    else:
        print(f"Unknown argument: {arg}")
        print("Usage: python voice_patcher.py [--status | --patch | --unpatch]")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
