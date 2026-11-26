"""Best-effort copy of a tracker's Wialon Combine stream to a second server.

Home Assistant stays the server the tracker talks to: it decodes the stream and
sends the accept-ACKs itself. This module only forwards the same frames, byte
for byte, to a server the owner named, and throws away whatever that server
answers. Home Assistant telemetry therefore never depends on the second server;
the price is that frames sent while it was unreachable beyond the buffer are
lost to it, because the tracker already considers them delivered.

No Home Assistant imports: usable and testable on its own.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Callable

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT_SEC = 8.0
# After a failed connect or a dropped connection, wait this long before the
# next attempt. Shared by every tracker connection, so a tracker that
# reconnects often does not turn into a connect storm against a dead server.
BACKOFF_SEC = 30.0
# Frames waiting for the second server are kept up to this many bytes; beyond
# it the oldest are dropped.
MAX_BUFFER_BYTES = 256 * 1024

Target = tuple[str, int]


def parse_target(text: str | None) -> Target | None:
    """Parse the owner's "host:port". Empty means the copy is off.

    Raises ValueError for anything else that is not host:port with a port in
    1..65535. IPv6 literals go in brackets: [::1]:20332.
    """
    text = (text or "").strip()
    if not text:
        return None
    host, sep, port = text.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not sep or not host or not port.isdigit() or any(c.isspace() for c in host):
        raise ValueError("expected host:port")
    number = int(port)
    if not 1 <= number <= 65535:
        raise ValueError("port out of range")
    return host, number


class MirrorState:
    """Reachability of the second server, shared across tracker connections."""

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self.fail_at = 0.0
        self.down = False  # True once an outage has been logged
        self.connected = False
        # Outcome of the latest delivery attempt: None until there was one.
        # Stays True between connections, so it does not flap every time the
        # tracker reconnects or the server closes an idle connection.
        self.alive: bool | None = None
        self._on_change = on_change

    def reset(self) -> None:
        """Forget everything: the copy was turned off or pointed elsewhere."""
        self.fail_at = 0.0
        self.down = False
        self.connected = False
        self._set_alive(None)

    def _set_alive(self, alive: bool | None) -> None:
        if alive is self.alive:
            return
        self.alive = alive
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001
                pass

    def backoff_left(self) -> float:
        return max(0.0, BACKOFF_SEC - (time.monotonic() - self.fail_at)) if self.fail_at else 0.0

    def failed(self, target: Target, reason: str) -> None:
        self.fail_at = time.monotonic()
        self.connected = False
        self._set_alive(False)
        if not self.down:
            self.down = True
            _LOGGER.warning(
                "Vega Combine copy: %s:%s is unavailable (%s); telemetry in Home Assistant "
                "is not affected, frames beyond the buffer are not delivered there",
                target[0], target[1], reason)

    def succeeded(self, target: Target) -> None:
        self.fail_at = 0.0
        self.connected = True
        self._set_alive(True)
        if self.down:
            self.down = False
            _LOGGER.info("Vega Combine copy: %s:%s is reachable again", *target)
        else:
            _LOGGER.info("Vega Combine copy: connected to %s:%s", *target)


class CombineMirror:
    """Copies one tracker connection's Combine frames to the second server.

    `target` is called before every connect, so turning the copy off or
    pointing it elsewhere takes effect without dropping the tracker.
    A Combine session must open with the login frame, so the tracker's login is
    remembered and replayed first on every connection to the second server.
    """

    def __init__(self, target: Callable[[], Target | None], state: MirrorState,
                 *, open_connection=None) -> None:
        self._target = target
        self._state = state
        self._open = open_connection
        self._login: bytes | None = None
        self._queue: collections.deque[bytes] = collections.deque()
        self._queued = 0
        self.dropped = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._writer = None
        self._peer_closed = False
        self._discard_task: asyncio.Task | None = None
        self._closed = False

    def feed(self, kind: str, raw: bytes) -> None:
        """Take one frame the tracker sent. Never blocks, never raises."""
        if self._closed:
            return
        if kind == "login":
            self._login = raw
        elif kind in ("data", "keepalive"):
            if self._target() is None:
                return
            if kind == "keepalive" and self._writer is None:
                return  # nothing to keep alive; do not let these fill the buffer
            self._queue.append(raw)
            self._queued += len(raw)
            while self._queued > MAX_BUFFER_BYTES and len(self._queue) > 1:
                self._queued -= len(self._queue.popleft())
                self.dropped += 1
        else:
            return  # the tracker's own ack frames answer nobody on that side
        if self._task is None and self._target() is not None:
            self._task = asyncio.get_running_loop().create_task(self._run())
        self._wake.set()

    async def close(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        # A cancellation that lands while a connect attempt is finishing can be
        # swallowed by asyncio.wait_for on older Pythons; _run also checks
        # _closed, and the cancel is repeated until the task is really gone.
        while task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task], timeout=0.2)
        self._drop_connection()

    def _drop_connection(self) -> None:
        writer, self._writer = self._writer, None
        discard, self._discard_task = self._discard_task, None
        if discard is not None:
            discard.cancel()
        if writer is not None:
            self._state.connected = False
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _run(self) -> None:
        while not self._closed:
            target = self._target()
            if self._peer_closed and self._writer is not None:
                # The server hung up (idle timeout or restart): reconnect on the
                # next frame. Not an outage until a connect actually fails.
                self._drop_connection()
            if target is None:
                self._queue.clear()
                self._queued = 0
                self._drop_connection()
            elif self._login is not None and self._queue:
                try:
                    await self._deliver(target)
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    self._drop_connection()
                    if self._closed:
                        return
                    self._state.failed(target, type(err).__name__ if not str(err) else str(err))
                    await asyncio.sleep(BACKOFF_SEC)
                    continue
            self._wake.clear()
            if self._queue and self._target() is not None and self._login is not None:
                continue
            await self._wake.wait()

    async def _deliver(self, target: Target) -> None:
        if self._writer is None:
            wait = self._state.backoff_left()
            if wait:
                await asyncio.sleep(wait)
            opener = self._open or asyncio.open_connection
            reader, writer = await asyncio.wait_for(opener(*target), CONNECT_TIMEOUT_SEC)
            self._writer = writer
            self._peer_closed = False
            self._discard_task = asyncio.get_running_loop().create_task(self._discard(reader, writer))
            writer.write(self._login)
            await writer.drain()
            self._state.succeeded(target)
        while self._queue:
            if self._peer_closed:
                return  # _run drops the connection and comes back for the rest
            raw = self._queue[0]
            self._writer.write(raw)
            await self._writer.drain()
            # Removed only once written, so a frame cut off by a drop is resent.
            if self._queue and self._queue[0] is raw:
                self._queue.popleft()
                self._queued -= len(raw)

    async def _discard(self, reader, writer) -> None:
        """Read and drop the second server's answers; notice when it hangs up."""
        try:
            while await reader.read(4096):
                pass
        except Exception:  # noqa: BLE001
            pass
        if self._writer is writer:
            self._peer_closed = True
            self._wake.set()
