"""Wialon Combine codec + S1 ingest.

Run with: python3 -B -m unittest discover -s tests -p 'test_combine.py'
No test opens a socket or writes to a tracker.

The .hex/.json fixtures under tests/fixtures/combine come from the platform's
reference suite (internal/providers/vega/combine), except data-hv-battery,
captured from the tracker with position records zeroed and CRC recomputed.
The decoder here is a port of that codec and must agree with these fixtures.
"""
import asyncio
import importlib
import json
import struct
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FX = Path(__file__).parent / "fixtures" / "combine"
PACKAGE = "vega_combine_tests"
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT / "custom_components" / "vega")]
sys.modules[PACKAGE] = pkg
try:
    import homeassistant.core  # noqa: F401
except ImportError:
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

combine = importlib.import_module(PACKAGE + ".combine")
protocol = importlib.import_module(PACKAGE + ".protocol")
runtime_module = importlib.import_module(PACKAGE + ".runtime")
VegaRuntime = runtime_module.VegaRuntime


def _hex_lines(name):
    return [l.strip() for l in (FX / name).read_text().splitlines()
            if l.strip() and not l.startswith("#")]


class CodecTests(unittest.TestCase):
    def test_crc16_check_value(self):
        self.assertEqual(combine.crc16(b"123456789"), 0xBB3D)

    def test_encode_ack(self):
        ack = combine.encode_ack(764)  # 764 = 0x02FC
        self.assertEqual(ack, bytes([0x40, 0x40, 0x00, 0x02, 0xFC]))
        self.assertEqual(len(ack), combine.AckSize if hasattr(combine, "AckSize") else 5)

    def test_single_frame_fixtures_match(self):
        for hx in sorted(FX.glob("*.hex")):
            if hx.name.startswith("stream-"):
                continue
            lines = _hex_lines(hx.name)
            self.assertEqual(len(lines), 1, hx.name)
            b = bytes.fromhex(lines[0])
            pkt, n = combine.parse_frame(b)
            exp = json.loads((FX / (hx.stem + ".json")).read_text())
            with self.subTest(fixture=hx.name):
                self.assertEqual(n, len(b), "whole frame consumed")
                self.assertEqual(pkt["type"], exp["type"])
                self.assertEqual(pkt["seq"], exp["seq"])
                if "messages" in exp:
                    self.assertEqual(len(pkt["messages"]), len(exp["messages"]))

    def test_data_decode_values(self):
        b = bytes.fromhex(_hex_lines("data-can-full.hex")[0])
        pkt, _ = combine.parse_frame(b)
        recs = [r for m in pkt["messages"] for r in m["records"]]
        pos = next(r["position"] for r in recs if "position" in r)
        self.assertAlmostEqual(pos["lat"], 55.000044, places=6)
        self.assertAlmostEqual(pos["lon"], 37.0002, places=6)
        params = {p["id"]: p for r in recs for p in r.get("params", [])}
        self.assertIn(1252, params)  # 12V battery
        self.assertAlmostEqual(params[1252]["value"], 12.64, places=2)

    def test_login_decodes_id(self):
        b = bytes.fromhex(_hex_lines("login-real.hex")[0])
        pkt, _ = combine.parse_frame(b)
        self.assertEqual(pkt["type"], "login")
        self.assertEqual(pkt["login"]["id"], "100000000000009")
        self.assertNotIn("password", pkt["login"])  # never decoded/kept

    def test_short_frame_raises(self):
        b = bytes.fromhex(_hex_lines("data-can-full.hex")[0])
        with self.assertRaises(combine.ShortFrame):
            combine.parse_frame(b[:10])


class StreamTests(unittest.TestCase):
    def test_reassembles_frame_split_across_chunks(self):
        s = combine.Stream()
        out = []
        for chunk in _hex_lines("stream-split-frame.hex"):
            s.write(bytes.fromhex(chunk))
            out.extend(s.frames())
        self.assertEqual(s.buffered(), 0, "no leftover bytes")
        self.assertEqual([p["type"] for p in out], ["data"])

    def test_multi_frame_chunk(self):
        s = combine.Stream()
        joined = b"".join(bytes.fromhex(c) for c in _hex_lines("stream-multi-frame.hex"))
        s.write(joined)
        got = list(s.frames())
        exp = json.loads((FX / "stream-multi-frame.json").read_text())
        exp_list = exp if isinstance(exp, list) else [exp]
        self.assertEqual(len(got), len(exp_list))

    def test_resyncs_after_garbage(self):
        s = combine.Stream()
        good = bytes.fromhex(_hex_lines("keepalive-1.hex")[0])
        s.write(b"\x00\x01rubbish\xff" + good)
        out = list(s.frames())
        self.assertEqual([p["type"] for p in out], ["keepalive"])
        self.assertGreater(s.dropped, 0)


class CombineIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        async def executor(fn, *a):
            return fn(*a)
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(path=lambda *p: str(Path(self.temp.name, *p))),
            async_add_executor_job=executor)
        self.rt = VegaRuntime(hass, "test", 21443, "0000", "0000", 30)
        self.ingested = []
        self.rt._ingest_sensor = lambda s: self.ingested.append(s)

    def test_known_param_forwarded_with_coerced_value(self):
        self.rt._ingest_combine_param({"id": 1252, "value": 12.64})  # battery_12v float
        self.assertEqual(self.ingested, [{"name": "battery_12v", "value": 12.64}])

    def test_bool_param_coerced_to_bool(self):
        sid = next(s for s, d in protocol.SENSOR_DEFS.items() if d.get("type") == "bool")
        name = protocol.SENSOR_DEFS[sid]["name"]
        self.rt._ingest_combine_param({"id": sid, "value": 1})
        self.assertEqual(self.ingested, [{"name": name, "value": True}])

    def test_unknown_param_dropped(self):
        self.rt._ingest_combine_param({"id": 999999, "value": 7})
        self.assertEqual(self.ingested, [])

    def test_position_maps_to_snapshot_keys(self):
        self.rt._ingest_combine_position(
            {"lat": 55.1, "lon": 37.2, "speed": 3, "course": 90, "sats": 9, "hdop": 0.8})
        self.assertEqual(self.rt._position["latitude"], 55.1)
        self.assertEqual(self.rt._position["longitude"], 37.2)
        self.assertEqual(self.rt._position["speed"], 3)
        self.assertEqual(self.rt._position["course"], 90)
        self.assertEqual(self.rt._position["satellites"], 9)


class HvIngestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        hass = types.SimpleNamespace(config=types.SimpleNamespace(
            path=lambda *p: str(Path(self.temp.name, *p))))
        self.rt = VegaRuntime(hass, "test", 21443, "0000", "0000", 30)
        self.addAsyncCleanup(self.rt.stop)
        self.hv = importlib.import_module(PACKAGE + ".hv_battery")
        self.sent = []
        async def drain():
            pass
        self.writer = types.SimpleNamespace(write=self.sent.append, drain=drain)

    async def packet(self, stamp, params):
        body = struct.pack('>IBBB', stamp, 1, 7, len(params))
        for sid, value in params:
            body += struct.pack('>HBf', 0x8000 | sid, 8, value)
        frame = b'$$\x01' + struct.pack('>HH', 123, len(body)) + body
        frame += struct.pack('>H', combine.crc16(frame))
        decoded, size = combine.parse_frame(frame)
        self.assertEqual(size, len(frame))
        await self.rt._combine_packet(self.writer, decoded)

    async def test_wire_float32_pair_updates_snapshot_and_ack(self):
        with patch.object(runtime_module.time, 'time', return_value=1000):
            self.rt._combine_last_seen = 1000
            await self.packet(1000, [(2922, 371.6), (2923, -7.2)])
            data = self.rt.snapshot()
        self.assertEqual(data['hv_battery_voltage'], 371.6)
        self.assertEqual(data['hv_battery_current'], -7.2)
        self.assertEqual(data['hv_battery_power'], -2.676)
        self.assertEqual(self.sent, [combine.encode_ack(123)])

    async def test_real_tracker_packet_with_separate_measurements(self):
        from datetime import datetime
        frame = bytes.fromhex(_hex_lines('data-hv-battery.hex')[0])
        decoded, _ = combine.parse_frame(frame)
        now = datetime.fromisoformat(decoded['messages'][-1]['time'].replace('Z', '+00:00')).timestamp() + 10
        with patch.object(runtime_module.time, 'time', return_value=now):
            self.rt._combine_last_seen = now
            await self.rt._combine_packet(self.writer, decoded)
            data = self.rt.snapshot()
        self.assertEqual(data['hv_battery_voltage'], 361.4)
        self.assertEqual(data['hv_battery_current'], 2.8)
        self.assertEqual(data['hv_battery_power'], 1.012)

    async def test_history_and_keepalive_cannot_refresh_power(self):
        with patch.object(runtime_module.time, 'time', return_value=1000):
            self.rt._combine_last_seen = 1000
            await self.packet(1000, [(2922, 360), (2923, 3)])
        with patch.object(runtime_module.time, 'time', return_value=1100):
            self.rt._combine_last_seen = 1100
            await self.packet(1000, [(2922, 360), (2923, 3)])
            data = self.rt.snapshot()
        self.assertTrue(data['is_online'])
        self.assertIsNone(data['hv_battery_power'])

    async def test_separate_combine_messages_are_not_one_sample(self):
        with patch.object(runtime_module.time, 'time', return_value=1000):
            self.rt._combine_last_seen = 1000
            await self.packet(990, [(2922, 360)])
            await self.packet(1000, [(2923, 3)])
            self.assertIsNone(self.rt.snapshot()['hv_battery_power'])

    async def test_binary_reply_ingestion_uses_physical_float_units(self):
        with patch.object(runtime_module.time, 'time', return_value=1000):
            self.rt._wialon_last_seen = 1000
            for sid, value in ((2922, 360), (2923, 3)):
                self.rt._ingest_sensor(protocol.decode_sensor(sid, struct.pack('<f', value) + bytes(4)))
            self.assertEqual(self.rt.snapshot()['hv_battery_power'], 1.08)

    async def test_invalid_combine_value_invalidates_previous(self):
        with patch.object(runtime_module.time, 'time', return_value=1000):
            self.rt._combine_last_seen = 1000
            await self.packet(1000, [(2922, 360), (2923, 3)])
        with patch.object(runtime_module.time, 'time', return_value=1001):
            await self.packet(1001, [(2923, float('nan'))])
            self.assertIsNone(self.rt.snapshot()['hv_battery_power'])

    async def test_expiry_publishes_without_any_new_network_packet(self):
        updates = []
        self.rt.set_update_callback(updates.append)
        with patch.object(self.hv, 'MAX_AGE', 0.025):
            self.rt._wialon_last_seen = runtime_module.time.time()
            for sid, value in ((2922, 360), (2923, 3)):
                self.rt._ingest_sensor(protocol.decode_sensor(sid, struct.pack('<f', value)))
            self.rt._publish()
            self.assertEqual(updates[-1]['hv_battery_power'], 1.08)
            await asyncio.sleep(0.06)
            self.assertIsNone(updates[-1]['hv_battery_power'])
        self.assertIsNone(self.rt._hv_expiry)


if __name__ == "__main__":
    unittest.main(verbosity=2)
