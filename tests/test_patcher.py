"""
Unit tests for Vencord Graceful Patcher.

These exercise every file-manipulating code path against synthetic Discord
installations in a temporary directory, so they run on any platform.
    python -m unittest discover -s tests -v
"""

import io
import json
import struct
import random
import shutil
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

import patcher_common
import voice_patcher
import patcher

logging.disable(logging.CRITICAL)

V2 = next(p for p in voice_patcher.load_patterns(ROOT / "audio_patterns.json")
          if p["name"] == "webrtc_opus_encoder_factory_v2")


def random_bytes(n: int, seed: int = 1234) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.getrandbits(8) for _ in range(n))


def synthetic_voice_module(pattern: bytes, count: int, seed: int = 1) -> bytes:
    """Random binary with `count` copies of `pattern` at well separated offsets."""
    data = bytearray(random_bytes(64 * 1024, seed))
    for i in range(count):
        off = 4096 + i * 16384
        data[off:off + len(pattern)] = pattern
    return bytes(data)


class TempEnv(unittest.TestCase):
    """Redirects every filesystem location the patcher touches into a temp dir."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vgp-test-"))
        self.local = self.tmp / "Local"
        self.roaming = self.tmp / "Roaming"
        self.dist = self.roaming / "Vencord" / "dist"
        self.appdata_dir = self.local / "VencordGracefulPatch"
        self.local.mkdir(parents=True)
        self.dist.mkdir(parents=True)
        self.appdata_dir.mkdir(parents=True)

        self.patches = [
            mock.patch.object(patcher_common, "LOCAL_APPDATA", str(self.local)),
            mock.patch.object(patcher_common, "APP_DATA_DIR", self.appdata_dir),
            mock.patch.object(patcher_common, "STATE_FILE", self.appdata_dir / "state.json"),
            mock.patch.object(voice_patcher, "LOCAL_APPDATA", str(self.local)),
            mock.patch.object(patcher, "LOCAL_APPDATA", str(self.local)),
            mock.patch.object(patcher, "VENCORD_DIST_DIR", self.dist),
            mock.patch.object(patcher, "PATCHER_JS", self.dist / "patcher.js"),
            mock.patch.object(patcher, "RENDERER_JS", self.dist / "renderer.js"),
            mock.patch.object(patcher, "get_running_processes", lambda: self.running),
            mock.patch.object(patcher, "send_windows_notification", self._toast),
            mock.patch.object(voice_patcher, "send_windows_notification", self._toast),
            mock.patch.object(patcher, "time", mock.Mock(sleep=lambda s: None, monotonic=__import__("time").monotonic)),
        ]
        for p in self.patches:
            p.start()
        self.running = set()
        self.toasts = []
        patcher._renderer_cache.clear()
        patcher._dist_download["last_attempt"] = None
        voice_patcher._inspect_cache.clear()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _toast(self, title, message, app_name=None):
        self.toasts.append((title, message))

    # helpers -------------------------------------------------------------

    def make_dist(self, renderer="console.log('vencord');\n//# sourceMappingURL=vencord://renderer.js.map\n"):
        (self.dist / "patcher.js").write_text("require('_app.asar')", encoding="utf-8")
        (self.dist / "preload.js").write_text("preload", encoding="utf-8")
        (self.dist / "renderer.js").write_text(renderer, encoding="utf-8")
        (self.dist / "renderer.css").write_text("body{}", encoding="utf-8")

    def make_discord(self, branch="stable", version="1.0.9000", voice=None, stock_asar=None):
        folder = patcher_common.DISCORD_BRANCHES[branch]["folder"]
        vdir = self.local / folder / f"app-{version}"
        res = vdir / "resources"
        res.mkdir(parents=True)
        (res / "app.asar").write_bytes(stock_asar if stock_asar is not None else random_bytes(20000, 7))
        if voice is not None:
            vmod = vdir / "modules" / "discord_voice-1" / "discord_voice"
            vmod.mkdir(parents=True)
            (vmod / "discord_voice.node").write_bytes(voice)
        return vdir


class AsarTests(unittest.TestCase):
    def test_asar_layout_matches_electron_pickle_format(self):
        path = r"C:\Users\me\AppData\Roaming\Vencord\dist\patcher.js"
        blob = patcher.create_vencord_asar(path)
        four, hdr_sz, obj_sz, hdr_len = struct.unpack("<IIII", blob[:16])
        self.assertEqual(four, 4)
        self.assertEqual(obj_sz, hdr_sz - 4)
        aligned = (hdr_len + 3) & ~3
        self.assertEqual(hdr_sz, aligned + 8)
        header = json.loads(blob[16:16 + hdr_len])
        data_start = 8 + hdr_sz
        files = header["files"]
        idx = blob[data_start + int(files["index.js"]["offset"]):][:files["index.js"]["size"]]
        pkg = blob[data_start + int(files["package.json"]["offset"]):][:files["package.json"]["size"]]
        self.assertEqual(idx.decode(), f"require({json.dumps(path)})")
        self.assertEqual(json.loads(pkg)["main"], "index.js")
        self.assertEqual(len(blob), data_start + files["package.json"]["size"] + int(files["package.json"]["offset"]))

    def test_version_tuple(self):
        self.assertEqual(patcher.get_version_tuple("app-1.0.9259"), (1, 0, 9259))
        self.assertEqual(patcher.get_version_tuple("app-garbage"), (0, 0, 0))
        self.assertLess(patcher.get_version_tuple("app-1.0.999"), patcher.get_version_tuple("app-1.0.1000"))


class ConfigTests(TempEnv):
    def test_defaults_merge_and_validation(self):
        cfg_file = self.tmp / "config.json"
        cfg_file.write_text(json.dumps({
            "check_interval_seconds": 1,            # clamped to minimum
            "cooldown_after_close_seconds": "abc",  # invalid -> default
            "enable_fake_deafen": 0,                # coerced to bool
            "monitored_branches": ["Stable", "nope", "canary", "stable"],
        }), encoding="utf-8")
        cfg = patcher.load_config(cfg_file)
        self.assertEqual(cfg["check_interval_seconds"], patcher.MIN_CHECK_INTERVAL_SECONDS)
        self.assertEqual(cfg["cooldown_after_close_seconds"], 5)
        self.assertIs(cfg["enable_fake_deafen"], False)
        self.assertEqual(cfg["monitored_branches"], ["stable", "canary"])
        self.assertTrue(cfg["enable_stereo_patch"])

    def test_broken_config_falls_back_to_defaults(self):
        cfg_file = self.tmp / "config.json"
        cfg_file.write_text("{ not json", encoding="utf-8")
        self.assertEqual(patcher.load_config(cfg_file), patcher.load_config(self.tmp / "missing.json"))

    def test_repo_config_is_valid_and_matches_defaults(self):
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(set(cfg), set(patcher.DEFAULT_CONFIG))
        self.assertEqual(cfg, patcher.DEFAULT_CONFIG)


class VencordPatchTests(TempEnv):
    def test_patch_unpatch_roundtrip(self):
        self.make_dist()
        stock = random_bytes(30000, 3)
        vdir = self.make_discord(stock_asar=stock)
        res = vdir / "resources"

        self.assertTrue(patcher.patch_version_natively(vdir))
        self.assertEqual((res / "_app.asar").read_bytes(), stock)
        self.assertTrue(patcher.is_vencord_loader(res / "app.asar"))
        self.assertIn(str(self.dist / "patcher.js").encode(), (res / "app.asar").read_bytes())

        # idempotent
        self.assertTrue(patcher.patch_version_natively(vdir))
        status = patcher.check_branch_status("stable")
        self.assertTrue(status["versions"][0]["is_patched"])
        self.assertEqual(status["unpatched_versions"], [])

        self.assertTrue(patcher.unpatch_version_natively(vdir))
        self.assertEqual((res / "app.asar").read_bytes(), stock)
        self.assertFalse((res / "_app.asar").exists())

    def test_stale_backup_is_replaced_by_real_stock_asar(self):
        self.make_dist()
        stock = random_bytes(30000, 5)
        vdir = self.make_discord(stock_asar=stock)
        (vdir / "resources" / "_app.asar").write_bytes(b"old stale backup" * 1000)
        self.assertTrue(patcher.patch_version_natively(vdir))
        self.assertEqual((vdir / "resources" / "_app.asar").read_bytes(), stock)

    def test_rollback_when_loader_write_fails(self):
        self.make_dist()
        stock = random_bytes(30000, 9)
        vdir = self.make_discord(stock_asar=stock)
        with mock.patch.object(patcher, "create_vencord_asar", side_effect=OSError("disk full")):
            self.assertFalse(patcher.patch_version_natively(vdir))
        self.assertEqual((vdir / "resources" / "app.asar").read_bytes(), stock)
        self.assertFalse((vdir / "resources" / "_app.asar").exists())

    def test_patch_refused_without_vencord_dist(self):
        vdir = self.make_discord()
        with mock.patch.object(patcher, "download_file", side_effect=OSError("offline")):
            self.assertFalse(patcher.patch_version_natively(vdir))
            self.assertTrue((vdir / "resources" / "app.asar").exists())
            self.assertFalse((vdir / "resources" / "_app.asar").exists())
            # second call within the retry window must not try the network again
            with mock.patch.object(patcher, "download_file") as dl:
                self.assertFalse(patcher.ensure_vencord_dist())
                dl.assert_not_called()

    def test_dist_download_writes_all_files_atomically(self):
        def fake_download(url, dest, timeout=60):
            name = url.rsplit("/", 1)[1]
            dest.write_bytes(b"require('../_app.asar')" if name == "patcher.js" else b"x")
        with mock.patch.object(patcher, "download_file", side_effect=fake_download):
            self.assertTrue(patcher.ensure_vencord_dist())
        for name in patcher.VENCORD_DIST_FILES:
            self.assertTrue((self.dist / name).exists(), name)
        self.assertEqual(list(self.dist.glob("*.download")), [])

    def test_unpatch_leaves_foreign_app_asar_alone(self):
        vdir = self.make_discord()
        (vdir / "resources" / "_app.asar").write_bytes(random_bytes(30000, 2))
        self.assertFalse(patcher.unpatch_version_natively(vdir))
        self.assertTrue((vdir / "resources" / "_app.asar").exists())


class FakeDeafenTests(TempEnv):
    def test_inject_refresh_remove(self):
        original = "console.log('vencord');\n//# sourceMappingURL=vencord://renderer.js.map\n"
        self.make_dist(renderer=original)
        payload_file = self.tmp / "fake_deafen_plus.js"
        payload_file.write_text("(() => { console.log('v1'); })();", encoding="utf-8")
        renderer = self.dist / "renderer.js"

        with mock.patch.object(patcher, "FAKE_DEAFEN_JS", payload_file):
            patcher.sync_fake_deafen(True)
            content = renderer.read_text(encoding="utf-8")
            self.assertEqual(patcher.renderer_injection_state(renderer, patcher.get_fake_deafen_payload()), "current")
            self.assertTrue(content.startswith(original.rstrip()))
            self.assertIn("console.log('v1')", content)

            # unchanged payload -> no rewrite
            before = renderer.stat()
            patcher.sync_fake_deafen(True)
            self.assertEqual(renderer.stat().st_mtime_ns, before.st_mtime_ns)

            # payload updated -> old block replaced, never duplicated
            payload_file.write_text("(() => { console.log('v2-longer'); })();", encoding="utf-8")
            patcher.sync_fake_deafen(True)
            content = renderer.read_text(encoding="utf-8")
            self.assertNotIn("console.log('v1')", content)
            self.assertIn("console.log('v2-longer')", content)
            self.assertEqual(content.count(patcher.FAKE_DEAFEN_MARKER_PREFIX), 1)

            # legacy marker without hash counts as stale and gets refreshed
            renderer.write_text(original + "\n/* === VencordGracefulPatch: FakeDeafen+ Integration === */\nold\n", encoding="utf-8")
            patcher.sync_fake_deafen(True)
            content = renderer.read_text(encoding="utf-8")
            self.assertNotIn("\nold\n", content)
            self.assertEqual(content.count(patcher.FAKE_DEAFEN_MARKER_PREFIX), 1)

            # disabled -> removed, original restored exactly (modulo trailing newline)
            patcher.sync_fake_deafen(False)
            self.assertEqual(renderer.read_text(encoding="utf-8").rstrip(), original.rstrip())
            self.assertFalse(patcher.remove_fake_deafen(self.dist))

    def test_real_payload_is_valid_javascript_shape(self):
        payload = patcher.get_fake_deafen_payload(ROOT / "tools" / "fake_deafen_plus.js")
        self.assertTrue(payload.startswith("/**"))
        self.assertTrue(payload.rstrip().endswith("})();"))


class VoicePatchTests(TempEnv):
    def test_inspect_patch_unpatch(self):
        stock = synthetic_voice_module(V2["search"], V2["expected_matches"])
        vdir = self.make_discord(voice=stock)
        node = next(vdir.glob("modules/*/discord_voice/discord_voice.node"))

        info = voice_patcher.inspect_voice_module(node)
        self.assertEqual(info["status"], voice_patcher.STATUS_READY)
        self.assertEqual(info["profile"], "webrtc_opus_encoder_factory_v2")

        self.assertTrue(voice_patcher.patch_voice_module(node, notify=True, running=set()))
        patched = node.read_bytes()
        self.assertEqual(len(patched), len(stock))
        self.assertEqual(patched.count(V2["replace"]), 2)
        self.assertEqual(patched.count(V2["search"]), 0)
        self.assertEqual(voice_patcher.backup_path_for(node).read_bytes(), stock)
        self.assertEqual(voice_patcher.inspect_voice_module(node)["status"], voice_patcher.STATUS_PATCHED)
        self.assertEqual(len(self.toasts), 1)

        # everything except the two signatures is untouched
        diff = [i for i, (a, b) in enumerate(zip(stock, patched)) if a != b]
        self.assertTrue(all(4096 <= i < 4096 + 12 or 20480 <= i < 20480 + 12 for i in diff))

        self.assertTrue(voice_patcher.unpatch_voice_module(node, notify=False, running=set()))
        self.assertEqual(node.read_bytes(), stock)

    def test_unrecognized_and_blocked(self):
        vdir = self.make_discord(voice=synthetic_voice_module(V2["search"], 3))
        node = next(vdir.glob("modules/*/discord_voice/discord_voice.node"))
        self.assertEqual(voice_patcher.inspect_voice_module(node)["status"], voice_patcher.STATUS_UNRECOGNIZED)
        self.assertFalse(voice_patcher.patch_voice_module(node, notify=True, running=set()))
        self.assertFalse(voice_patcher.backup_path_for(node).exists())

        vdir2 = self.make_discord(branch="canary", voice=synthetic_voice_module(V2["search"], 2))
        node2 = next(vdir2.glob("modules/*/discord_voice/discord_voice.node"))
        # Stable running must not block Canary, but Canary running must
        self.assertEqual(patcher_common.discord_processes_for_path(node2), ["DiscordCanary.exe"])
        self.assertFalse(voice_patcher.patch_voice_module(node2, notify=False, running={"discordcanary.exe"}))
        self.assertTrue(voice_patcher.patch_voice_module(node2, notify=False, running={"discord.exe"}))

    def test_inspect_cache_invalidates_on_change(self):
        vdir = self.make_discord(voice=synthetic_voice_module(V2["search"], 2))
        node = next(vdir.glob("modules/*/discord_voice/discord_voice.node"))
        first = voice_patcher.inspect_voice_module(node)
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("should be cached")):
            self.assertEqual(voice_patcher.inspect_voice_module(node)["md5"], first["md5"])
        node.write_bytes(synthetic_voice_module(V2["search"], 2, seed=99))
        self.assertNotEqual(voice_patcher.inspect_voice_module(node)["md5"], first["md5"])

    def test_find_voice_modules_newest_or_all(self):
        self.make_discord(version="1.0.9000", voice=b"a")
        self.make_discord(version="1.0.9001", voice=b"b")
        newest = voice_patcher.find_voice_modules()
        self.assertEqual([p.read_bytes() for p in newest], [b"b"])
        self.assertEqual(len(voice_patcher.find_voice_modules(all_versions=True)), 2)

    def test_invalid_profiles_are_skipped(self):
        bad = self.tmp / "patterns.json"
        bad.write_text(json.dumps({"profiles": [
            {"name": "bad-len", "search_hex": "01 02", "replace_hex": "01", "expected_matches": 1},
            {"name": "same", "search_hex": "01 02", "replace_hex": "01 02", "expected_matches": 1},
            {"name": "ok", "search_hex": "01 02", "replace_hex": "03 04", "expected_matches": 1},
        ]}), encoding="utf-8")
        self.assertEqual([p["name"] for p in voice_patcher.load_patterns(bad)], ["ok"])


class DaemonLoopTests(TempEnv):
    def config(self, **overrides):
        cfg = patcher.load_config(self.tmp / "nope.json")
        cfg["cooldown_after_close_seconds"] = 0
        cfg.update(overrides)
        return cfg

    def test_waits_while_running_then_patches_everything(self):
        self.make_dist()
        stock = random_bytes(30000, 11)
        vdir = self.make_discord(stock_asar=stock, voice=synthetic_voice_module(V2["search"], 2))
        runtime = patcher.new_runtime()
        cfg = self.config()

        self.running = {"discord.exe"}
        patcher.run_single_check(cfg, runtime)
        self.assertTrue(runtime["waiting"]["stable"])
        self.assertEqual((vdir / "resources" / "app.asar").read_bytes(), stock)
        self.assertEqual(self.toasts, [])

        self.running = set()
        patcher.run_single_check(cfg, runtime)
        self.assertFalse(runtime["waiting"]["stable"])
        self.assertTrue(patcher.is_vencord_loader(vdir / "resources" / "app.asar"))
        self.assertEqual((vdir / "resources" / "_app.asar").read_bytes(), stock)
        node = next(vdir.glob("modules/*/discord_voice/discord_voice.node"))
        self.assertEqual(voice_patcher.inspect_voice_module(node)["status"], voice_patcher.STATUS_PATCHED)
        self.assertIn(patcher.FAKE_DEAFEN_MARKER_PREFIX, (self.dist / "renderer.js").read_text(encoding="utf-8"))
        self.assertEqual([t[0] for t in self.toasts], ["Vencord Auto-Patched", "Discord Stereo Audio Active"])

        # steady state: nothing more to do, no more toasts
        patcher.run_single_check(cfg, runtime)
        self.assertEqual(len(self.toasts), 2)

    def test_restart_during_cooldown_keeps_waiting(self):
        self.make_dist()
        vdir = self.make_discord()
        runtime = patcher.new_runtime()
        cfg = self.config()
        self.running = {"discord.exe"}
        patcher.run_single_check(cfg, runtime)

        snapshots = iter([set(), {"discord.exe"}])
        with mock.patch.object(patcher, "get_running_processes", lambda: next(snapshots)):
            patcher.run_single_check(cfg, runtime)
        self.assertFalse((vdir / "resources" / "_app.asar").exists())
        self.assertTrue(runtime["waiting"]["stable"])

    def test_fake_deafen_resynced_even_while_discord_runs(self):
        self.make_dist()
        self.make_discord()
        runtime = patcher.new_runtime()
        self.running = {"discord.exe"}
        patcher.run_single_check(self.config(), runtime)
        self.assertIn(patcher.FAKE_DEAFEN_MARKER_PREFIX, (self.dist / "renderer.js").read_text(encoding="utf-8"))
        # Vencord self-update overwrites renderer.js -> re-injected on next tick
        (self.dist / "renderer.js").write_text("fresh vencord build\n", encoding="utf-8")
        patcher.run_single_check(self.config(), runtime)
        self.assertIn(patcher.FAKE_DEAFEN_MARKER_PREFIX, (self.dist / "renderer.js").read_text(encoding="utf-8"))
        patcher.run_single_check(self.config(enable_fake_deafen=False), runtime)
        self.assertEqual((self.dist / "renderer.js").read_text(encoding="utf-8"), "fresh vencord build\n")

    def test_unrecognized_signature_alerts_once_and_persists(self):
        self.make_dist()
        vdir = self.make_discord(voice=synthetic_voice_module(V2["search"], 5))
        (vdir / "resources" / "_app.asar").write_bytes(b"x")
        # make the version look already patched so only the voice module matters
        (vdir / "resources" / "app.asar").write_bytes(patcher.create_vencord_asar("patcher.js"))

        runtime = patcher.new_runtime()
        self.running = {"discord.exe"}
        patcher.run_single_check(self.config(), runtime)
        patcher.run_single_check(self.config(), runtime)
        self.assertEqual([t[0] for t in self.toasts], ["Discord Audio Notice"])
        node = next(vdir.glob("modules/*/discord_voice/discord_voice.node"))
        self.assertFalse(voice_patcher.backup_path_for(node).exists())

        # a fresh daemon start reads the persisted state and stays quiet
        patcher.run_single_check(self.config(), patcher.new_runtime())
        self.assertEqual(len(self.toasts), 1)
        self.assertIn("notified_voice_signatures", patcher_common.load_state())

    def test_unknown_process_list_never_patches(self):
        self.make_dist()
        vdir = self.make_discord(voice=synthetic_voice_module(V2["search"], 2))
        runtime = patcher.new_runtime()
        with mock.patch.object(patcher, "get_running_processes", lambda: None):
            patcher.run_single_check(self.config(), runtime)
        self.assertFalse((vdir / "resources" / "_app.asar").exists())
        node = next(vdir.glob("modules/*/discord_voice/discord_voice.node"))
        self.assertEqual(voice_patcher.inspect_voice_module(node)["status"], voice_patcher.STATUS_READY)
        self.assertFalse(voice_patcher.patch_voice_module(node, notify=False, running=None)
                         if voice_patcher.get_running_processes() is None else False)
        with mock.patch.object(voice_patcher, "get_running_processes", lambda: None):
            self.assertFalse(voice_patcher.patch_voice_module(node, notify=False))
        self.assertTrue(patcher_common.is_process_running("Discord.exe", None) in (True, False))

    def test_failure_backoff_prevents_retry_storm(self):
        self.make_dist()
        self.make_discord()
        runtime = patcher.new_runtime()
        with mock.patch.object(patcher, "patch_version_natively", return_value=False) as pv:
            patcher.run_single_check(self.config(), runtime)
            patcher.run_single_check(self.config(), runtime)
            self.assertEqual(pv.call_count, 1)
        self.assertIn("stable", runtime["backoff"])

    def test_unpatch_all_restores_everything(self):
        self.make_dist()
        stock = random_bytes(30000, 21)
        voice = synthetic_voice_module(V2["search"], 2)
        vdir = self.make_discord(stock_asar=stock, voice=voice)
        runtime = patcher.new_runtime()
        patcher.run_single_check(self.config(), runtime)

        with mock.patch.object(patcher, "stop_daemon_processes", return_value=[]), \
             mock.patch.object(voice_patcher, "get_running_processes", lambda: set()), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertTrue(patcher.unpatch_all(self.config()))
        self.assertEqual((vdir / "resources" / "app.asar").read_bytes(), stock)
        self.assertFalse((vdir / "resources" / "_app.asar").exists())
        node = next(vdir.glob("modules/*/discord_voice/discord_voice.node"))
        self.assertEqual(node.read_bytes(), voice)
        self.assertNotIn(patcher.FAKE_DEAFEN_MARKER_PREFIX, (self.dist / "renderer.js").read_text(encoding="utf-8"))


class WindowsHelperTests(unittest.TestCase):
    def test_failed_tasklist_reads_as_unknown(self):
        with mock.patch.object(patcher_common, "IS_WINDOWS", True), \
             mock.patch.object(patcher_common.subprocess, "run", side_effect=OSError("boom")):
            self.assertIsNone(patcher_common.get_running_processes())
            self.assertTrue(patcher_common.is_process_running("Discord.exe"))
        with mock.patch.object(patcher_common, "IS_WINDOWS", True), \
             mock.patch.object(patcher_common.subprocess, "run", return_value=mock.Mock(stdout="", returncode=1)):
            self.assertIsNone(patcher_common.get_running_processes())

    def test_tasklist_csv_parsing(self):
        csv = ('"System Idle Process","0","Services","0","8 K"\n'
               '"Discord.exe","1234","Console","1","123,456 K"\n'
               '"DiscordCanary.exe","99","Console","1","1 K"\n'
               'INFO: garbage line\n')
        fake = mock.Mock(stdout=csv, returncode=0)
        with mock.patch.object(patcher_common, "IS_WINDOWS", True), \
             mock.patch.object(patcher_common.subprocess, "run", return_value=fake):
            snap = patcher_common.get_running_processes()
        self.assertEqual(snap, {"system idle process", "discord.exe", "discordcanary.exe"})
        self.assertTrue(patcher_common.is_process_running("Discord.exe", snap))
        self.assertFalse(patcher_common.is_process_running("DiscordPTB.exe", snap))

    def test_stop_daemon_processes_parses_pids(self):
        fake = mock.Mock(stdout="4321\n\n8765\n")
        with mock.patch.object(patcher_common, "IS_WINDOWS", True), \
             mock.patch.object(patcher_common.subprocess, "run", return_value=fake) as run:
            self.assertEqual(patcher_common.stop_daemon_processes(exclude_pid=1), [4321, 8765])
        script = run.call_args[0][0][-1]
        self.assertIn("patcher.py", script)
        self.assertIn("--daemon", script)
        self.assertIn("-ne 1", script)

    def test_toast_escapes_single_quotes(self):
        with mock.patch.object(patcher_common, "IS_WINDOWS", True), \
             mock.patch.object(patcher_common.subprocess, "run") as run:
            patcher_common.send_windows_notification("It's", "a 'test'")
        script = run.call_args[0][0][-1]
        self.assertIn("'It''s'", script)
        self.assertIn("'a ''test'''", script)


if __name__ == "__main__":
    unittest.main()
