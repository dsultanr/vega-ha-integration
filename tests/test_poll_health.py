"""Poll health, data-based last_online and a stop() that does not hang.

Run with: python3 -B -m unittest discover -s tests -p 'test_poll_health.py'
No test opens a socket.
"""
import asyncio
import importlib
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "vega_poll_health_tests"
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
    const.SENSOR_KEY_MAP = {"soc": "battery_percentage"}
    const.DOWNLINK_EVENT = "vega_downlink"
    sys.modules[const.__name__] = const

runtime_module = importlib.import_module(PACKAGE + ".runtime")
VegaRuntime = runtime_module.VegaRuntime
SENSOR_COUNT = len(runtime_module.protocol.SENSOR_DEFS)


def _runtime(tmp):
    async def executor(fn, *a):
        return fn(*a)
    hass = types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *p: str(Path(tmp, *p))),
        async_add_executor_job=executor)
    return VegaRuntime(hass, "test", 21443, "0000", "0000", 30)


class _Session:
    """Scripted session: mode is 'value', 'refuse', 'silent' or 'raise'."""

    def __init__(self, mode):
        self.mode = mode
        self.timeouts = 0
        self.calls = 0
        self.last_seen = time.time()
        self.authenticated = True

    async def query_sensor(self, sensor_id, sub_id=0):
        self.calls += 1
        if self.mode == "silent":
            self.timeouts += 1
            return None
        if self.mode == "raise":
            raise RuntimeError("relay timeout")
        if self.mode == "refuse":
            return None
        return {"name": "soc", "value": 55}


class PollHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rt = _runtime(self.temp.name)

    async def test_answers_mark_poll_healthy_and_move_last_online(self):
        session = _Session("value")
        self.assertTrue(await self.rt._poll_sensors(session))
        self.assertEqual(session.calls, SENSOR_COUNT)
        snap = self.rt.snapshot()
        self.assertIs(snap["poll_problem"], False)
        self.assertIn("last_online", snap)
        self.assertEqual(snap["battery_percentage"], 55)

    async def test_silent_tracker_stops_cycle_early_and_reports_problem(self):
        session = _Session("silent")
        with self.assertLogs(runtime_module._LOGGER, "WARNING") as logs:
            await self.rt._poll_sensors(session)
        self.assertEqual(session.calls, runtime_module.POLL_SILENT_LIMIT)
        self.assertIn("does not answer the sensor poll", logs.output[0])
        snap = self.rt.snapshot()
        self.assertIs(snap["poll_problem"], True)
        self.assertNotIn("last_online", snap)

    async def test_problem_is_logged_once_and_recovery_is_reported(self):
        with self.assertLogs(runtime_module._LOGGER, "INFO") as logs:
            await self.rt._poll_sensors(_Session("silent"))
            await self.rt._poll_sensors(_Session("silent"))
            await self.rt._poll_sensors(_Session("value"))
        text = "\n".join(logs.output)
        self.assertEqual(text.count("does not answer the sensor poll"), 1)
        self.assertEqual(text.count("sends data again"), 1)
        self.assertIs(self.rt.snapshot()["poll_problem"], False)

    async def test_refusals_are_answers_not_silence(self):
        session = _Session("refuse")
        await self.rt._poll_sensors(session)
        self.assertEqual(session.calls, SENSOR_COUNT)
        self.assertNotIn("battery_percentage", self.rt.snapshot())
        self.assertIs(self.rt.snapshot()["poll_problem"], False)

    async def test_raising_session_counts_as_silence(self):
        session = _Session("raise")
        await self.rt._poll_sensors(session)
        self.assertEqual(session.calls, runtime_module.POLL_SILENT_LIMIT)
        self.assertIs(self.rt.snapshot()["poll_problem"], True)

    async def test_traffic_without_data_keeps_online_but_not_last_online(self):
        session = _Session("silent")
        self.rt._vega_sessions["peer"] = session  # connected, authorized, no data
        snap = self.rt.snapshot()
        self.assertTrue(snap["is_online"])
        self.assertNotIn("last_online", snap)
        self.assertNotIn("poll_problem", snap)

    async def test_last_online_stays_at_last_data_while_traffic_continues(self):
        self.rt._ingest_sensor({"name": "soc", "value": 60})
        self.rt._last_data -= 3600
        expected = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(self.rt._last_data))
        self.rt._vega_sessions["peer"] = _Session("silent")  # fresh traffic, no values
        snap = self.rt.snapshot()
        self.assertTrue(snap["is_online"])
        self.assertEqual(snap["last_online"], expected)

    async def test_combine_position_moves_last_online(self):
        self.rt._ingest_combine_position({"lat": 55.0, "lon": 37.0})
        self.assertIn("last_online", self.rt.snapshot())


class PollModeTests(unittest.IsolatedAsyncioTestCase):
    """Which sensors a cycle asks for, by the owner's poll mode."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rt = _runtime(self.temp.name)
        self.ids = list(runtime_module.protocol.SENSOR_DEFS)

    def _cover(self, ids, age=0.0):
        for sid in ids:
            self.rt._sensor_seen[sid] = time.time() - age

    async def test_missing_is_the_default_and_the_first_cycle_asks_for_everything(self):
        self.assertEqual(self.rt.poll_mode, "missing")
        self._cover(self.ids)
        session = _Session("value")
        await self.rt._poll_sensors(session)
        self.assertEqual(session.calls, SENSOR_COUNT)
        self.assertEqual(self.rt.snapshot()["polled_sensors"], SENSOR_COUNT)

    async def test_missing_then_asks_only_for_what_records_did_not_bring(self):
        session = _Session("value")
        await self.rt._poll_sensors(session)
        session.calls = 0
        self._cover(self.ids[7:])
        published = []
        self.rt.set_update_callback(published.append)
        self.assertTrue(await self.rt._poll_sensors(session))
        self.assertEqual(session.calls, 7)
        self.assertEqual(self.rt.snapshot()["polled_sensors"], 7)

    async def test_a_sensor_last_seen_long_ago_is_asked_for_again(self):
        session = _Session("value")
        await self.rt._poll_sensors(session)
        session.calls = 0
        self._cover(self.ids[1:])
        self._cover(self.ids[:1], age=runtime_module.RECORD_COVER_SEC + 1)
        await self.rt._poll_sensors(session)
        self.assertEqual(session.calls, 1)

    async def test_everything_covered_asks_for_nothing_and_is_not_a_problem(self):
        session = _Session("value")
        await self.rt._poll_sensors(session)
        session.calls = 0
        self._cover(self.ids)
        await self.rt._poll_sensors(session)
        self.assertEqual(session.calls, 0)
        snap = self.rt.snapshot()
        self.assertEqual(snap["polled_sensors"], 0)
        self.assertIs(snap["poll_problem"], False)

    async def test_a_new_connection_gets_a_complete_cycle_again(self):
        self._cover(self.ids)
        await self.rt._poll_sensors(_Session("value"))
        second = _Session("value")
        await self.rt._poll_sensors(second)
        self.assertEqual(second.calls, SENSOR_COUNT)

    async def test_all_asks_for_everything_every_cycle(self):
        self.rt.poll_mode = "all"
        self._cover(self.ids)
        session = _Session("value")
        await self.rt._poll_sensors(session)
        await self.rt._poll_sensors(session)
        self.assertEqual(session.calls, 2 * SENSOR_COUNT)

    async def test_off_asks_for_nothing(self):
        self.rt.poll_mode = "off"
        session = _Session("value")
        await self.rt._poll_sensors(session)
        self.assertEqual(session.calls, 0)
        snap = self.rt.snapshot()
        self.assertEqual(snap["polled_sensors"], 0)
        self.assertNotIn("poll_problem", snap)  # no evidence either way yet

    async def test_mode_change_applies_to_the_next_cycle(self):
        session = _Session("value")
        await self.rt._poll_sensors(session)
        self.rt.poll_mode = "off"
        session.calls = 0
        await self.rt._poll_sensors(session)
        self.assertEqual(session.calls, 0)
        self.rt.poll_mode = "all"
        await self.rt._poll_sensors(session)
        self.assertEqual(session.calls, SENSOR_COUNT)


class DataFlowTests(unittest.IsolatedAsyncioTestCase):
    """The problem flag when little or nothing is polled."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rt = _runtime(self.temp.name)
        self.rt.poll_mode = "off"

    async def test_poll_off_and_no_records_becomes_a_problem_after_the_long_limit(self):
        session = _Session("value")
        await self.rt._poll_sensors(session)  # starts the grace period
        self.assertNotIn("poll_problem", self.rt.snapshot())
        self.rt._last_answer -= runtime_module.DATA_SILENCE_SEC - 5
        await self.rt._poll_sensors(session)
        self.assertNotIn("poll_problem", self.rt.snapshot())
        self.rt._last_answer -= 10
        with self.assertLogs(runtime_module._LOGGER, "WARNING"):
            await self.rt._poll_sensors(session)
        self.assertIs(self.rt.snapshot()["poll_problem"], True)

    async def test_a_record_clears_the_problem_even_if_it_is_old_history(self):
        session = _Session("value")
        await self.rt._poll_sensors(session)
        self.rt._last_answer -= runtime_module.DATA_SILENCE_SEC + 5
        with self.assertLogs(runtime_module._LOGGER, "WARNING"):
            await self.rt._poll_sensors(session)
        with self.assertLogs(runtime_module._LOGGER, "INFO") as logs:
            self.rt._ingest_records({"seq": 1, "records": [{
                "time": int(time.time()) - 7200, "lat": 55.0, "lon": 37.0, "alt": 0, "speed": 0,
                "course": 0, "sats": 0, "sensors": []}]})
        self.assertIn("sends data again", logs.output[0])
        self.assertIs(self.rt.snapshot()["poll_problem"], False)

    async def test_unanswered_queries_wait_while_records_are_recent(self):
        self.rt.poll_mode = "all"
        self.rt._ingest_records({"seq": 1, "records": []})  # the tracker just spoke
        await self.rt._poll_sensors(_Session("silent"))
        self.assertNotIn("poll_problem", self.rt.snapshot())
        self.rt._last_answer -= runtime_module.POLL_SILENCE_SEC + 1
        with self.assertLogs(runtime_module._LOGGER, "WARNING"):
            await self.rt._poll_sensors(_Session("silent"))
        self.assertIs(self.rt.snapshot()["poll_problem"], True)


class _Transport:
    def __init__(self):
        self.aborted = False

    def abort(self):
        self.aborted = True


class _Writer:
    def __init__(self, transport):
        self.transport = transport


class _Server:
    """Like asyncio.Server on Python 3.12+: wait_closed() waits for connections."""

    def __init__(self, transports):
        self.transports = transports
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        while not all(t.aborted for t in self.transports):
            await asyncio.sleep(0.01)


class StopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rt = _runtime(self.temp.name)

    async def test_stop_drops_open_connections_instead_of_waiting(self):
        transports = [_Transport(), _Transport()]
        for t in transports:
            self.rt._writers.add(_Writer(t))
        self.rt._server = _Server(transports)
        await asyncio.wait_for(self.rt.stop(), 1)
        self.assertTrue(all(t.aborted for t in transports))
        self.assertIsNone(self.rt._server)
        self.assertFalse(self.rt._writers)

    async def test_stop_gives_up_on_a_listener_that_never_closes(self):
        stuck = _Transport()
        server = _Server([stuck])  # nothing tracked, so nothing gets aborted
        self.rt._server = server
        runtime_module.STOP_TIMEOUT_SEC, saved = 0.05, runtime_module.STOP_TIMEOUT_SEC
        self.addCleanup(setattr, runtime_module, "STOP_TIMEOUT_SEC", saved)
        await asyncio.wait_for(self.rt.stop(), 1)
        self.assertTrue(server.closed)
        self.assertIsNone(self.rt._server)

    async def test_tracked_connection_is_forgotten_when_handler_returns(self):
        seen = []

        async def handler(reader, writer):
            seen.append(writer in self.rt._writers)
        self.rt._handle_connection = handler
        writer = object()
        await self.rt._tracked_connection(None, writer)
        self.assertEqual(seen, [True])
        self.assertNotIn(writer, self.rt._writers)


if __name__ == "__main__":
    unittest.main()
