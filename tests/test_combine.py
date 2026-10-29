"""Wialon Combine codec + S1 ingest.

Run with: python3 -B -m unittest discover -s tests -p 'test_combine.py'
No test opens a socket or writes to a tracker.

The .hex/.json fixtures under tests/fixtures/combine are copied from the
platform's reference suite (internal/providers/vega/combine); the decoder here
is a port of that codec and must agree with them.
"""
import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
