"""HA adapter for the authenticated cloudru telemetry/command relay."""
import asyncio
import json
import logging
import time
from pathlib import Path
from . import protocol
from .relay_wire import MAGIC, handshake

_LOGGER = logging.getLogger(__name__)


class RemoteSession:
    def __init__(self, channel, runtime):
        self.channel, self.runtime = channel, runtime
        self.writer = channel.writer
        self.last_seen = 0
        self.authenticated = True
        self.authenticated_event = asyncio.Event()
        self.authenticated_event.set()
        self.pending = {}
        self.sequence = 0
        self.ready = False

    async def call(self, op, setting, sub, value=None, timeout=6):
        if not self.ready:
            raise RuntimeError('No authenticated S4 tracker session on cloudru')
        if op == 'write' and not self.runtime.direct_control:
            raise RuntimeError('Direct control disabled')
        self.sequence += 1
        rid = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self.channel.send({'type':'request','id':rid,'op':op,'setting':setting,
                                     'sub':sub,'value':value,'expires':time.time()+timeout})
            response = await asyncio.wait_for(future, timeout)
            if response.get('error'):
                raise RuntimeError(response['error'])
            return response
        finally:
            self.pending.pop(rid, None)

    async def query_sensor(self, sensor_id, sub_id=0, timeout=6):
        response = await self.call('read', sensor_id, sub_id, timeout=timeout)
        parsed = protocol.parse_command_ans(bytes.fromhex(response['payload']))
        return parsed.get('sensor') if parsed else None

    async def set_setting(self, setting_id, sub_id, val, timeout=6):
        response = await self.call('write', setting_id, sub_id, val.hex(), timeout)
        parsed = protocol.parse_command_ans(bytes.fromhex(response['payload'])) or {}
        data = parsed.get('data', b'')
        result = data[0] if data else parsed.get('result_code', -1)
        return {'success':result == 0,'setting_id':setting_id,'sub_id':sub_id,'result_code':result}


class NoAck:
    def write(self, data):pass
    async def drain(self):pass


async def handle(runtime, reader, writer, initial):
    session = None
    poll = None
    try:
        suffix = await asyncio.wait_for(reader.readexactly(len(MAGIC)-len(initial)), 5)
        if initial + suffix != MAGIC:
            return
        config_path = Path(runtime.hass.config.path('vega_relay.json'))
        config = await runtime.hass.async_add_executor_job(lambda: json.loads(config_path.read_text()))
        channel = await handshake(reader, writer, config['token'], server=True)
        hello = await asyncio.wait_for(channel.recv(), 5)
        if hello.get('type') != 'hello' or str(hello.get('imei')) != str(runtime.imei):
            raise ValueError('Unexpected relay tracker')
        session = RemoteSession(channel, runtime)
        await channel.send({'type':'hello','accepted':True})
        # Relay can be staged while the tracker still connects directly to HA.
        # Only take command ownership when cloudru confirms authenticated S4.
        while True:
            msg = await asyncio.wait_for(channel.recv(), 45)
            kind = msg.get('type')
            if kind == 'status':
                ready = bool(msg.get('s4_ready'))
                session.ready = ready
                runtime._relay_status = {**msg, "connected":True, "received_at":time.time()}
                if msg.get("capture_error") and not getattr(runtime,"_capture_error_notified",False):
                    from homeassistant.components import persistent_notification
                    persistent_notification.async_create(runtime.hass,
                        "На cloudru произошла ошибка записи. Захват трафика может быть неполным; проверьте диск и журнал vega-mitm.",
                        title="Vega: ошибка захвата на cloudru", notification_id="vega_cloud_capture_error")
                    runtime._capture_error_notified=True
                if ready:
                    session.last_seen = float(msg.get('last_seen',0))
                    current = runtime._inject_session
                    if current is not session:
                        if current is not None:
                            current.writer.close()
                        runtime._inject_session = session
                    if poll is None:
                        poll = runtime.hass.async_create_background_task(runtime._inject_poll_loop(session),name='vega_cloud_relay_poll')
                elif runtime._inject_session is session:
                    runtime._inject_session = None
                runtime._publish()
                await channel.send({'type':'pong'})
            elif kind == 'response':
                future = session.pending.get(msg.get('id'))
                if future and not future.done():future.set_result(msg)
            elif kind == 'combine':
                # No ACK is sent over this tap: cloudru owns tracker acknowledgements.
                if time.time()-float(msg['observed']) < 90:
                    runtime._combine_last_seen = float(msg['observed'])
                    await runtime._combine_packet(NoAck(),msg['packet'])
                    runtime._publish()
            elif kind == 'vega' and msg.get('slot') == 4:
                if time.time()-float(msg['observed']) < 90:
                    session.last_seen = float(msg['observed'])
                    runtime._capture('t2c',int(msg['cmd']),bytes.fromhex(msg['payload']))
            else:
                raise ValueError('Unknown relay message')
    except asyncio.CancelledError:
        raise
    except Exception as err:
        _LOGGER.warning('Vega cloud relay disconnected: %s',type(err).__name__)
    finally:
        if poll:
            poll.cancel()
            await asyncio.gather(poll,return_exceptions=True)
        if session:
            runtime._relay_status = {**getattr(runtime,"_relay_status",{}),"connected":False}
            for future in session.pending.values():
                if not future.done():future.set_exception(ConnectionError('Cloud relay disconnected'))
            if runtime._inject_session is session:runtime._inject_session=None
        writer.close()
