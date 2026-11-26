"""The tracker's own records (0x11): parsing, acknowledging, use, keep-alive.

Run with: python3 -B tests/test_records.py
No test opens a socket. Frames are synthetic; the layout was checked against
15 294 captured frames (18 511 records) outside the repository.
"""
import asyncio
import importlib
from pathlib import Path
import struct
import sys
import tempfile
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "vega_records_tests"
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
    const.SENSOR_KEY_MAP = {"soc": "battery_percentage", "odometer": "odometer",
                            "coolant_temp": "coolant_temp"}
    const.DOWNLINK_EVENT = "vega_downlink"
    sys.modules[const.__name__] = const

protocol = importlib.import_module(PACKAGE + ".protocol")
runtime_module = importlib.import_module(PACKAGE + ".runtime")

SOC, ODOMETER, COOLANT, HDOP, UNKNOWN = 2803, 2800, 2901, 106, 25


def _tlv(sid, kind, value):
    return struct.pack("<HBB", sid, kind, len(value)) + value


def _record(stamp, lat=55.75, lon=37.62, alt=151, speed=0, course=225, sats=12, sensors=b""):
    return struct.pack("<IffhHHBH", int(stamp), lat, lon, alt, speed, course, sats,
                       0x8000 | len(sensors)) + sensors


def _payload(seq, *records):
    return struct.pack("<HH", seq, len(records)) + b"".join(records)


def _sensors(soc=55, odometer=20690, coolant=-3, hdop=0.53):
    return (_tlv(HDOP, 4, struct.pack("<f", hdop)) + _tlv(UNKNOWN, 1, b"\x02")
            + _tlv(SOC, 1, bytes([soc])) + _tlv(ODOMETER, 3, struct.pack("<I", odometer))
            + _tlv(COOLANT, 10, struct.pack("<h", coolant)))


class ParseTests(unittest.TestCase):
    def test_single_record(self):
        parsed = protocol.parse_records(_payload(211, _record(1795000000, sensors=_sensors())))
        self.assertEqual(parsed["seq"], 211)
        (rec,) = parsed["records"]
        self.assertEqual(rec["time"], 1795000000)
        self.assertAlmostEqual(rec["lat"], 55.75, places=4)
        self.assertAlmostEqual(rec["lon"], 37.62, places=4)
        self.assertEqual((rec["alt"], rec["speed"], rec["course"], rec["sats"]), (151, 0, 225, 12))
        self.assertEqual([sid for sid, _ in rec["sensors"]], [HDOP, UNKNOWN, SOC, ODOMETER, COOLANT])

    def test_several_records_and_an_empty_sensor_block(self):
        parsed = protocol.parse_records(_payload(
            7, _record(100, sensors=_sensors()), _record(101), _record(102, sensors=_tlv(SOC, 1, b"\x10"))))
        self.assertEqual([r["time"] for r in parsed["records"]], [100, 101, 102])
        self.assertEqual(parsed["records"][1]["sensors"], [])

    def test_anything_that_does_not_parse_exactly_is_rejected(self):
        good = _payload(1, _record(100, sensors=_sensors()))
        for bad in (b"", good[:3], good[:-1], good + b"\x00", good[:30],
                    struct.pack("<HH", 1, 2) + good[4:]):
            self.assertIsNone(protocol.parse_records(bad))
        broken = bytearray(good)
        broken[4 + 21 + 3] = 200  # first sensor claims a value longer than the block
        self.assertIsNone(protocol.parse_records(bytes(broken)))

    def test_ack_is_the_packet_number_and_status_zero(self):
        frame = protocol.build_records_ack_frame(0x00AB)
        self.assertEqual(frame[:6], bytes([0x03, 0x00, 0x12, 0xAB, 0x00, 0x00]))
        buf = bytearray(frame)
        self.assertEqual(list(protocol.iter_frames(buf)), [(0x12, b"\xab\x00\x00")])


class _Writer:
    def __init__(self):
        self.sent = bytearray()

    def get_extra_info(self, name):
        return ("198.51.100.7", 4000)

    def write(self, data):
        self.sent.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass

    def frames(self):
        return list(protocol.iter_frames(bytearray(self.sent)))


def _runtime(tmp, **kw):
    async def executor(fn, *a):
        return fn(*a)
    hass = types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *p: str(Path(tmp, *p))),
        async_add_executor_job=executor)
    return runtime_module.VegaRuntime(hass, "test", 21443, "0000", "0000", 30, **kw)


class UseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rt = _runtime(self.temp.name)
        self.writer = _Writer()
        self.session = runtime_module.VegaSession(self.writer)
        self.session.authenticated = True

    async def _feed(self, payload):
        await self.rt._process_vega_frame(self.session, protocol.CMD_RECORDS, payload)

    async def test_direct_session_acknowledges_and_uses_a_fresh_record(self):
        now = time.time()
        await self._feed(_payload(300, _record(now, speed=42, sensors=_sensors())))
        self.assertEqual(self.writer.frames(), [(protocol.CMD_RECORDS_ANS, struct.pack("<HB", 300, 0))])
        snap = self.rt.snapshot()
        self.assertAlmostEqual(snap["latitude"], 55.75, places=4)
        self.assertAlmostEqual(snap["longitude"], 37.62, places=4)
        self.assertEqual((snap["speed"], snap["course"], snap["altitude"], snap["satellites"]),
                         (42, 225, 151, 12))
        self.assertEqual(snap["hdop"], 0.53)
        self.assertEqual(snap["battery_percentage"], 55)
        self.assertEqual(snap["odometer"], 20690)
        self.assertEqual(snap["coolant_temp"], -3)
        self.assertIn("last_online", snap)

    async def test_replayed_history_is_acknowledged_but_not_applied(self):
        now = time.time()
        await self._feed(_payload(1, _record(now, sensors=_sensors(soc=55))))
        await self._feed(_payload(2, _record(now - 3600, lat=10.0, lon=10.0, sensors=_sensors(soc=91))))
        await self._feed(_payload(3, _record(now - 30, lat=11.0, lon=11.0, sensors=_sensors(soc=90))))
        self.assertEqual([p for _, p in self.writer.frames()],
                         [struct.pack("<HB", n, 0) for n in (1, 2, 3)])
        snap = self.rt.snapshot()
        self.assertEqual(snap["battery_percentage"], 55)
        self.assertAlmostEqual(snap["latitude"], 55.75, places=4)

    async def test_old_backlog_alone_does_not_move_last_online(self):
        await self._feed(_payload(1, _record(time.time() - 7200, sensors=_sensors())))
        snap = self.rt.snapshot()
        self.assertNotIn("last_online", snap)
        self.assertNotIn("latitude", snap)
        self.assertEqual(len(self.writer.frames()), 1)  # still acknowledged

    async def test_a_stamp_from_the_future_does_not_block_later_records(self):
        now = time.time()
        await self._feed(_payload(1, _record(now + 86400 * 400, sensors=_sensors(soc=1))))
        await self._feed(_payload(2, _record(now, sensors=_sensors(soc=55))))
        self.assertEqual(self.rt.snapshot()["battery_percentage"], 55)

    async def test_no_fix_keeps_the_previous_position_but_uses_the_sensors(self):
        now = time.time()
        await self._feed(_payload(1, _record(now - 5, sensors=_sensors(soc=60))))
        await self._feed(_payload(2, _record(now, lat=0.0, lon=0.0, sensors=_sensors(soc=59))))
        await self._feed(_payload(3, _record(now, lat=float("nan"), lon=37.0, sensors=_sensors(soc=58))))
        snap = self.rt.snapshot()
        self.assertAlmostEqual(snap["latitude"], 55.75, places=4)
        self.assertEqual(snap["battery_percentage"], 58)

    async def test_a_frame_that_does_not_parse_is_not_acknowledged(self):
        await self._feed(_payload(1, _record(time.time(), sensors=_sensors()))[:-2])
        self.assertEqual(self.writer.frames(), [])
        self.assertNotIn("latitude", self.rt.snapshot())

    async def test_relayed_session_uses_the_record_without_answering(self):
        published = []
        self.rt.set_update_callback(published.append)
        self.rt._capture("t2c", protocol.CMD_RECORDS,
                         _payload(9, _record(time.time(), sensors=_sensors())))
        self.assertEqual(self.rt.snapshot()["battery_percentage"], 55)
        self.assertTrue(published)
        self.assertEqual(self.writer.sent, b"")
        # The cloud's own acknowledgement passing the other way is not data.
        self.rt._capture("c2t", protocol.CMD_RECORDS, _payload(10, _record(time.time(), lat=1.0, lon=1.0)))
        self.assertAlmostEqual(self.rt.snapshot()["latitude"], 55.75, places=4)


class _Reader:
    def __init__(self):
        self.done = asyncio.Event()

    async def read(self, n):
        await self.done.wait()
        return b""


class KeepAliveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        saved = runtime_module.KEEPALIVE_SEC
        runtime_module.KEEPALIVE_SEC = 0.02
        self.addCleanup(setattr, runtime_module, "KEEPALIVE_SEC", saved)
        self.rt = _runtime(self.temp.name)
        self.rt.imei = "864000000000001"  # known, so no device-info query at connect

    async def test_direct_session_gets_device_info_requests_until_it_ends(self):
        reader, writer = _Reader(), _Writer()
        task = asyncio.create_task(self.rt._handle_vega(reader, writer, b""))
        await asyncio.sleep(0.03)
        session = next(iter(self.rt._vega_sessions.values()))
        before_auth = [c for c, _ in writer.frames()]
        self.assertNotIn(protocol.CMD_GET_DEV_INF, before_auth)  # not before authorization
        session.authenticated = True
        await asyncio.sleep(0.09)
        keepalives = [p for c, p in writer.frames() if c == protocol.CMD_GET_DEV_INF]
        self.assertGreaterEqual(len(keepalives), 2)
        self.assertEqual(set(keepalives), {b""})
        reader.done.set()
        await task
        count = len(writer.frames())
        await asyncio.sleep(0.06)
        self.assertEqual(len(writer.frames()), count)  # stopped with the session

    async def test_keepalives_stay_out_of_the_downlink_log(self):
        logged = []
        self.rt._ha_downlink = logged.append
        reader, writer = _Reader(), _Writer()
        task = asyncio.create_task(self.rt._handle_vega(reader, writer, b""))
        await asyncio.sleep(0.01)
        next(iter(self.rt._vega_sessions.values())).authenticated = True
        await asyncio.sleep(0.07)
        reader.done.set()
        await task
        self.assertTrue(any(c == protocol.CMD_GET_DEV_INF for c, _ in writer.frames()))
        self.assertFalse(any(len(f) >= 3 and f[2] == protocol.CMD_GET_DEV_INF for f in logged))


if __name__ == "__main__":
    unittest.main()
