"""Optional downlink (HA -> tracker) packet logging via the HA event bus.

Run with: python3 -B -m unittest discover -s tests -p 'test_downlink.py'
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
PACKAGE = "vega_downlink_tests"
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

runtime_module = importlib.import_module(PACKAGE + ".runtime")
VegaRuntime = runtime_module.VegaRuntime
VegaSession = runtime_module.VegaSession
InjectionSession = runtime_module.InjectionSession


class _Bus:
    def __init__(self):
        self.events = []

    def async_fire(self, event_type, data):
        self.events.append((event_type, data))


class _Writer:
    def write(self, data):
        pass

    async def drain(self):
        pass

    def get_extra_info(self, _):
        return ("1.2.3.4", 5)


def _runtime(tmp, **kw):
    async def executor(fn, *a):
        return fn(*a)
    bus = _Bus()
    hass = types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *p: str(Path(tmp, *p))),
        async_add_executor_job=executor, bus=bus)
    rt = VegaRuntime(hass, "864756081132914", 21443, "0000", "0000", 30, **kw)
    return rt, bus


class DownlinkLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_disabled_fires_nothing(self):
        rt, bus = _runtime(self.temp.name, log_downlink=False)
        rt._ha_downlink(bytes.fromhex("0c0001f1360050"))
        rt._log_downlink("proxy", b"\x01\x02\x03", None)
        self.assertEqual(bus.events, [])

    def test_ha_frame_fires_event_with_cmd_and_hex(self):
        rt, bus = _runtime(self.temp.name, log_downlink=True)
        frame = bytes.fromhex("0c0001f136005002")  # Vega frame, cmd byte = 0x01
        rt._ha_downlink(frame)
        self.assertEqual(len(bus.events), 1)
        et, data = bus.events[0]
        self.assertEqual(et, "vega_downlink")
        self.assertEqual(data["origin"], "ha")
        self.assertEqual(data["cmd"], 0x01)
        self.assertEqual(data["length"], len(frame))
        self.assertEqual(data["hex"], frame.hex())
        self.assertEqual(data["imei"], "864756081132914")

    def test_proxy_origin(self):
        rt, bus = _runtime(self.temp.name, log_downlink=True)
        rt._log_downlink("proxy", b"\x24\x24\x01", ("5.6.7.8", 9))
        self.assertEqual(bus.events[0][1]["origin"], "proxy")
        self.assertEqual(bus.events[0][1]["peer"], "('5.6.7.8', 9)")


class SessionHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_vega_session_send_raw_invokes_cb(self):
        seen = []
        s = VegaSession(_Writer())
        s.downlink_cb = seen.append
        await s.send_raw(b"\xaa\xbb\xcc")
        self.assertEqual(seen, [b"\xaa\xbb\xcc"])

    async def test_injection_request_invokes_cb_before_write(self):
        seen = []
        inj = InjectionSession(_Writer(), asyncio.Lock())
        inj.downlink_cb = seen.append
        try:
            await inj._request(b"\x01\x02\x03", 0x50000001, timeout=0.01)
        except asyncio.TimeoutError:
            pass  # no reply arrives in a unit test; we only assert the cb fired
        self.assertEqual(seen, [b"\x01\x02\x03"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
