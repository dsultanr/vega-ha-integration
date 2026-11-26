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


class _End:
    async def read(self, n):
        return b""


class _Sink:
    def __init__(self):
        self.closed = False

    def get_extra_info(self, name):
        return ("198.51.100.7", 4000)

    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class UpstreamSensorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rt = _runtime(self.temp.name, proxy_mode=True)
        self.rt.hass.async_create_background_task = lambda coro, **kw: asyncio.create_task(coro)

        async def poll(*args):
            await asyncio.sleep(3600)
        self.rt._inject_poll_loop = poll
        self.published = []
        self.rt.set_update_callback(lambda data: self.published.append(data.get("upstream_ok")))
        saved = runtime_module.asyncio.open_connection
        self.addCleanup(setattr, runtime_module.asyncio, "open_connection", saved)

    def _upstream(self, outcome):
        async def open_connection(host, port):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        runtime_module.asyncio.open_connection = open_connection

    async def test_absent_until_a_connect_was_tried(self):
        self.assertNotIn("upstream_ok", self.rt.snapshot())

    async def test_follows_the_last_connect(self):
        self._upstream((_End(), _Sink()))
        await self.rt._handle_vega_proxy(_End(), _Sink(), b"")
        self.assertIs(self.rt.snapshot()["upstream_ok"], True)

        self._upstream(ConnectionRefusedError("refused"))
        degraded = []

        async def degrade(*args):
            degraded.append(args[-1])
        self.rt._proxy_degrade = degrade
        await self.rt._handle_vega_proxy(_End(), _Sink(), b"")
        self.assertIs(self.rt.snapshot()["upstream_ok"], False)
        self.assertEqual(degraded, ["unreachable"])
        # Inside the backoff window no connect is made and the state stays off.
        await self.rt._handle_vega_proxy(_End(), _Sink(), b"")
        self.assertEqual(degraded, ["unreachable", "backoff"])
        self.assertIs(self.rt.snapshot()["upstream_ok"], False)
        self.assertEqual([p for p in self.published if p is not None][:1], [True])
        self.assertIs(self.published[-1], False)

    async def test_absent_when_proxy_mode_is_off(self):
        self.rt._note_upstream(True)
        self.rt.proxy_mode = False
        self.assertNotIn("upstream_ok", self.rt.snapshot())


class RawRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_bytes_saved_before_forward_even_when_drain_fails(self):
        import base64
        import json
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            rt = _runtime(tmp, direct_control=True)
            tasks=[]
            def task(coro, **kwargs):
                t=asyncio.create_task(coro);tasks.append(t);return t
            rt.hass.async_create_background_task=task
            async def poll(*args):
                await asyncio.sleep(3600)
            rt._inject_poll_loop=poll
            rt._log_downlink=lambda *args:None
            seen=[]
            def saved(direction):
                records=[json.loads(line) for p in Path(tmp,'vega_rawcap').glob('*.jsonl') for line in p.read_text().splitlines()]
                return b''.join(base64.b64decode(r['base64']) for r in records if r['direction']==direction)
            class Reader:
                def __init__(self,data):self.data=data
                async def read(self,n):
                    data,self.data=self.data,b''
                    return data
            class Writer:
                def __init__(self,direction):self.direction=direction;self.sent=b''
                def get_extra_info(self,name):return ('test',1)
                def write(self,data):
                    self.sent+=data
                    assert saved(self.direction)==self.sent
                    seen.append(self.direction)
                async def drain(self):
                    if self.direction=='c2t':raise OSError('connection lost')
                def close(self):pass
            downstream=Writer('c2t');upstream=Writer('t2c')
            incoming=b'not a frame'+bytes(range(256))*20
            async def connect(*args):return Reader(incoming),upstream
            with patch.object(runtime_module.asyncio,'open_connection',side_effect=connect):
                await rt._handle_vega_proxy(Reader(b'next'),downstream,b'initial')
            self.assertEqual(saved('t2c'),b'initialnext')
            self.assertEqual(saved('c2t'),incoming)
            self.assertIn('c2t',seen)
            await asyncio.gather(*tasks,return_exceptions=True)

class CloudRelayOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_foreign_connection_does_not_close_cloud_relay(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            rt = _runtime(tmp, direct_control=True)
            class Relay:
                is_cloud_relay = True
                def __init__(self):self.writer=_Writer()
            relay=Relay();rt._inject_session=relay
            foreign=_Writer();foreign.get_extra_info=lambda name:('scanner',1)
            async def connect(*args):raise AssertionError('upstream must not be opened')
            with patch.object(runtime_module.asyncio,'open_connection',side_effect=connect):
                await rt._handle_vega_proxy(None,foreign,b'GET /')
            self.assertTrue(foreign.closed)
            self.assertFalse(relay.writer.closed)
            self.assertIs(rt._inject_session,relay)


class ServerSlotReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_four_slots_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt=_runtime(tmp)
            calls=[]
            class Session:
                async def query_sensor(self,setting,sub):
                    calls.append((setting,sub))
                    raw=(f'server{sub}:21124'.encode().ljust(32,b'\0')+b'\x34\x12\x03')
                    return {'id':3001,'raw_hex':raw.hex()}
            rt._inject_session=Session()
            result=await rt.async_read_server_slots()
            self.assertEqual(calls,[(3001,i) for i in range(4)])
            self.assertEqual(result['slots'][3]['address'],'server3:21124')
            self.assertEqual(result['slots'][0]['period_raw'],0x1234)
            self.assertNotIn('raw_hex',result['slots'][0])

    async def test_unknown_layout_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt=_runtime(tmp)
            class Session:
                async def query_sensor(self,*args):return {'id':3001,'raw_hex':'00'}
            rt._inject_session=Session()
            with self.assertRaises(runtime_module.CommandError):
                await rt.async_read_server_slots()

class ServerAddressWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_guard_and_preserve_protocol_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt=_runtime(tmp,direct_control=True);writes=[]
            raw=b'old:12'.ljust(32,b'\0')+b'\x12\x34\x03'
            class Session:
                async def query_sensor(self,*args):return {'id':3001,'raw_hex':raw.hex()}
                async def set_setting(self,*args):writes.append(args);return {'success':True}
            rt._inject_session=Session()
            with self.assertRaises(runtime_module.CommandError):await rt.async_set_server_address(4,'new:42','different:12')
            self.assertFalse(writes)
            result=await rt.async_set_server_address(4,'new:42','old:12')
            self.assertTrue(result['changed']);self.assertEqual(writes[0][:2],(3001,3))
            self.assertEqual(writes[0][2][-3:],raw[-3:]);self.assertEqual(len(writes[0][2]),35)


if __name__ == "__main__":
    unittest.main(verbosity=2)
