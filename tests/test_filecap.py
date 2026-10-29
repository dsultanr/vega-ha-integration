"""File/firmware capture: FIRMWARE_CMDS detection, lossless capture, finalize.

Run with: python3 -B -m unittest discover -s tests -p 'test_filecap.py'
No test opens a socket or writes to a tracker.

Opcodes 0x04/0x08/0x13 used here are the ones actually observed on the live
Vega MITM relay (GET_FILE, FILE_CHUNK_ANS, FILE_WHOLLY on S4 file transfers);
0x0A (GET_DEV_INF) is the routine poll that must NOT be treated as a file.
"""
import asyncio
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "vega_filecap_tests"
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT / "custom_components" / "vega")]
sys.modules[PACKAGE] = pkg
try:
    import homeassistant.core  # noqa: F401
except ImportError:
    # Stdlib-only checkout: shim the few HA types runtime.py needs at import.
    for name in ("homeassistant", "homeassistant.core", "homeassistant.exceptions"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["homeassistant.core"].HomeAssistant = object
    sys.modules["homeassistant.exceptions"].HomeAssistantError = type(
        "HomeAssistantError", (Exception,), {})
    const = types.ModuleType(PACKAGE + ".const")
    const.DEFAULT_IMEI = "000000000000000"
    const.ONLINE_TIMEOUT = 180
    const.SENSOR_KEY_MAP = {}
    const.DOWNLINK_EVENT = "vega_downlink"
    sys.modules[const.__name__] = const

runtime_module = importlib.import_module(PACKAGE + ".runtime")
VegaRuntime = runtime_module.VegaRuntime
_FileCapture = runtime_module._FileCapture
FIRMWARE_CMDS = runtime_module.FIRMWARE_CMDS


def make_hass(tmp):
    """Fake hass: sync executor, task tracking, config.path into a temp dir."""
    tasks = []

    async def executor(fn, *args):
        return fn(*args)

    def create_task(coro, *a, **k):
        t = asyncio.ensure_future(coro)
        tasks.append(t)
        return t

    hass = types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *p: str(Path(tmp, *p))),
        loop=asyncio.get_event_loop(),
        async_add_executor_job=executor,
        async_create_task=create_task,
    )
    hass._tasks = tasks
    return hass


class FirmwareCmdSetTests(unittest.TestCase):
    def test_set_is_file_opcodes_without_get_dev_inf(self):
        self.assertEqual(FIRMWARE_CMDS, {0x03, 0x04, 0x08, 0x09, 0x13, 0x14})
        # GET_DEV_INF (0x0A/0x0B) is routine polling — the mitm false-positive bug.
        self.assertNotIn(0x0A, FIRMWARE_CMDS)
        self.assertNotIn(0x0B, FIRMWARE_CMDS)
        # COMMAND_ANS (telemetry) must not be a file either.
        self.assertNotIn(0x02, FIRMWARE_CMDS)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = make_hass(self.temp.name)
        self.fc = _FileCapture(self.hass)
        self.notes = []
        self.fc._notify = lambda nid, title, msg: self.notes.append((nid, title, msg))

    async def _finalize_now(self):
        if self.fc._finalize_handle is not None:
            self.fc._finalize_handle.cancel()
        self.fc._finalize()
        await asyncio.gather(*self.hass._tasks)

    async def test_captures_payloads_losslessly_and_finalizes(self):
        frames = [
            ("c2t", 0x08, bytes.fromhex("deadbeef")),  # GET_FILE
            ("c2t", 0x04, b"\x01\x02\x03"),            # FILE_CHUNK_ANS
            ("t2c", 0x13, b"\xAA" * 55),               # FILE_WHOLLY
        ]
        for d, c, p in frames:
            self.fc.feed(d, c, p, "864756081132914")

        base = self.fc._base
        self.assertTrue(Path(base + ".jsonl").exists())
        self.assertTrue(Path(base + ".payloads.bin").exists())

        # .bin is exactly the ordered concatenation of payloads.
        expected = b"".join(p for _, _, p in frames)
        self.assertEqual(Path(base + ".payloads.bin").read_bytes(), expected)

        # .jsonl keeps one lossless record per frame.
        recs = [json.loads(line) for line in Path(base + ".jsonl").read_text().splitlines()]
        self.assertEqual(len(recs), 3)
        self.assertEqual(recs[0]["cmd"], 0x08)
        self.assertEqual(recs[0]["cmd_name"], "GET_FILE")
        self.assertEqual(recs[0]["dir"], "c2t")
        self.assertEqual(recs[0]["hex"], "deadbeef")
        self.assertEqual(recs[2]["len"], 55)

        # A start notification was raised.
        self.assertTrue(any(t == "Vega: файловый обмен (возможно прошивка)"
                            for _, t, _ in self.notes))

        await self._finalize_now()
        self.assertFalse(self.fc._active)
        digest = hashlib.sha256(expected).hexdigest()
        self.assertTrue(any(digest in msg for _, _, msg in self.notes),
                        "finalize notification must carry the sha256 of the capture")

    async def test_new_transfer_after_quiet_gap_opens_a_new_file(self):
        self.fc.feed("c2t", 0x08, b"\x01", "123")
        base1 = self.fc._base
        await self._finalize_now()
        self.fc.feed("c2t", 0x08, b"\x02", "123")
        self.assertNotEqual(self.fc._base, base1)
        self.assertTrue(self.fc._active)
        if self.fc._finalize_handle is not None:
            self.fc._finalize_handle.cancel()


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        async def _mk():
            return make_hass(self.temp.name)
        self.hass = asyncio.new_event_loop().run_until_complete(_mk())
        self.runtime = VegaRuntime(self.hass, "test", 21443, "0000", "0000", 30)
        self.fed = []
        self.runtime._filecap = types.SimpleNamespace(
            feed=lambda *a: self.fed.append(a))

    def test_capture_dispatches_file_cmds_and_skips_get_dev_inf(self):
        self.runtime._capture("c2t", 0x08, b"\x00")   # GET_FILE  -> captured
        self.runtime._capture("t2c", 0x13, b"\x00")   # FILE_WHOLLY -> captured
        self.runtime._capture("c2t", 0x0A, b"")       # GET_DEV_INF -> skipped
        cmds = [a[1] for a in self.fed]
        self.assertEqual(cmds, [0x08, 0x13])


if __name__ == "__main__":
    unittest.main(verbosity=2)
