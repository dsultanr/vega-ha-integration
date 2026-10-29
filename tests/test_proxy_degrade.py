"""Proxy upstream-down degradation: local fallback vs throttled close.

Run with: python3 -B -m unittest discover -s tests -p 'test_proxy_degrade.py'
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
PACKAGE = "vega_proxy_tests"
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


class _Writer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _runtime(tmp, **kw):
    async def executor(fn, *a):
        return fn(*a)
    hass = types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *p: str(Path(tmp, *p))),
        async_add_executor_job=executor)
    return VegaRuntime(hass, "test", 21443, "0000", "0000", 30,
                       upstream=("up.example", 21124), **kw)


class ProxyDegradeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_direct_control_serves_locally(self):
        rt = _runtime(self.temp.name, direct_control=True)
        served = []
        async def fake_handle_vega(reader, writer, initial):
            served.append((reader, writer, initial))
        rt._handle_vega = fake_handle_vega
        w = _Writer()
        await rt._proxy_degrade("READER", w, b"abc", ("1.2.3.4", 5), "unreachable")
        self.assertEqual(len(served), 1)
        self.assertEqual(served[0][2], b"abc")   # initial forwarded
        self.assertFalse(w.closed)               # local handler owns the socket

    async def test_no_direct_throttles_then_closes(self):
        rt = _runtime(self.temp.name, direct_control=False)
        slept = []
        orig_sleep = asyncio.sleep
        async def fake_sleep(d):
            slept.append(d)
            await orig_sleep(0)
        runtime_module.asyncio.sleep = fake_sleep
        try:
            w = _Writer()
            await rt._proxy_degrade("READER", w, b"", ("1.2.3.4", 5), "backoff")
        finally:
            runtime_module.asyncio.sleep = orig_sleep
        self.assertEqual(slept, [runtime_module.UPSTREAM_THROTTLE_SEC])
        self.assertTrue(w.closed)                # throttled, then closed


if __name__ == "__main__":
    unittest.main(verbosity=2)
