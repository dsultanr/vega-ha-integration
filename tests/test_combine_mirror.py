"""Copy of the Wialon Combine stream to the owner's server.

Run with: python3 -B tests/test_combine_mirror.py
No test opens a socket.
"""
import asyncio
import importlib
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
FX = Path(__file__).parent / "fixtures" / "combine"
PACKAGE = "vega_combine_mirror_tests"
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
mirror_module = importlib.import_module(PACKAGE + ".combine_mirror")
runtime_module = importlib.import_module(PACKAGE + ".runtime")
CombineMirror = mirror_module.CombineMirror
MirrorState = mirror_module.MirrorState
parse_target = mirror_module.parse_target


def _frame(name):
    lines = [l.strip() for l in (FX / name).read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    return bytes.fromhex(lines[0])


LOGIN = _frame("login-real.hex")
DATA = _frame("data-driving-locked.hex")
DATA2 = _frame("data-can-full.hex")
KEEPALIVE = _frame("keepalive-1.hex")
TARGET = ("copy.example", 20332)


class _UpWriter:
    def __init__(self, fail_after=None):
        self.sent = []
        self.closed = False
        self.fail_after = fail_after

    def write(self, data):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise ConnectionResetError("reset")
        self.sent.append(bytes(data))

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class _UpReader:
    """Server side that stays silent until hang_up() is called."""

    def __init__(self, replies=()):
        self._replies = list(replies)
        self._eof = asyncio.Event()

    def hang_up(self):
        self._eof.set()

    async def read(self, n):
        if self._replies:
            return self._replies.pop(0)
        await self._eof.wait()
        return b""


class _Net:
    """Scripted open_connection: each entry is an Exception or a (reader, writer)."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    async def __call__(self, host, port):
        self.calls.append((host, port))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


async def _settle(rounds=20):
    for _ in range(rounds):
        await asyncio.sleep(0)


class ParseTargetTests(unittest.TestCase):
    def test_empty_means_off(self):
        for text in ("", "   ", None):
            self.assertIsNone(parse_target(text))

    def test_host_and_port(self):
        self.assertEqual(parse_target(" wialon.example.org:20332 "), ("wialon.example.org", 20332))
        self.assertEqual(parse_target("10.0.0.5:1"), ("10.0.0.5", 1))
        self.assertEqual(parse_target("[::1]:65535"), ("::1", 65535))

    def test_rejects_everything_else(self):
        for text in ("host", "host:", ":20332", "host:0", "host:65536", "host:abc",
                     "host:-1", "ho st:20332", "host:20 332"):
            with self.assertRaises(ValueError, msg=text):
                parse_target(text)


class RawFramesTests(unittest.TestCase):
    def test_raw_bytes_are_the_exact_frames_even_when_split(self):
        stream = combine.Stream()
        blob = LOGIN + DATA + KEEPALIVE
        got = []
        for i in range(0, len(blob), 7):
            stream.write(blob[i:i + 7])
            got.extend(stream.raw_frames())
        self.assertEqual([raw for _, raw in got], [LOGIN, DATA, KEEPALIVE])
        self.assertEqual([p["type"] for p, _ in got], ["login", "data", "keepalive"])

    def test_frames_still_yields_packets_only(self):
        stream = combine.Stream()
        stream.write(LOGIN + DATA)
        self.assertEqual([p["type"] for p in stream.frames()], ["login", "data"])


class MirrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = MirrorState()
        self.target = TARGET
        saved = mirror_module.BACKOFF_SEC
        mirror_module.BACKOFF_SEC = 0.01
        self.addCleanup(setattr, mirror_module, "BACKOFF_SEC", saved)

    def _mirror(self, net):
        mirror = CombineMirror(lambda: self.target, self.state, open_connection=net)
        self.addAsyncCleanup(mirror.close)
        return mirror

    async def test_forwards_login_then_frames_byte_for_byte(self):
        writer = _UpWriter()
        net = _Net((_UpReader(), writer))
        mirror = self._mirror(net)
        mirror.feed("login", LOGIN)
        mirror.feed("data", DATA)
        await _settle()
        mirror.feed("keepalive", KEEPALIVE)
        mirror.feed("data", DATA2)
        await _settle()
        self.assertEqual(net.calls, [TARGET])
        self.assertEqual(writer.sent, [LOGIN, DATA, KEEPALIVE, DATA2])
        self.assertTrue(self.state.connected)

    async def test_off_means_no_connection_and_nothing_buffered(self):
        self.target = None
        net = _Net()
        mirror = self._mirror(net)
        mirror.feed("login", LOGIN)
        mirror.feed("data", DATA)
        await _settle()
        self.assertEqual(net.calls, [])
        self.assertEqual(mirror._queued, 0)
        self.assertIsNone(mirror._task)

    async def test_login_alone_does_not_connect(self):
        net = _Net()
        mirror = self._mirror(net)
        mirror.feed("login", LOGIN)
        await _settle()
        self.assertEqual(net.calls, [])

    async def test_server_replies_are_discarded(self):
        writer = _UpWriter()
        net = _Net((_UpReader(replies=[b"\x40\x40\x00\x00\x01", b"junk"]), writer))
        mirror = self._mirror(net)
        mirror.feed("login", LOGIN)
        mirror.feed("data", DATA)
        await _settle()
        self.assertEqual(writer.sent, [LOGIN, DATA])
        self.assertTrue(self.state.connected)

    async def test_unreachable_server_keeps_frames_and_delivers_after_recovery(self):
        writer = _UpWriter()
        net = _Net(ConnectionRefusedError("refused"), (_UpReader(), writer))
        mirror = self._mirror(net)
        with self.assertLogs(mirror_module._LOGGER, "INFO") as logs:
            mirror.feed("login", LOGIN)
            mirror.feed("data", DATA)
            await _settle()
            self.assertTrue(self.state.down)
            mirror.feed("data", DATA2)
            await asyncio.sleep(0.05)
        self.assertEqual(writer.sent, [LOGIN, DATA, DATA2])
        text = "\n".join(logs.output)
        self.assertEqual(text.count("is unavailable"), 1)
        self.assertEqual(text.count("is reachable again"), 1)
        self.assertFalse(self.state.down)

    async def test_outage_is_logged_once_across_tracker_connections(self):
        net = _Net(*[ConnectionRefusedError("refused") for _ in range(100)])
        with self.assertLogs(mirror_module._LOGGER, "WARNING") as logs:
            for _ in range(2):  # the tracker reconnects: a new mirror each time
                mirror = CombineMirror(lambda: self.target, self.state, open_connection=net)
                mirror.feed("login", LOGIN)
                mirror.feed("data", DATA)
                await asyncio.sleep(0.03)
                await mirror.close()
        self.assertGreaterEqual(len(net.calls), 2)
        self.assertEqual(len(logs.output), 1)

    async def test_write_failure_reconnects_replays_login_and_resends_the_frame(self):
        first, second = _UpWriter(fail_after=2), _UpWriter()
        net = _Net((_UpReader(), first), (_UpReader(), second))
        mirror = self._mirror(net)
        mirror.feed("login", LOGIN)
        mirror.feed("data", DATA)
        await _settle()
        mirror.feed("data", DATA2)  # the write of this frame fails
        await asyncio.sleep(0.05)
        self.assertEqual(first.sent, [LOGIN, DATA])
        self.assertTrue(first.closed)
        self.assertEqual(second.sent, [LOGIN, DATA2])

    async def test_server_hang_up_reconnects_on_next_frame_without_an_outage(self):
        reader, first, second = _UpReader(), _UpWriter(), _UpWriter()
        net = _Net((reader, first), (_UpReader(), second))
        mirror = self._mirror(net)
        mirror.feed("login", LOGIN)
        mirror.feed("data", DATA)
        await _settle()
        reader.hang_up()
        await _settle()
        self.assertTrue(first.closed)
        self.assertFalse(self.state.down)
        mirror.feed("data", DATA2)
        await _settle()
        self.assertEqual(second.sent, [LOGIN, DATA2])

    async def test_keepalives_do_not_pile_up_while_disconnected(self):
        net = _Net(ConnectionRefusedError("refused"))
        mirror = self._mirror(net)
        mirror.feed("login", LOGIN)
        for _ in range(50):
            mirror.feed("keepalive", KEEPALIVE)
        self.assertEqual(mirror._queued, 0)

    async def test_buffer_is_bounded_and_drops_the_oldest(self):
        saved = mirror_module.MAX_BUFFER_BYTES
        mirror_module.MAX_BUFFER_BYTES = len(DATA) * 3
        self.addCleanup(setattr, mirror_module, "MAX_BUFFER_BYTES", saved)
        mirror_module.BACKOFF_SEC = 60
        net = _Net(ConnectionRefusedError("refused"))
        mirror = self._mirror(net)
        mirror.feed("login", LOGIN)
        for _ in range(10):
            mirror.feed("data", DATA)
        mirror.feed("data", DATA2)
        self.assertLessEqual(mirror._queued, max(len(DATA) * 3, len(DATA2)))
        self.assertGreater(mirror.dropped, 0)
        self.assertIs(mirror._queue[-1], DATA2)

    async def test_turning_the_copy_off_drops_the_connection_and_the_buffer(self):
        writer = _UpWriter()
        net = _Net((_UpReader(), writer))
        mirror = self._mirror(net)
        mirror.feed("login", LOGIN)
        mirror.feed("data", DATA)
        await _settle()
        self.target = None
        mirror.feed("data", DATA2)
        mirror._wake.set()
        await _settle()
        self.assertEqual(writer.sent, [LOGIN, DATA])
        self.assertTrue(writer.closed)

    async def test_close_cancels_the_task_and_closes_the_connection(self):
        writer = _UpWriter()
        mirror = CombineMirror(lambda: self.target, self.state,
                               open_connection=_Net((_UpReader(), writer)))
        mirror.feed("login", LOGIN)
        mirror.feed("data", DATA)
        await _settle()
        await mirror.close()
        self.assertTrue(writer.closed)
        self.assertIsNone(mirror._task)
        self.assertFalse(self.state.connected)


class _TrackerReader:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def read(self, n):
        await asyncio.sleep(0)
        return self.chunks.pop(0) if self.chunks else b""


class _TrackerWriter:
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


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _runtime(self, **kw):
        async def executor(fn, *a):
            return fn(*a)
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(path=lambda *p: str(Path(self.temp.name, *p))),
            async_add_executor_job=executor)
        return runtime_module.VegaRuntime(hass, "test", 21443, "0000", "0000", 30, **kw)

    async def _serve(self, rt, net):
        saved = mirror_module.asyncio.open_connection
        mirror_module.asyncio.open_connection = net
        self.addCleanup(setattr, mirror_module.asyncio, "open_connection", saved)
        tracker = _TrackerWriter()
        reader = _TrackerReader([DATA, DATA2, KEEPALIVE])

        class _Slow(_TrackerReader):
            async def read(inner, n):
                await _settle()
                return await _TrackerReader.read(inner, n)
        reader.__class__ = _Slow
        await rt._handle_combine(reader, tracker, LOGIN)
        return tracker

    async def test_copy_is_off_by_default(self):
        rt = self._runtime()
        self.assertIsNone(rt.combine_upstream)
        net = _Net()
        tracker = await self._serve(rt, net)
        self.assertEqual(net.calls, [])
        self.assertEqual(len(tracker.sent), 4 * 5)  # login, 2 data, keep-alive ACKed by HA
        self.assertIn("latitude", rt.snapshot())

    async def test_tracker_is_served_and_the_stream_is_copied(self):
        rt = self._runtime(combine_upstream=TARGET)
        writer = _UpWriter()
        net = _Net((_UpReader(), writer))
        tracker = await self._serve(rt, net)
        self.assertEqual(net.calls, [TARGET])
        self.assertEqual(writer.sent, [LOGIN, DATA, DATA2, KEEPALIVE])
        self.assertTrue(writer.closed)  # tracker gone, copy connection closed
        self.assertEqual(len(tracker.sent), 4 * 5)
        self.assertIn("latitude", rt.snapshot())

    async def test_sensor_follows_the_last_delivery_attempt(self):
        rt = self._runtime(combine_upstream=TARGET)
        self.assertNotIn("combine_copy_ok", rt.snapshot())  # nothing tried yet
        published = []
        rt.set_update_callback(lambda data: published.append(data.get("combine_copy_ok")))
        await self._serve(rt, _Net((_UpReader(), _UpWriter())))
        # Still "ok" after the tracker left and the copy connection was closed.
        self.assertIs(rt.snapshot()["combine_copy_ok"], True)
        self.assertIn(True, published)
        with self.assertLogs(mirror_module._LOGGER, "WARNING"):
            await self._serve(rt, _Net(ConnectionRefusedError("refused")))
        self.assertIs(rt.snapshot()["combine_copy_ok"], False)
        self.assertIs(published[-1], False)

    async def test_sensor_is_absent_when_the_copy_is_off_or_repointed(self):
        rt = self._runtime(combine_upstream=TARGET)
        await self._serve(rt, _Net((_UpReader(), _UpWriter())))
        rt._combine_copy.reset()  # what an address change does
        self.assertNotIn("combine_copy_ok", rt.snapshot())
        rt._combine_copy._set_alive(True)
        rt.combine_upstream = None
        self.assertNotIn("combine_copy_ok", rt.snapshot())

    async def test_dead_copy_server_does_not_affect_home_assistant(self):
        rt = self._runtime(combine_upstream=TARGET)
        net = _Net(ConnectionRefusedError("refused"))
        with self.assertLogs(mirror_module._LOGGER, "WARNING"):
            tracker = await self._serve(rt, net)
        self.assertEqual(len(tracker.sent), 4 * 5)
        self.assertIn("latitude", rt.snapshot())
        self.assertIn("last_online", rt.snapshot())


if __name__ == "__main__":
    unittest.main()
