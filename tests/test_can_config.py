"""CAN editing against archived device bytes and simulated transport failures.

Run with: python3 -B -m unittest discover -s tests -p 'test_can_config.py'
No test opens a socket or writes to a tracker.
"""
import asyncio
import copy
import importlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "vega_config_tests"
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT / "custom_components" / "vega")]
sys.modules[PACKAGE] = pkg
try:
    import homeassistant.core  # noqa: F401
except ImportError:
    # The runtime only needs HA types/constants here. In the HA container use
    # the real dependencies, while a standalone checkout stays stdlib-only.
    for name in ("homeassistant", "homeassistant.core", "homeassistant.exceptions"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["homeassistant.core"].HomeAssistant = object
    sys.modules["homeassistant.exceptions"].HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    const = types.ModuleType(PACKAGE + ".const")
    const.DEFAULT_IMEI = "000000000000000"
    const.ONLINE_TIMEOUT = 180
    const.SENSOR_KEY_MAP = {}
    const.DOWNLINK_EVENT = "vega_downlink"
    sys.modules[const.__name__] = const

protocol = importlib.import_module(PACKAGE + ".protocol")
runtime_module = importlib.import_module(PACKAGE + ".runtime")
VegaRuntime = runtime_module.VegaRuntime
CommandError = runtime_module.CommandError
FIXTURES = Path(__file__).parent / "fixtures"
SLOTS = {int(k): bytes.fromhex(v) for k, v in json.loads((FIXTURES / "can_slots.json").read_text()).items()}
SCENARIOS = {int(k): bytes.fromhex(v) for k, v in json.loads((FIXTURES / "scenarios.json").read_text()).items()}


def document(slot=5):
    decoded = protocol.decode_can_script(SLOTS[slot])
    return {"name": decoded["name"], "repeat": decoded["repeat"],
            "frames": [{k: f[k] for k in ("delay_ms", "can_id", "bus", "data")} for f in decoded["frames"]]}


class ProtocolTests(unittest.TestCase):
    def test_all_archived_slots_roundtrip(self):
        for slot, raw in SLOTS.items():
            with self.subTest(slot=slot):
                self.assertEqual(protocol.build_can_script_from_dict(protocol.decode_can_script(raw)), raw)

    def test_remoteon_is_preheat_bytes_but_not_a_scenario_route(self):
        self.assertEqual(SLOTS[5][16:], SLOTS[4][16:])
        scenarios = [protocol.decode_scenario(raw, index) for index, raw in SCENARIOS.items()]
        bindings = protocol.command_script_bindings(scenarios)
        for action, slot in {"lock": 0, "unlock": 1, "cooling": 2, "preheat": 4, "horn": 7}.items():
            self.assertEqual(bindings[action]["status"], "single")
            self.assertEqual(bindings[action]["slots"], [slot])
        self.assertEqual(bindings["remote_on"]["status"], "none")
        self.assertEqual(bindings["remote_off"]["slots"], [])

    def test_routes_follow_actual_scenario_not_old_slot_table(self):
        raws = dict(SCENARIOS)
        unlock = bytearray(raws[9])
        unlock[49] = 4 * (3 + 1)
        raws[9] = bytes(unlock)
        scenarios = [protocol.decode_scenario(raw, i) for i, raw in raws.items()]
        self.assertEqual(protocol.command_script_bindings(scenarios)["unlock"]["slots"], [3])

    def test_partial_and_conditional_routes_do_not_claim_unique_binding(self):
        scenarios = [protocol.decode_scenario(raw, index) for index, raw in SCENARIOS.items()]
        self.assertEqual(protocol.command_script_bindings(scenarios[:-1])["unlock"]["status"], "unavailable")
        raw = bytearray(SCENARIOS[9])
        struct.pack_into("<HfB", raw, 8, 2806, 0.0, 0)
        raw[7] = 1
        scenarios[9] = protocol.decode_scenario(bytes(raw), 9)
        self.assertEqual(protocol.command_script_bindings(scenarios)["unlock"]["status"], "conditional")
        raw[7] = 0  # OR: the command's own condition is sufficient.
        scenarios[9] = protocol.decode_scenario(bytes(raw), 9)
        self.assertEqual(protocol.command_script_bindings(scenarios)["unlock"]["status"], "single")

    def test_multiple_scenarios_and_unknown_format(self):
        scenarios = [protocol.decode_scenario(raw, index) for index, raw in SCENARIOS.items()]
        extra = bytearray(SCENARIOS[9])
        extra[49] = 4 * (7 + 1)
        scenarios[12] = protocol.decode_scenario(bytes(extra), 12)
        self.assertEqual(protocol.command_script_bindings(scenarios)["unlock"]["status"], "multiple")
        for raw in (b"\0" * 49, SCENARIOS[9][:49] + b"\xfc"):
            with self.assertRaises(ValueError):
                protocol.decode_scenario(raw, 9)

    def test_reject_input_that_used_to_be_silently_truncated(self):
        mutations = [
            ("name", "a" * 17), ("name", "a\0b"), ("name", "🙂"),
            ("repeat", True), ("repeat", 1.5), ("repeat", -1), ("repeat", 256),
            ("frames", [document()["frames"][0]] * 5), ("frames", None),
        ]
        for key, value in mutations:
            spec = document()
            spec[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                protocol.build_can_script_from_dict(spec)
        for key, values in {"data": ["ff", "ff" * 9, "zz" * 8, None],
                            "delay_ms": [-1, 32768, "20", 1.5, True],
                            "can_id": [-1, 0x20000000], "bus": [-1, 4]}.items():
            for value in values:
                spec = document()
                spec["frames"][0][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    protocol.build_can_script_from_dict(spec)
        for spec in ({}, [], {**document(), "repeet": 1}):
            with self.assertRaises(ValueError):
                protocol.build_can_script_from_dict(spec)

    def test_zero_repeat_is_preserved(self):
        spec = document()
        spec["repeat"] = 0
        self.assertEqual(protocol.decode_can_script(protocol.build_can_script_from_dict(spec))["repeat"], 0)


class FakeSession:
    def __init__(self):
        self.settings = {(5502, i): raw for i, raw in SLOTS.items()}
        self.settings.update({(4001, i): raw for i, raw in SCENARIOS.items()})
        self.writes = []
        self.reads = []
        self.mode = "normal"
        self.authenticated = True
        self.last_seen = 0

    async def query_sensor(self, sid, sub):
        self.reads.append((sid, sub))
        if self.writes and self.mode == "read_timeout":
            return None
        raw = self.settings.get((sid, sub))
        return protocol.decode_sensor(sid, raw) if raw is not None else None

    async def set_setting(self, sid, sub, data):
        self.writes.append((sid, sub, data))
        if self.mode not in ("mismatch", "read_timeout"):
            self.settings[sid, sub] = data
        if self.mode == "lost_ack":
            raise asyncio.TimeoutError
        return {"success": True}


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        async def executor(fn, *args):
            return fn(*args)
        hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda *p: str(Path(self.temp.name, *p))),
                                     async_add_executor_job=executor)
        self.runtime = VegaRuntime(hass, "test", 21443, "0000", "0000", 30, direct_control=True)
        self.session = FakeSession()
        self.runtime._inject_session = self.session
        self.spec = document()
        self.spec["name"] = "Edited"
        self.events = []
        self.runtime.set_update_callback(lambda data: self.events.append(data))

    async def test_success_is_full_readback_and_persists_raw(self):
        response = await self.runtime.async_write_can_script(5, self.spec, SLOTS[5].hex())
        self.assertTrue(response["verified"])
        self.assertEqual(len(self.session.writes), 1)
        self.assertEqual(self.session.reads, [(5502, 5), (5502, 5)])
        cache = json.loads(Path(self.runtime._scripts_path()).read_text())
        self.assertEqual(cache[0]["raw"], protocol.build_can_script_from_dict(self.spec).hex())
        backup = json.loads(Path(self.temp.name, "vega", "can_script_backups_test.jsonl").read_text())
        self.assertEqual(backup["before"], SLOTS[5].hex())
        self.assertTrue(self.events)

    async def test_ack_success_is_insufficient_and_actual_state_is_shown(self):
        self.session.mode = "mismatch"
        with patch.object(runtime_module.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaisesRegex(CommandError, "байты отличаются"):
                await self.runtime.async_write_can_script(5, self.spec)
        self.assertEqual(len(self.session.writes), 1)
        self.assertEqual(self.runtime.can_scripts[0]["name"], "RemoteOn")

    async def test_lost_ack_but_matching_read_is_success(self):
        self.session.mode = "lost_ack"
        self.assertTrue((await self.runtime.async_write_can_script(5, self.spec))["verified"])
        self.assertEqual(len(self.session.writes), 1)

    async def test_readback_timeout_never_reports_success_or_keeps_stale_slot(self):
        self.session.mode = "read_timeout"
        with patch.object(runtime_module.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaisesRegex(CommandError, "не подтверждена"):
                await self.runtime.async_write_can_script(5, self.spec)
        self.assertEqual(len(self.session.writes), 1)
        self.assertEqual(self.runtime.can_scripts, [])

    async def test_concurrent_edit_is_rejected_without_writing(self):
        with self.assertRaisesRegex(CommandError, "Слот изменился"):
            await self.runtime.async_write_can_script(5, self.spec, SLOTS[4].hex())
        self.assertEqual(self.session.writes, [])

    async def test_invalid_or_disabled_commands_never_reach_transport(self):
        for slot in (-1, 8, True, "5"):
            with self.assertRaises(CommandError):
                await self.runtime.async_write_can_script(slot, self.spec)
        invalid = copy.deepcopy(self.spec)
        invalid["frames"][0]["data"] = "ff" * 9
        with self.assertRaises(CommandError):
            await self.runtime.async_write_can_script(5, invalid)
        self.runtime.direct_control = False
        with self.assertRaises(CommandError):
            await self.runtime.async_write_can_script(5, self.spec)
        self.assertEqual(self.session.reads, [])
        self.assertEqual(self.session.writes, [])

    async def test_backup_failure_and_control_disabled_during_read_abort(self):
        with patch.object(self.runtime, "_backup_script", side_effect=CommandError("backup failure")):
            with self.assertRaises(CommandError):
                await self.runtime.async_write_can_script(5, self.spec)
        async def disable(*args):
            self.runtime.direct_control = False
        with patch.object(self.runtime, "_backup_script", side_effect=disable):
            with self.assertRaises(CommandError):
                await self.runtime.async_write_can_script(5, self.spec)
        self.assertEqual(self.session.writes, [])

    async def test_unchanged_script_is_verified_without_write(self):
        response = await self.runtime.async_write_can_script(5, document())
        self.assertTrue(response["unchanged"])
        self.assertEqual(self.session.writes, [])

    async def test_unknown_bytes_are_not_silently_overwritten(self):
        raw = bytearray(SLOTS[5])
        raw[16] = 0x21
        self.session.settings[5502, 5] = bytes(raw)
        with self.assertRaisesRegex(CommandError, "без потерь"):
            await self.runtime.async_write_can_script(5, self.spec)
        self.assertEqual(self.session.writes, [])

    async def test_two_writers_cannot_overwrite_same_revision(self):
        other = copy.deepcopy(self.spec)
        other["name"] = "Second"
        results = await asyncio.gather(
            self.runtime.async_write_can_script(5, self.spec, SLOTS[5].hex()),
            self.runtime.async_write_can_script(5, other, SLOTS[5].hex()), return_exceptions=True)
        self.assertTrue(results[0]["verified"])
        self.assertIsInstance(results[1], CommandError)
        self.assertEqual(len(self.session.writes), 1)

    async def test_read_scenarios_and_invalidate_mapping_when_unavailable(self):
        await self.runtime.async_read_can_scripts()
        self.assertEqual(self.runtime.command_slot("unlock"), 1)
        self.assertIsNone(self.runtime.command_slot("remote_on"))
        self.session.settings.pop((4001, 24))
        await self.runtime.async_read_can_scripts()
        self.assertIsNone(self.runtime.command_slot("unlock"))
        self.assertEqual(len(self.runtime.can_scripts), 8)
        self.assertEqual(self.session.writes, [])

    async def test_pre_read_failure_never_writes(self):
        self.session.settings.pop((5502, 5))
        with self.assertRaises(CommandError):
            await self.runtime.async_write_can_script(5, self.spec)
        self.assertEqual(self.session.writes, [])

    async def test_failed_refresh_clears_previously_read_bindings(self):
        await self.runtime.async_read_can_scripts()
        self.session.settings.pop((5502, 5))
        with self.assertRaises(CommandError):
            await self.runtime.async_read_can_scripts()
        self.assertIsNone(self.runtime.command_slot("unlock"))
        self.assertIsNone(self.runtime.scenario_read_at)


if __name__ == "__main__":
    unittest.main()
