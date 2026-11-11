"""Durable, session-separated capture of cloud relay bytes, before forwarding."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import secrets
import time

_LOGGER = logging.getLogger(__name__)
SEGMENT_BYTES = 16 * 1024 * 1024


class RawCapture:
    """Record all received bytes independently of the protocol decoder.

    Each JSONL record contains a direction, receive time and base64 TCP chunk.
    Concatenate decoded data per direction/session to reconstruct the stream.
    Files are private and rotated, never automatically deleted. Disk failures
    are reported explicitly; forwarding continues so capture cannot disconnect
    the vehicle. Executor work is awaited (no unbounded background queue).
    """

    def __init__(self, hass):
        self.hass = hass
        self.directory = Path(hass.config.path("vega_rawcap"))
        self.session = f"{time.time_ns()}-{secrets.token_hex(6)}"
        self.segment = 0
        self.size = 0
        self.sequence = 0
        self.failed = False
        self.lock = asyncio.Lock()

    async def feed(self, direction: str, data: bytes) -> None:
        if not data:
            return
        received = time.time_ns()
        async with self.lock:
            self.sequence += 1
            record = {"session": self.session, "seq": self.sequence,
                      "received_ns": received, "direction": direction,
                      "length": len(data),
                      "base64": base64.b64encode(data).decode("ascii")}
            line = (json.dumps(record, separators=(",", ":")) + "\n").encode()
            # Shield the executor and wait even on cancellation: no concurrent
            # writer may enter until this write/fsync has actually finished.
            task = asyncio.ensure_future(self.hass.async_add_executor_job(self._append, line))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await task
                except Exception as err:
                    self._error(err)
                raise
            except Exception as err:
                self._error(err)

    def _append(self, line: bytes) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.size and self.size + len(line) > SEGMENT_BYTES:
            self.segment += 1
            self.size = 0
        path = self.directory / f"{self.session}-{self.segment:05d}.jsonl"
        new = not path.exists()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            remaining = memoryview(line)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("short capture write")
                self.size += written
                remaining = remaining[written:]
            os.fsync(fd)
        except Exception:
            # A partially written record must not corrupt subsequent records.
            self.segment += 1
            self.size = 0
            raise
        finally:
            os.close(fd)
        if new:
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    def _error(self, err: Exception) -> None:
        if self.failed:
            return
        self.failed = True
        _LOGGER.error("Vega raw capture INCOMPLETE, session %s: %s", self.session, err)
        try:
            from homeassistant.components import persistent_notification
            persistent_notification.async_create(
                self.hass,
                "Не удалось сохранить часть сырого трафика Vega. "
                "Захват может быть неполным. Проверьте свободное место и журнал HA. "
                f"Сессия: {self.session}",
                title="Vega: ошибка сохранения трафика",
                notification_id="vega_raw_capture_error",
            )
        except Exception:
            _LOGGER.exception("Vega: cannot create capture error notification")
