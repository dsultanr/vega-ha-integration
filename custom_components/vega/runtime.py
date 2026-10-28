"""Runtime that owns the raw-TCP listener the tracker dials into.

This is the part of the old standalone bridge that has to be a *server*: it runs
an ``asyncio.start_server`` inside Home Assistant's event loop, decodes the Vega
binary and Wialon IPS streams, keeps the latest telemetry snapshot, and (only
when explicitly enabled) sends direct commands back to the vehicle.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import protocol
from .const import DEFAULT_IMEI, ONLINE_TIMEOUT, SENSOR_KEY_MAP

_LOGGER = logging.getLogger(__name__)


# Sensors whose raw polarity is opposite the HA device_class meaning.
# central_lock: raw 1 == locked; HA binary_sensor LOCK uses on=unlocked.
_INVERT_BOOL = {"central_lock"}


def demux_protocol(prefix: bytes) -> str:
    """Classify an inbound connection by its first bytes.

    Wialon Combine starts 0x24 0x24; TLS 0x16; Wialon IPS '#'; everything else is
    treated as the native Vega binary protocol (whose frames begin with a length
    byte). Combine is checked first because a Vega frame may also begin with 0x24.
    """
    if prefix[:2] == b"\x24\x24":
        return "combine"
    if prefix[:1] == b"\x16":
        return "tls"
    if prefix[:1] == b"#":
        return "wialon"
    return "vega"


class CommandError(HomeAssistantError):
    """Raised when a command cannot be dispatched to the vehicle."""


class VegaSession:
    """One authenticated Vega-binary connection and its pending request map."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.peer = writer.get_extra_info("peername")
        self.authenticated = False
        self.connected_at = time.time()
        self.last_seen = time.time()
        self.req_counter = 1000
        self.pending_requests: dict[int, asyncio.Future] = {}
        self.retried_pin = False

    def get_next_req_id(self) -> int:
        self.req_counter = (self.req_counter + 1) & 0x7FFFFFFF
        return self.req_counter

    async def send_raw(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def _request(self, frame: bytes, req_id: int, timeout: float = 6.0) -> dict:
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self.pending_requests[req_id] = future
        await self.send_raw(frame)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending_requests.pop(req_id, None)

    async def query_sensor(self, sensor_id: int, sub_id: int = 0, timeout: float = 6.0) -> dict | None:
        req_id = self.get_next_req_id()
        frame = protocol.build_query_sensor_frame(req_id, sensor_id, sub_id)
        try:
            res = await self._request(frame, req_id, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return res.get("sensor")

    async def set_setting(self, setting_id: int, sub_id: int, val: bytes, timeout: float = 6.0) -> dict:
        req_id = self.get_next_req_id()
        frame = protocol.build_set_setting_frame(req_id, setting_id, sub_id, val)
        t0 = time.time()
        try:
            res = await self._request(frame, req_id, timeout=timeout)
        except asyncio.TimeoutError:
            return {"success": False, "error": "timeout", "elapsed_ms": round((time.time() - t0) * 1000, 1)}
        data = res.get("data", b"")
        res_code = data[0] if len(data) > 0 else res.get("result_code", -1)
        accepted = res_code == 0
        return {
            "success": accepted,
            "setting_id": setting_id,
            "sub_id": sub_id,
            "result_code": res_code,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
        }


class InjectionSession:
    """Injects queries/commands into a cloud-authenticated relayed session.

    In proxy mode the tracker is already authenticated by the OEM cloud, so HA can
    write frames straight into the tracker side of the relay (no PIN needed) and
    match the replies by request id as they pass by in the tracker->cloud pipe.
    A lock serialises writes to the shared tracker writer.
    """

    def __init__(self, writer: asyncio.StreamWriter, lock: asyncio.Lock) -> None:
        self.writer = writer
        self.lock = lock
        self.authenticated = True  # piggybacks the cloud's auth on this session
        self.authenticated_event = asyncio.Event()
        self.last_seen = time.time()
        self.req_counter = 0x50000000
        self.pending_requests: dict[int, asyncio.Future] = {}

    def get_next_req_id(self) -> int:
        self.req_counter = 0x50000000 | ((self.req_counter + 1) & 0x0FFFFFFF)
        return self.req_counter

    async def _request(self, frame: bytes, req_id: int, timeout: float = 6.0) -> dict:
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self.pending_requests[req_id] = future
        async with self.lock:
            self.writer.write(frame)
            await self.writer.drain()
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending_requests.pop(req_id, None)

    async def query_sensor(self, sensor_id: int, sub_id: int = 0, timeout: float = 6.0) -> dict | None:
        req_id = self.get_next_req_id()
        frame = protocol.build_query_sensor_frame(req_id, sensor_id, sub_id)
        try:
            res = await self._request(frame, req_id, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return res.get("sensor")

    async def set_setting(self, setting_id: int, sub_id: int, val: bytes, timeout: float = 6.0) -> dict:
        req_id = self.get_next_req_id()
        frame = protocol.build_set_setting_frame(req_id, setting_id, sub_id, val)
        _LOGGER.debug("Vega[inject] set_setting %s/%s=%s req_id=0x%X TX=%s", setting_id, sub_id, val.hex(), req_id, frame.hex())
        try:
            res = await self._request(frame, req_id, timeout=timeout)
        except asyncio.TimeoutError:
            return {"success": False, "error": "timeout"}
        data = res.get("data", b"")
        res_code = data[0] if len(data) > 0 else res.get("result_code", -1)
        _LOGGER.debug(
            "Vega[inject] set_setting %s/%s REPLY subcmd=0x%02X result_code=%s data=%s",
            setting_id, sub_id, res.get("subcmd", -1), res.get("result_code"), data.hex(),
        )
        return {"success": res_code == 0, "setting_id": setting_id, "sub_id": sub_id, "result_code": res_code}


# --- firmware / file-transfer capture ------------------------------------
# Frames that carry files/firmware over the Vega binary channel. GET_DEV_INF
# (0x0A/0x0B) is deliberately NOT here: it is routine device-info polling, tens
# of thousands of frames. These only ever appear on the proxy relay path, so
# capture is inherently proxy-only.
#
# The internal payload layout of these frames is NOT reverse-engineered (neither
# the JADX audit nor the official rev11 doc describe per-chunk offset/name
# headers), so we never try to be clever: every payload is recorded losslessly
# as hex in a .jsonl, and the raw payload bytes are also concatenated best-effort
# into a .payloads.bin. The .jsonl is the source of truth; the .bin may contain
# small per-chunk headers we cannot strip.
FIRMWARE_CMDS = {0x03, 0x04, 0x08, 0x09, 0x13, 0x14}
_FW_CMD_NAMES = {
    0x03: "FILE_CHUNK", 0x04: "FILE_CHUNK_ANS",
    0x08: "GET_FILE", 0x09: "GET_FILE_ANS",
    0x13: "FILE_WHOLLY", 0x14: "FILE_WHOLLY_ANS",
}
_FW_IDLE_FINALIZE_SEC = 20.0  # a transfer is "done" after this quiet gap


class _FileCapture:
    """Captures Vega file/firmware frames to disk and notifies via Home Assistant.

    Called from the event loop for every FIRMWARE_CMDS frame seen on the relay.
    A "transfer" is a burst of such frames; it is finalized after a quiet gap,
    which computes size + sha256 (off-loop) and raises a completion notification.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._dir = hass.config.path("vega_filecap")
        self._active = False
        self._base: str | None = None
        self._jsonl = None
        self._binf = None
        self._frames = 0
        self._bytes = 0
        self._kinds: set[str] = set()
        self._first_ts: str | None = None
        self._imei = "unknown"
        self._seq = 0  # disambiguates transfers that start within the same second
        self._finalize_handle = None  # asyncio.TimerHandle

    def feed(self, direction: str, cmd: int, payload: bytes, imei: str) -> None:
        now = time.time()
        if not self._active:
            self._imei = imei or "unknown"
            self._start(now)
            if not self._active:
                return
        name = _FW_CMD_NAMES.get(cmd, f"0x{cmd:02X}")
        try:
            self._jsonl.write(json.dumps(
                {"ts": now, "dir": direction, "cmd": cmd, "cmd_name": name,
                 "len": len(payload), "hex": payload.hex()},
                ensure_ascii=False) + "\n")
            self._jsonl.flush()
            self._binf.write(payload)
            self._binf.flush()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Vega filecap: write failed: %s", err)
        self._frames += 1
        self._bytes += len(payload)
        self._kinds.add(name)
        if self._finalize_handle is not None:
            self._finalize_handle.cancel()
        self._finalize_handle = self.hass.loop.call_later(_FW_IDLE_FINALIZE_SEC, self._finalize)

    def _start(self, now: float) -> None:
        try:
            os.makedirs(self._dir, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        self._first_ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        self._seq += 1
        self._base = os.path.join(
            self._dir, f"vega_file_{self._imei}_{self._first_ts}_{self._seq:03d}")
        try:
            self._jsonl = open(self._base + ".jsonl", "a", encoding="utf-8")
            self._binf = open(self._base + ".payloads.bin", "ab")
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Vega filecap: cannot open capture files: %s", err)
            self._jsonl = self._binf = None
            return
        self._active = True
        self._frames = 0
        self._bytes = 0
        self._kinds = set()
        _LOGGER.warning(
            "Vega: FILE/FIRMWARE transfer with cloud STARTED; capturing to %s.*", self._base)
        self._notify(
            "vega_filecap_" + self._imei,
            "Vega: файловый обмен (возможно прошивка)",
            "Начался файловый обмен трекера Vega с облаком — идёт захват.\n"
            f"Файлы: `{self._base}.jsonl` и `.payloads.bin`.",
        )

    def _finalize(self) -> None:
        self._finalize_handle = None
        if not self._active:
            return
        self._active = False
        jf, bf = self._jsonl, self._binf
        self._jsonl = self._binf = None
        for f in (jf, bf):
            try:
                if f:
                    f.close()
            except Exception:  # noqa: BLE001
                pass
        self.hass.async_create_task(self._finalize_async(
            self._base, self._imei, self._frames, self._bytes,
            sorted(self._kinds), self._first_ts))

    async def _finalize_async(self, base, imei, frames, nbytes, kinds, first_ts) -> None:
        size, digest = await self.hass.async_add_executor_job(self._digest, f"{base}.payloads.bin")
        _LOGGER.warning(
            "Vega: FILE/FIRMWARE transfer CAPTURED: %d frames, %d payload bytes, "
            "bin=%d B sha256=%s kinds=%s files=%s.*",
            frames, nbytes, size, digest, ",".join(kinds), base)
        self._notify(
            "vega_filecap_" + imei,
            "Vega: файловый обмен завершён",
            "Файловый обмен трекера Vega с облаком завершён.\n"
            f"Кадры: {', '.join(kinds)}\n"
            f"Кадров: {frames}, payload: {nbytes} Б, bin: {size} Б\n"
            f"sha256: `{digest}`\n"
            f"Начало: {first_ts}\n"
            f"Файлы: `{base}.jsonl`, `{base}.payloads.bin`\n"
            "⚠️ bin — best-effort конкатенация payload'ов (layout файловых кадров не вскрыт); "
            "для точной сборки — jsonl.",
        )

    @staticmethod
    def _digest(binpath: str):
        import hashlib
        h = hashlib.sha256()
        size = 0
        try:
            with open(binpath, "rb") as f:
                for blk in iter(lambda: f.read(65536), b""):
                    h.update(blk)
                    size += len(blk)
            return size, h.hexdigest()
        except Exception:  # noqa: BLE001
            return size, ""

    def _notify(self, nid: str, title: str, message: str) -> None:
        try:
            from homeassistant.components import persistent_notification
            persistent_notification.async_create(
                self.hass, message, title=title, notification_id=nid)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Vega filecap: notify failed: %s", err)


class VegaRuntime:
    """Owns the listener, the telemetry snapshot and the command path."""

    def __init__(
        self,
        hass: HomeAssistant,
        imei: str,
        port: int,
        pin: str,
        pin2: str,
        poll_interval: int,
        proxy_mode: bool = False,
        direct_control: bool = False,
        upstream: tuple[str, int] | None = None,
        learned_pin: str | None = None,
        learned_imei: str = "",
        persist_cb: Callable[[dict], None] | None = None,
    ) -> None:
        self.hass = hass
        self.imei = imei
        self.port = port
        self.pin = pin
        self.pin2 = pin2
        self.poll_interval = poll_interval
        # Two independent switches:
        #   proxy_mode     -> relay Vega connections to the cloud + learn PIN/IMEI
        #   direct_control -> terminate locally and allow commands (SET_SETTING)
        self.proxy_mode = proxy_mode
        self.direct_control = direct_control
        self.upstream = upstream
        self.learned_pin = learned_pin
        self.learned_imei = learned_imei or ""
        self._persist_cb = persist_cb

        self._server: asyncio.AbstractServer | None = None
        self._poll_task: asyncio.Task | None = None
        self._update_cb: Callable[[dict], None] | None = None

        self._vega_sessions: dict[Any, VegaSession] = {}
        self._wialon_last_seen: float = 0.0
        self._wialon_imei: str = learned_imei or ""

        self._sensors: dict[str, Any] = {}  # state_key -> value
        self._position: dict[str, Any] = {}  # latitude/longitude/... from Wialon
        self._last_remote_state: Any = None
        self._inject_session: InjectionSession | None = None  # active proxy injection
        self._can_scripts: list[dict] = []  # decoded Setting 5502 slots
        self._slot_names: dict[int, str] = {}  # slot index -> name (for button labels)
        self._scripts_lock = asyncio.Lock()
        self._scenarios: list[dict] = []
        self._scenario_read_at: str | None = None
        self._filecap = _FileCapture(hass)  # firmware/file-transfer catcher (proxy path)

    @property
    def _auth_pin(self) -> str:
        """PIN HA presents to the tracker in direct mode: learned one wins."""
        return self.learned_pin or self.pin

    # --- lifecycle -----------------------------------------------------------

    def set_update_callback(self, cb: Callable[[dict], None]) -> None:
        self._update_cb = cb

    async def start(self) -> None:
        self.load_scripts()  # restore slot names/scripts from disk for labels
        self._server = await asyncio.start_server(self._handle_connection, "0.0.0.0", self.port)
        self._poll_task = self.hass.async_create_background_task(
            self._poll_loop(), name=f"vega_poll_{self.imei}"
        )
        _LOGGER.info(
            "Vega listener bound on 0.0.0.0:%s (proxy_mode=%s direct_control=%s)",
            self.port, self.proxy_mode, self.direct_control,
        )

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    # --- snapshot ------------------------------------------------------------

    def _active_vega_session(self) -> VegaSession | None:
        now = time.time()
        latest: VegaSession | None = None
        for s in list(self._vega_sessions.values()):
            if s.authenticated and (now - s.last_seen) < 120:
                if latest is None or s.last_seen > latest.last_seen:
                    latest = s
        return latest

    def snapshot(self) -> dict:
        now = time.time()
        last_vega = max((s.last_seen for s in self._vega_sessions.values()), default=0.0)
        if self._inject_session:
            last_vega = max(last_vega, self._inject_session.last_seen)
        last_seen = max(last_vega, self._wialon_last_seen)
        is_online = (now - last_seen) < ONLINE_TIMEOUT if last_seen else False

        data: dict[str, Any] = {}
        data.update(self._sensors)
        data.update(self._position)
        data["is_online"] = is_online
        speed = data.get("speed")
        data["is_moving"] = bool(is_online and isinstance(speed, (int, float)) and speed > 0)
        if last_seen:
            data["last_online"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(last_seen))
        # onboarding diagnostics
        data["learned_imei"] = self.imei if (self.imei and self.imei != DEFAULT_IMEI) else (self._wialon_imei or self.learned_imei or self.imei)
        data["pin_learned"] = bool(self.learned_pin)
        if self.proxy_mode and self.direct_control:
            data["mode"] = "proxy+direct"
        elif self.direct_control:
            data["mode"] = "direct"
        elif self.proxy_mode:
            data["mode"] = "proxy"
        else:
            data["mode"] = "idle"
        return data

    def _publish(self) -> None:
        if self._update_cb is None:
            return
        # An entity-update error (e.g. a bad value for one sensor) must never
        # propagate up and tear down the tracker connection.
        try:
            self._update_cb(self.snapshot())
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Vega: publish/update error ignored (session kept): %s", err)

    def _ingest_sensor(self, sensor: dict) -> None:
        name = sensor.get("name")
        key = SENSOR_KEY_MAP.get(name)
        if key is not None:
            value = sensor.get("value")
            # Polarity fix: raw central_lock 1 == locked, but HA LOCK uses
            # on=unlocked, so invert.
            if name in _INVERT_BOOL and isinstance(value, bool):
                value = not value
            self._sensors[key] = value

    # --- connection demux ----------------------------------------------------

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            initial = await asyncio.wait_for(reader.read(1), timeout=1.5)
        except asyncio.TimeoutError:
            initial = b""
        # Wialon Combine opens with 0x24 0x24, but a Vega binary frame can also
        # start with 0x24 (payload length 36), so read the second byte to tell
        # them apart before classifying.
        if initial[:1] == b"\x24":
            try:
                initial += await asyncio.wait_for(reader.read(1), timeout=1.5)
            except asyncio.TimeoutError:
                pass

        kind = demux_protocol(initial)
        if kind == "tls":
            # A TLS ClientHello: this integration does not terminate TLS, drop it.
            _LOGGER.debug("Vega: dropping TLS connection from %s (not supported)", peer)
            writer.close()
            return
        if kind == "combine":
            await self._handle_combine(reader, writer, initial)
        elif kind == "wialon":
            await self._handle_wialon(reader, writer, initial)
        elif self.proxy_mode:
            # Relay this Vega connection to the cloud (keeps the OEM cloud/app
            # online) and learn PIN/IMEI. If direct_control is also on, HA injects
            # its own queries/commands into the same (cloud-authenticated) session
            # in parallel — the cloud relay keeps running.
            await self._handle_vega_proxy(reader, writer, initial)
        else:
            # No proxy: terminate locally (direct-only tracker). Commands need
            # direct_control and the (learned) PIN.
            await self._handle_vega(reader, writer, initial)

    async def _handle_combine(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, initial: bytes) -> None:
        """Stub for Wialon Combine (0x24 0x24).

        Combine is not decoded here yet (the integration expects a Wialon IPS +
        Vega binary tracker). This branch exists only to recognise Combine and
        keep it off the Vega binary path, where its bytes would be mis-parsed. It
        drains the connection quietly (no ACKs) instead of corrupting a session.
        """
        peer = writer.get_extra_info("peername")
        _LOGGER.warning(
            "Vega: Wialon Combine connection from %s is not supported (stub); draining. "
            "Point the tracker's telemetry slot at Wialon IPS (0x02) instead.", peer
        )
        try:
            while True:
                if not await reader.read(4096):
                    break
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    # --- Vega binary ---------------------------------------------------------

    async def _handle_vega(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, initial: bytes) -> None:
        session = VegaSession(writer)
        peer = session.peer
        self._vega_sessions[peer] = session
        _LOGGER.info("Vega binary session from %s", peer)
        try:
            await session.send_raw(protocol.build_auth_frame(self._auth_pin))
            if not self.imei or self.imei == DEFAULT_IMEI:
                await session.send_raw(protocol.build_frame(protocol.CMD_GET_DEV_INF, b""))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Vega: error sending auth to %s: %s", peer, err)

        buf = bytearray(initial)
        try:
            while True:
                chunk = await reader.read(2048)
                if not chunk:
                    break
                session.last_seen = time.time()
                buf.extend(chunk)
                for cmd, payload in protocol.iter_frames(buf):
                    await self._process_vega_frame(session, cmd, payload)
                self._publish()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Vega: connection error from %s: %s", peer, err)
        finally:
            self._vega_sessions.pop(peer, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _process_vega_frame(self, session: VegaSession, cmd: int, payload: bytes) -> None:
        if cmd == protocol.CMD_AUTORIZATION:
            await session.send_raw(protocol.build_auth_ack_frame(True))
            session.authenticated = True
        elif cmd == protocol.CMD_AUTORIZATION_ANS:
            status = payload[0] if payload else 0
            if status == 0x01:
                session.authenticated = True
                _LOGGER.info("Vega: tracker %s authorized", session.peer)
            elif not session.retried_pin:
                session.retried_pin = True
                await session.send_raw(protocol.build_auth_frame(self.pin2))
            else:
                _LOGGER.warning("Vega: tracker %s auth rejected with both PINs", session.peer)
        elif cmd == protocol.CMD_COMMAND_ANS:
            parsed = protocol.parse_command_ans(payload)
            if parsed is None:
                return
            fut = session.pending_requests.get(parsed["req_id"])
            if fut is not None and not fut.done():
                fut.set_result(parsed)
            if "sensor" in parsed:
                self._ingest_sensor(parsed["sensor"])
        elif cmd == protocol.CMD_COMMAND:
            # Unsolicited telemetry/command frame from the tracker: ACK it.
            if len(payload) >= 4:
                req_id = struct.unpack("<I", payload[:4])[0]
                ans = struct.pack("<IBB", req_id, 0x00, 0)
                await session.send_raw(protocol.build_frame(protocol.CMD_COMMAND_ANS, ans))
        elif cmd == protocol.CMD_GET_DEV_INF_ANS:
            imei = protocol.parse_imei(payload)
            if imei:
                self._learn(imei=imei)
        elif cmd == protocol.CMD_GET_FILE:
            if len(payload) >= 4:
                req_id = struct.unpack("<I", payload[:4])[0]
                ans = struct.pack("<IB", req_id, 0x01)
                await session.send_raw(protocol.build_frame(protocol.CMD_GET_FILE_ANS, ans))

    # --- proxy / onboarding (MITM relay + learn PIN/IMEI) --------------------

    async def _handle_vega_proxy(self, reader, writer, initial: bytes) -> None:
        """Transparently relay a Vega connection to the cloud and learn PIN/IMEI.

        The tracker keeps talking to its real server through us; we only observe.
        The per-slot PIN (cloud->tracker CMD_AUTORIZATION) and the IMEI
        (tracker->cloud GET_DEV_INF_ANS) are captured so the user can then flip to
        direct control with a known PIN. Any CMD_COMMAND_ANS seen is also decoded
        so telemetry already appears during onboarding.
        """
        peer = writer.get_extra_info("peername")
        if not self.upstream:
            _LOGGER.warning("Vega proxy: no upstream configured, draining %s", peer)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(*self.upstream), timeout=8
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Vega proxy: upstream %s unreachable: %s", self.upstream, err)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            return
        _LOGGER.info(
            "Vega proxy: relaying %s <-> %s:%s (direct_control=%s)",
            peer, *self.upstream, self.direct_control,
        )
        if initial:
            up_writer.write(initial)
            await up_writer.drain()

        # Injection runs in parallel with the relay: HA writes its own frames to
        # the tracker (shared writer, locked) and matches replies in the t2c pipe.
        tracker_lock = asyncio.Lock()
        inject = InjectionSession(writer, tracker_lock)
        self._inject_session = inject
        poll_task = self.hass.async_create_background_task(
            self._inject_poll_loop(inject), name="vega_inject_poll"
        )

        async def t2c() -> None:  # tracker -> cloud (relay + capture + resolve inject)
            pbuf = bytearray()
            try:
                while True:
                    chunk = await reader.read(4096)
                    if not chunk:
                        break
                    up_writer.write(chunk)
                    await up_writer.drain()
                    inject.last_seen = time.time()
                    self._wialon_last_seen = time.time()
                    pbuf.extend(chunk)
                    for cmd, payload in protocol.iter_frames(pbuf):
                        self._capture("t2c", cmd, payload, inject)
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    up_writer.close()
                except Exception:  # noqa: BLE001
                    pass

        async def c2t() -> None:  # cloud -> tracker (locked; shares writer with inject)
            pbuf = bytearray()
            try:
                while True:
                    chunk = await up_reader.read(4096)
                    if not chunk:
                        break
                    async with tracker_lock:
                        writer.write(chunk)
                        await writer.drain()
                    pbuf.extend(chunk)
                    for cmd, payload in protocol.iter_frames(pbuf):
                        self._capture("c2t", cmd, payload, inject)
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            await asyncio.gather(t2c(), c2t())
        finally:
            if poll_task:
                poll_task.cancel()
            if self._inject_session is inject:
                self._inject_session = None

    async def _inject_poll_loop(self, inject: "InjectionSession") -> None:
        """Poll sensors by injecting queries into the relayed (cloud-authed) session."""
        try:
            # Wait for cloud authorization before injecting queries
            await asyncio.wait_for(inject.authenticated_event.wait(), timeout=15.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return
        await asyncio.sleep(2.0)

        # If IMEI is still unknown, query device info now that we are authenticated
        if not self.imei or self.imei == DEFAULT_IMEI:
            try:
                async with inject.lock:
                    inject.writer.write(protocol.build_frame(protocol.CMD_GET_DEV_INF, b""))
                    await inject.writer.drain()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Vega: query IMEI failed: %s", err)
            await asyncio.sleep(1.0)

        while True:
            try:
                changed = False
                for sid in protocol.SENSOR_DEFS:
                    sensor = await inject.query_sensor(sid, 0)
                    if sensor is not None:
                        self._ingest_sensor(sensor)
                        changed = True
                if changed:
                    self._publish()
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Vega inject poll error: %s", err)
                await asyncio.sleep(self.poll_interval)

    def _capture(self, direction: str, cmd: int, payload: bytes, inject: "InjectionSession | None" = None) -> None:
        _LOGGER.debug("Vega capture [%s]: cmd=0x%02X len=%d", direction, cmd, len(payload))
        if cmd in FIRMWARE_CMDS:
            # File/firmware frame (either direction) — capture losslessly + notify.
            self._filecap.feed(direction, cmd, payload, self.imei)
        # Log the full bytes of cloud->tracker COMMAND frames so we can see exactly
        # what the Evolute cloud sends for each app action (lock/unlock/etc).
        if direction == "c2t" and cmd == protocol.CMD_COMMAND:
            _LOGGER.debug("Vega capture [c2t] CMD payload=%s", payload.hex())
        if direction == "c2t" and cmd == protocol.CMD_AUTORIZATION:
            pin = protocol.parse_authorization_pin(payload)
            if pin and pin != self.learned_pin:
                self._learn(pin=pin)
        elif direction == "t2c" and cmd == protocol.CMD_AUTORIZATION_ANS:
            status = payload[0] if payload else 0
            if status == 0x01 and inject is not None:
                _LOGGER.info("Vega: cloud authentication confirmed, starting telemetry polling")
                inject.authenticated_event.set()
        elif direction == "t2c" and cmd == protocol.CMD_GET_DEV_INF_ANS:
            imei = protocol.parse_imei(payload)
            if imei:
                self._learn(imei=imei)
        elif cmd == protocol.CMD_COMMAND_ANS:
            parsed = protocol.parse_command_ans(payload)
            if not parsed:
                return
            # resolve an injected request if this reply is one of ours
            if inject is not None:
                fut = inject.pending_requests.get(parsed["req_id"])
                if fut is not None and not fut.done():
                    fut.set_result(parsed)
            if "sensor" in parsed:
                self._ingest_sensor(parsed["sensor"])
                self._publish()

    def _learn(self, pin: str | None = None, imei: str | None = None) -> None:
        changed = {}
        if pin and pin != self.learned_pin:
            self.learned_pin = pin
            changed["learned_pin"] = pin
            _LOGGER.info("Vega proxy: learned PIN for this slot")
        if imei and imei != self.learned_imei and imei != DEFAULT_IMEI:
            self._wialon_imei = imei
            self.imei = imei
            self.learned_imei = imei
            changed["learned_imei"] = imei
            changed["imei"] = imei
            _LOGGER.info("Vega: learned tracker IMEI: %s", imei)
            self.load_scripts()
        if changed and self._persist_cb is not None:
            try:
                self._persist_cb(changed)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Vega: persist learned data failed: %s", err)
        self._publish()

    # --- Wialon IPS (telemetry/position only, no Traccar) --------------------

    async def _handle_wialon(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, initial: bytes) -> None:
        peer = writer.get_extra_info("peername")
        _LOGGER.info("Wialon IPS session from %s", peer)
        buf = bytearray(initial)
        try:
            while True:
                chunk = await reader.read(2048)
                if not chunk:
                    break
                self._wialon_last_seen = time.time()
                buf.extend(chunk)
                while b"\n" in buf:
                    idx = buf.find(b"\n")
                    line_bytes = bytes(buf[: idx + 1])
                    del buf[: idx + 1]
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if line:
                        await self._process_wialon_line(writer, line)
                self._publish()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Wialon: connection error from %s: %s", peer, err)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _process_wialon_line(self, writer: asyncio.StreamWriter, line: str) -> None:
        async def _send(data: bytes) -> None:
            writer.write(data)
            await writer.drain()

        if line.startswith("#L#"):
            payload = line[3:].rstrip("#")
            parts = payload.split(";")
            if len(parts) >= 2 and parts[0] in ("2.0", "1.1"):
                self._wialon_imei = parts[1]
            else:
                self._wialon_imei = parts[0] if parts else ""
            await _send(b"#AL#1\r\n")
            if self._wialon_imei and self._wialon_imei != DEFAULT_IMEI:
                self._learn(imei=self._wialon_imei)
            return
        if line.startswith("#P#"):
            await _send(b"#AP#\r\n")
            return

        if line.startswith("#D#") or line.startswith("#SD#") or line.startswith("#B#"):
            parsed = protocol.parse_wialon_data(line)
            if parsed:
                params = parsed.pop("params", None)
                for key, value in parsed.items():
                    if value is not None:
                        self._position[key] = value
                if isinstance(params, dict):
                    self._position.setdefault("_params", {}).update(params)
            if line.startswith("#D#"):
                await _send(b"#AD#1\r\n")
            elif line.startswith("#SD#"):
                await _send(b"#AD#1\r\n")
            else:
                cnt = max(1, line.count("|"))
                await _send(f"#AB#{cnt}\r\n".encode("ascii"))

    # --- poll loop + command path -------------------------------------------

    async def _poll_loop(self) -> None:
        await asyncio.sleep(1.0)
        while True:
            try:
                session = self._active_vega_session()
                if session is not None:
                    changed = False
                    for sid in protocol.SENSOR_DEFS:
                        sensor = await session.query_sensor(sid, 0)
                        if sensor is not None:
                            self._ingest_sensor(sensor)
                            changed = True
                    if self.direct_control:
                        await self._remote_state_watchdog(session)
                    if changed:
                        self._publish()
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Vega poll loop error: %s", err)
                await asyncio.sleep(self.poll_interval)

    async def _remote_state_watchdog(self, session: VegaSession) -> None:
        """Clear the levelled output 1341 when a Remote session ends by timer.

        This is a *write* to the vehicle, so it only runs when commands are
        enabled by the owner. 3 -> not-3 means the car left Remote on its own.
        """
        sensor = await session.query_sensor(protocol.REMOTE_STATE_SENSOR, 0)
        cur = sensor.get("value") if sensor else None
        prev = self._last_remote_state
        self._last_remote_state = cur
        if prev == 3 and cur is not None and cur != 3:
            _LOGGER.info("Vega: Remote session ended (%s -> %s), clearing output 1341", prev, cur)
            await session.set_setting(protocol.REMOTE_LEVEL_SETTING, 0, bytes([0]))

    async def async_poll_once(self) -> None:
        """One-shot sensor poll (used for a quick post-command confirmation)."""
        session = self._inject_session or self._active_vega_session()
        if session is None:
            return
        changed = False
        for sid in protocol.SENSOR_DEFS:
            try:
                sensor = await session.query_sensor(sid, 0)
            except Exception:  # noqa: BLE001
                sensor = None
            if sensor is not None:
                self._ingest_sensor(sensor)
                changed = True
        if changed:
            self._publish()

    # --- CAN scripts (Setting 5502 slots): read / view / write ---------------

    def slot_name(self, slot: int) -> str | None:
        return self._slot_names.get(slot)

    @property
    def can_scripts(self) -> list[dict]:
        return self._can_scripts

    @property
    def scenarios(self) -> list[dict]:
        return self._scenarios

    @property
    def command_bindings(self) -> dict:
        return protocol.command_script_bindings(self._scenarios if self._scenario_read_at else [])

    @property
    def scenario_read_at(self) -> str | None:
        return self._scenario_read_at

    def command_slot(self, action: str) -> int | None:
        binding = self.command_bindings.get(action, {})
        if binding.get("status") == "single":
            return binding["slots"][0]
        return None

    def _scripts_path(self) -> str:
        return os.path.join(self.hass.config.path("vega"), f"can_scripts_{self.imei}.json")

    def load_scripts(self) -> None:
        """Load the last-saved scripts from disk so labels survive a restart."""
        self._can_scripts = []
        self._slot_names = {}
        self._scenarios = []
        self._scenario_read_at = None
        try:
            path = self._scripts_path()
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    self._can_scripts = json.load(f)
                # Descriptions/summary are derived data: always recompute them
                # from the stable raw frames so a parser change takes effect on
                # the next load (never trust a cached description from disk).
                for s in self._can_scripts:
                    for f_item in s.get("frames", []):
                        f_item["description"] = protocol.explain_can_frame(
                            f_item.get("bus", 0), f_item.get("can_id", 0), f_item.get("data", "")
                        )
                    s["summary"] = protocol.explain_can_script(s)
                self._slot_names = {
                    int(s["slot"]): s["name"] for s in self._can_scripts if s.get("name")
                }
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Vega: load scripts failed: %s", err)

    async def _persist_scripts(self) -> None:
        path = self._scripts_path()
        data = self._can_scripts

        def _write() -> None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(path + ".tmp", path)

        try:
            await self.hass.async_add_executor_job(_write)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Vega: persist scripts failed: %s", err)

    def _script_session(self):
        session = self._inject_session or self._active_vega_session()
        if session is None:
            raise CommandError("Vehicle is offline (no active Vega session)")
        return session

    @staticmethod
    async def _read_setting(session, setting: int, sub: int, size: int) -> bytes:
        sensor = await session.query_sensor(setting, sub)
        if not sensor or sensor.get("id") != setting:
            raise CommandError(f"Нет корректного ответа на чтение {setting}/{sub}")
        try:
            buf = bytes.fromhex(sensor["raw_hex"])
        except (KeyError, TypeError, ValueError) as err:
            raise CommandError(f"Некорректные данные {setting}/{sub}") from err
        if len(buf) != size:
            raise CommandError(f"{setting}/{sub}: прочитано {len(buf)} байт вместо {size}")
        return buf

    def _store_script(self, slot: int, buf: bytes) -> None:
        dec = protocol.decode_can_script(buf)
        dec.update(slot=slot, read_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        # Do not offer a lossy edit of a format this encoder cannot reproduce.
        try:
            dec["editable"] = protocol.build_can_script_from_dict(
                {k: dec[k] for k in ("name", "repeat", "frames")}
            ) == buf
        except ValueError:
            dec["editable"] = False
        self._can_scripts = sorted(
            [s for s in self._can_scripts if s["slot"] != slot] + [dec], key=lambda s: s["slot"]
        )
        self._slot_names[slot] = dec["name"]

    async def async_read_can_scripts(self) -> list[dict]:
        """Read scripts and all scenario routes. Never infer routes from slot names."""
        async with self._scripts_lock:
            self._scenarios = []
            self._scenario_read_at = None
            self._publish()
            session = self._script_session()
            scripts = []
            for slot in range(protocol.CAN_SCRIPT_COUNT):
                scripts.append(await self._read_setting(session, 5502, slot, protocol.CAN_SCRIPT_SIZE))
            for slot, buf in enumerate(scripts):
                self._store_script(slot, buf)
            await self._persist_scripts()
            self._publish()
            # Script editing remains usable if the firmware cannot supply 4001.
            # In that case the UI explicitly reports unknown command bindings.
            try:
                scenarios = []
                for index in range(protocol.SCENARIO_COUNT):
                    raw = await self._read_setting(session, 4001, index, 50)
                    scenarios.append(protocol.decode_scenario(raw, index))
                self._scenarios = scenarios
                self._scenario_read_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                await self._persist_scenarios()
            except (CommandError, ValueError, OSError) as err:
                _LOGGER.warning("Vega: scripts read; scenario bindings unavailable: %s", err)
            self._publish()
            return self._can_scripts

    async def _persist_scenarios(self) -> None:
        path = self.hass.config.path("vega", f"scenarios_{self.imei}.json")
        data = self._scenarios

        def write():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(path + ".tmp", path)

        try:
            await self.hass.async_add_executor_job(write)
        except OSError as err:
            _LOGGER.warning("Vega: could not cache scenarios: %s", err)

    def _store_config_record(self, key: str, raw: bytes | None) -> None:
        setting, index = map(int, key.split("/"))
        if setting == 5502:
            if raw is not None:
                self._store_script(index, raw)
            else:
                self._can_scripts = [s for s in self._can_scripts if s["slot"] != index]
                self._slot_names.pop(index, None)
            return
        self._scenarios = [s for s in self._scenarios if s["index"] != index]
        if raw is not None:
            try:
                self._scenarios.append(protocol.decode_scenario(raw, index))
                self._scenarios.sort(key=lambda s: s["index"])
            except ValueError:
                self._scenario_read_at = None
        else:
            self._scenario_read_at = None

    @staticmethod
    def _prepare_command_edit(document: dict, expected: dict) -> tuple[dict, dict]:
        """Validate the entire edit before sending any requests to the tracker."""
        if not isinstance(document, dict) or not isinstance(expected, dict):
            raise ValueError("document и expected должны быть объектами")
        protocol._check_fields(document, {"scenarios", "scripts"}, set(), "document")
        originals = {}
        for key, value in expected.items():
            if not isinstance(key, str):
                raise ValueError("Некорректный ключ снимка")
            parts = key.split("/")
            if len(parts) != 2 or parts[0] not in ("4001", "5502") or not parts[1].isdigit():
                raise ValueError("Ключ снимка: 4001/индекс или 5502/слот")
            setting, index = map(int, parts)
            limit, size = (25, 50) if setting == 4001 else (8, 82)
            if not 0 <= index < limit or key != f"{setting}/{index}":
                raise ValueError("Индекс снимка вне диапазона")
            if not isinstance(value, str):
                raise ValueError("Исходные данные должны быть HEX-строкой")
            raw = bytes.fromhex(value)
            if len(raw) != size:
                raise ValueError(f"{key}: в снимке требуется {size} байт")
            originals[key] = raw
        requested = {}
        for field, setting, index_key, maximum in (("scenarios", 4001, "index", 25), ("scripts", 5502, "slot", 8)):
            items = document[field]
            if not isinstance(items, list) or len(items) > maximum:
                raise ValueError(f"{field}: требуется список, максимум {maximum}")
            for item in items:
                if not isinstance(item, dict) or index_key not in item:
                    raise ValueError(f"{field}: отсутствует {index_key}")
                index = protocol._integer(item[index_key], 0, maximum - 1, index_key)
                key = f"{setting}/{index}"
                if key in requested:
                    raise ValueError(f"{key}: повторяется в документе")
                if key not in originals:
                    raise ValueError(f"{key}: перечитайте настройки, исходный снимок отсутствует")
                spec = {k: v for k, v in item.items() if k != index_key}
                if setting == 4001:
                    # Refuse a baseline format the scenario decoder cannot explain.
                    if not protocol.decode_scenario(originals[key], index)["editable"]:
                        raise ValueError(f"{key}: неподдерживаемый формат сценария")
                    requested[key] = protocol.build_scenario_from_dict(spec, originals[key])
                    slot = spec["actions"]["can_script"]
                    if slot is not None and f"5502/{slot}" not in originals:
                        raise ValueError(f"Слот {slot}: нужен исходный снимок связанного CAN-скрипта")
                else:
                    original_spec = protocol.decode_can_script(originals[key])
                    if protocol.build_can_script_from_dict(original_spec) != originals[key]:
                        raise ValueError(f"{key}: формат CAN-скрипта нельзя сохранить без потерь")
                    requested[key] = protocol.build_can_script_from_dict(spec)
        if not requested:
            raise ValueError("Нет сценариев или CAN-скриптов для сохранения")
        return originals, requested

    async def _backup_command_edit(self, changes: dict, originals: dict) -> None:
        path = self.hass.config.path("vega", f"command_backups_{self.imei}.jsonl")
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "records": {k: {"before": originals[k].hex(), "requested": v.hex()} for k, v in changes.items()}}

        def write():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
                f.flush()
                os.fsync(f.fileno())

        try:
            await self.hass.async_add_executor_job(write)
        except OSError as err:
            raise CommandError("Не удалось сохранить исходные настройки; запись отменена") from err

    async def async_write_commands(self, document: dict, expected: dict) -> dict:
        """Save a reviewed command document. No command execution or write retries.

        The device has no multi-record transaction. All baselines are checked
        and backed up first; scripts precede scenario references. A failure
        stops remaining writes and explicitly reports already verified records.
        """
        try:
            originals, requested = self._prepare_command_edit(document, expected)
        except (ValueError, TypeError) as err:
            raise CommandError(str(err)) from err
        async with self._scripts_lock:
            if not self.direct_control:
                raise CommandError("Direct Control выключен")
            session = self._script_session()
            confirmed = []
            attempted = None
            try:
                for key, before in originals.items():
                    setting, index = map(int, key.split("/"))
                    actual = await self._read_setting(session, setting, index, len(before))
                    self._store_config_record(key, actual)
                    if actual != before:
                        self._scenario_read_at = None
                        raise CommandError(f"{key}: настройки изменились после открытия редактора. Перечитайте их; запись отменена")
                changes = {k: v for k, v in requested.items() if v != originals[k]}
                if not changes:
                    return {"verified": True, "unchanged": True, "written": []}
                await self._backup_command_edit(changes, originals)
                # Write referenced CAN content before changing scenario actions.
                for key in sorted(changes, key=lambda k: (not k.startswith("5502/"), int(k.split("/")[1]))):
                    if not self.direct_control:
                        raise CommandError("Direct Control выключен; дальнейшая запись отменена")
                    setting, index = map(int, key.split("/"))
                    before, desired = originals[key], changes[key]
                    current = await self._read_setting(session, setting, index, len(before))
                    self._store_config_record(key, current)
                    if current != before:
                        raise CommandError(f"{key}: настройки изменились перед записью")
                    if not self.direct_control:
                        raise CommandError("Direct Control выключен; дальнейшая запись отменена")
                    attempted = key
                    try:
                        await session.set_setting(setting, index, desired)
                    except (OSError, asyncio.TimeoutError):
                        pass  # A lost ACK can still accompany a successful write.
                    actual = None
                    for attempt in range(3):
                        try:
                            actual = await self._read_setting(session, setting, index, len(desired))
                            if actual == desired:
                                break
                        except (CommandError, OSError, asyncio.TimeoutError):
                            actual = None
                        if attempt < 2:
                            await asyncio.sleep(0.3)
                    self._store_config_record(key, actual)
                    if actual != desired:
                        detail = "нет ответа на чтение" if actual is None else "прочитанные байты отличаются"
                        raise CommandError(f"{key}: запись не подтверждена — {detail}")
                    confirmed.append(key)
                    attempted = None
                return {"verified": True, "unchanged": False, "written": confirmed}
            except (CommandError, OSError, asyncio.TimeoutError) as err:
                self._scenario_read_at = None
                done = ", ".join(confirmed) or "нет"
                pending = f" Возможна неподтверждённая запись {attempted}." if attempted else ""
                raise CommandError(f"{err}. Подтверждённые записи: {done}.{pending} Перечитайте настройки; черновик сохранён в редакторе") from err
            except asyncio.CancelledError:
                if attempted:
                    self._store_config_record(attempted, None)
                self._scenario_read_at = None
                raise
            finally:
                await self._persist_scripts()
                await self._persist_scenarios()
                self._publish()

    async def _backup_script(self, slot: int, before: bytes, requested: bytes) -> None:
        path = self.hass.config.path("vega", f"can_script_backups_{self.imei}.jsonl")
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "slot": slot, "before": before.hex(), "requested": requested.hex()}

        def _write():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
                f.flush()
                os.fsync(f.fileno())

        try:
            await self.hass.async_add_executor_job(_write)
        except OSError as err:
            raise CommandError("Не удалось сохранить исходный слот; запись отменена") from err

    async def async_write_can_script(self, slot: int, spec: dict, expected_raw: str | None = None) -> dict:
        """Write once, then confirm the complete slot by reading it back."""
        try:
            protocol.validate_script_slot(slot)
            buf = protocol.build_can_script_from_dict(spec)
            expected = bytes.fromhex(expected_raw) if expected_raw is not None else None
            if expected is not None and len(expected) != protocol.CAN_SCRIPT_SIZE:
                raise ValueError("expected_raw: ожидалось 82 байта")
        except (ValueError, TypeError) as err:
            raise CommandError(str(err)) from err
        async with self._scripts_lock:
            if not self.direct_control:
                raise CommandError("Direct Control выключен")
            session = self._script_session()
            before = await self._read_setting(session, 5502, slot, protocol.CAN_SCRIPT_SIZE)
            self._store_script(slot, before)
            if expected is not None and expected != before:
                await self._persist_scripts()
                self._publish()
                raise CommandError("Слот изменился после открытия редактора. Прочитайте его заново; запись отменена")
            current = next(s for s in self._can_scripts if s["slot"] == slot)
            if not current["editable"]:
                await self._persist_scripts()
                self._publish()
                raise CommandError("Формат слота нельзя сохранить без потерь; запись отменена")
            if before == buf:
                await self._persist_scripts()
                self._publish()
                return {"slot": slot, "verified": True, "unchanged": True}
            await self._backup_script(slot, before, buf)
            # Direct Control may have been turned off while reading/backing up.
            if not self.direct_control:
                raise CommandError("Direct Control выключен; запись отменена")
            try:
                await session.set_setting(5502, slot, buf)
            except (OSError, asyncio.TimeoutError):
                # An absent ACK is not proof the write failed. Never resend it.
                pass
            actual = None
            for attempt in range(3):
                try:
                    actual = await self._read_setting(session, 5502, slot, protocol.CAN_SCRIPT_SIZE)
                    if actual == buf:
                        break
                except (CommandError, OSError, asyncio.TimeoutError):
                    actual = None
                if attempt < 2:
                    await asyncio.sleep(0.3)
            if actual is not None:
                self._store_script(slot, actual)
            else:
                self._can_scripts = [s for s in self._can_scripts if s["slot"] != slot]
                self._slot_names.pop(slot, None)
            await self._persist_scripts()
            self._publish()
            if actual != buf:
                detail = "ответ на чтение не получен" if actual is None else "прочитанные байты отличаются"
                raise CommandError(f"Запись слота {slot} не подтверждена: {detail}. Прочитайте слот заново")
            _LOGGER.info("Vega: slot %s write verified (82 bytes)", slot)
            return {"slot": slot, "verified": True, "unchanged": False}

    async def async_send_command(self, action: str) -> dict:
        if not self.direct_control:
            raise CommandError("Direct control is off; enable the 'Direct control' switch first.")
        spec = protocol.COMMAND_MAP.get(action)
        if spec is None:
            raise CommandError(f"Unknown Vega command '{action}'")
        # Prefer the proxy injection session (commands ride the cloud-authed relay);
        # otherwise use a locally-terminated authenticated session.
        session = self._inject_session or self._active_vega_session()
        if session is None:
            raise CommandError("Vehicle is offline (no active Vega session)")

        sid, sub, val = spec["setting_id"], spec["sub_id"], bytes([spec["val"]])
        # The car reacts to a 0->1 edge, not a level. If 1341 is already armed the
        # command is accepted (res=0) yet silently ignored, so pre-clear it.
        if sid == protocol.REMOTE_LEVEL_SETTING and spec["val"] == 1:
            await session.set_setting(protocol.REMOTE_LEVEL_SETTING, 0, bytes([0]))
            await asyncio.sleep(1.0)
        result = await session.set_setting(sid, sub, val)
        if not result.get("success"):
            raise CommandError(f"Vega command '{action}' was not accepted: {result}")
        return result
