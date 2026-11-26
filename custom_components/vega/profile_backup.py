"""Full tracker profile as a Vega Configurator settings file (*.vsf). Read-only for the tracker.

File layout (Vega Configurator 1.28.46):
  0..7    TEA CBC-MAC of bytes 8..end; the configurator rejects a file whose MAC does not match
  8..23   b"SETTINGS\\" + 7 zero bytes
  24..    records: id u16 LE, sub u16 LE, size u16 LE, data[size]

The list of settings is the one the configurator itself reads ("считать всё", 120 ids
3001..5582 known to its settings table) plus what a file saved by Configurator 1.28.46 for
Windows holds beyond that table: the per-sensor transmit records 3004 (sub = sensor number),
3101/7, 5073/4, 5000 and 5504/21. What of it exists is decided by the tracker: a missing
record is refused at once and is simply not written to the file.
"""
import asyncio
import struct

from . import protocol

_KEY = struct.unpack("<4I", bytes.fromhex("8a5221607006f547fbaa998dfa9eadc6"))
_HEADER = b"SETTINGS\\" + bytes(7)
_M = 0xFFFFFFFF

# USB password and server PINs (3255), device identifier (5504). The configurator saves
# them; here only on explicit request. The BLE PIN (5070/4) is part of every copy.
SECRETS = frozenset({3255, 5504})
PROFILE_NAME = 5499

# (id, sub count, dynamic). Dynamic ids get their sub count from the tracker: subs are read
# until the first missing one, the number is only an upper bound.
CATALOG = (
    (3001, 4, False), (3002, 255, True), (3003, 255, True), (3005, 4, False), (3006, 4, False),
    (3007, 4, False), (3008, 3, False), (3009, 1, False), (3010, 2, False), (3100, 3, False),
    (3101, 8, False), (3102, 1, False), (3103, 1, False), (3104, 1, False), (3105, 1, False),
    (3106, 1, False), (3107, 1, False), (3108, 6, False), (3109, 6, False), (3110, 2, False),
    (3201, 5, False), (3202, 1, False), (3203, 7, False), (3251, 1, False), (3252, 1, False),
    (3253, 255, True), (3254, 255, True), (3255, 5, False), (3301, 255, True), (3351, 255, True),
    (3352, 255, True), (3353, 255, True), (3354, 255, True), (3356, 255, True), (3357, 1, False),
    (3359, 8, False), (3360, 1, False), (3361, 1, False), (3362, 2, False), (3363, 2, False),
    (3364, 2, False), (3365, 1, False), (3366, 1, False), (3367, 255, True), (3368, 2, False),
    (3369, 1, False), (3370, 1, False), (3371, 255, True), (3372, 1, False), (3383, 1, False),
    (4001, 255, True),
    *((sid, 1, False) for sid in range(5001, 5032)),
    (5034, 1, False), (5035, 300, True), (5036, 1, False), (5037, 1, False), (5038, 1, False),
    (5039, 10, False), (5040, 1, False), (5041, 1, False), (5044, 4, False), (5045, 1, False),
    (5070, 5, False), (5071, 1, False), (5073, 5, False), (5074, 16, False), (5078, 16, False),
    (5499, 1, False), (5500, 255, True), (5501, 255, True), (5502, 255, True), (5503, 255, True),
    (5505, 1, False), (5506, 1, False), (5508, 1, False), (5525, 1, False), (5526, 4, False),
    (5540, 1, False), (5541, 255, True), (5547, 1, False), (5550, 8, False), (5551, 3, False),
    (5552, 1, False), (5553, 1, False), (5564, 3, False), (5568, 4, False), (5572, 3, False),
    (5573, 30, False), (5581, 4, False), (5582, 1, False),
)

# Sensors that have a transmit record 3004/<sensor>: the union of the files saved by the
# configurator for ISpace_4wd (548 sensors) and ISpace_2WD_17072026 (606). The set depends on
# the tracker firmware; a sensor the tracker does not have is refused and skipped.
_SENSOR_RANGES = (
    (1, 24), (28, 28), (30, 31), (35, 35), (101, 166), (218, 229), (1001, 1003), (1041, 1043),
    (1061, 1064), (1081, 1083), (1101, 1103), (1121, 1121), (1141, 1148), (1151, 1158),
    (1181, 1190), (1201, 1202), (1241, 1241), (1243, 1246), (1249, 1253), (1256, 1275),
    (1281, 1295), (1301, 1315), (1321, 1335), (1341, 1355), (1520, 1581), (1600, 1646),
    (1648, 1648), (1650, 1661), (1669, 1669), (2001, 2005), (2007, 2016), (2019, 2023),
    (2025, 2025), (2800, 2927), (2975, 2978), (2984, 2991), (2993, 2993), (8000, 8129),
    (31700, 31724), (32220, 32249), (33000, 33005), (33007, 33008), (33219, 33220),
    (33223, 33223), (33298, 33299), (33305, 33305), (33330, 33330), (33333, 33334),
    (33952, 33952),
)
TRANSMIT_SENSORS = tuple(sid for first, last in _SENSOR_RANGES for sid in range(first, last + 1))


def plan(include_secrets: bool = False) -> list[tuple[int, tuple[int, ...], bool]]:
    """What to read, in file order: (id, subs, stop at the first missing sub)."""
    rows = [(setting, tuple(range(subs)), dynamic) for setting, subs, dynamic in CATALOG]
    rows += [(3004, TRANSMIT_SENSORS, False), (5000, (0,), False), (5504, (21,), False)]
    return sorted((row for row in rows if include_secrets or row[0] not in SECRETS), key=lambda row: row[0])


class ProfileReadError(Exception):
    """The profile could not be read completely; nothing may be saved."""


def mac(body: bytes) -> bytes:
    body = body[: len(body) & 0xFFFF]  # the configurator passes the length as u16
    body += bytes(-len(body) % 8)
    v0 = v1 = 0
    for at in range(0, len(body), 8):
        a, b = struct.unpack_from(">II", body, at)
        v0 ^= a
        v1 ^= b
        total = 0
        for _ in range(32):
            total = (total + 0x9E3779B9) & _M
            v0 = (v0 + ((((v1 << 4) & _M) + _KEY[0]) ^ ((v1 + total) & _M) ^ ((v1 >> 5) + _KEY[1]))) & _M
            v1 = (v1 + ((((v0 << 4) & _M) + _KEY[2]) ^ ((v0 + total) & _M) ^ ((v0 >> 5) + _KEY[3]))) & _M
    return struct.pack("<II", v0, v1)


def build(records) -> bytes:
    body = bytearray(_HEADER)
    for setting, sub, data in records:
        body += struct.pack("<HHH", setting, sub, len(data)) + data
    return mac(bytes(body)) + bytes(body)


def parse(data: bytes) -> list[tuple[int, int, bytes]]:
    """Records of a file; raises ValueError unless the MAC and the layout are exact."""
    if len(data) < 24 or data[8:24] != _HEADER:
        raise ValueError("not a SETTINGS vsf")
    if mac(data[8:]) != data[:8]:
        raise ValueError("bad MAC")
    records, at = [], 24
    while at < len(data):
        if at + 6 > len(data):
            raise ValueError("truncated record header")
        setting, sub, size = struct.unpack_from("<HHH", data, at)
        if at + 6 + size > len(data):
            raise ValueError("truncated record")
        records.append((setting, sub, data[at + 6: at + 6 + size]))
        at += 6 + size
    return records


async def _ask(session, setting: int, sub: int, timeout: float) -> dict | None:
    """Send one read and return the parsed COMMAND_ANS, or None when nothing came back."""
    if hasattr(session, "call"):
        # Cloud relay: the hub sends the frame itself and returns the tracker's reply as is.
        # It serves one request at a time and refuses a second one instead of queueing it.
        for _busy in range(20):
            try:
                response = await session.call("read", setting, sub, timeout=timeout)
            except asyncio.TimeoutError:
                return None
            except RuntimeError as err:
                if "busy" in str(err):
                    await asyncio.sleep(0.25)
                    continue
                if "expired" in str(err):
                    return None
                raise ProfileReadError(f"связь с трекером потеряна ({err})") from err
            payload = response.get("payload")
            # The hub reports its own timeout as an empty error and no payload.
            return protocol.parse_command_ans(bytes.fromhex(payload)) if payload else None
        raise ProfileReadError("канал трекера занят другими запросами")
    req_id = session.get_next_req_id()
    frame = protocol.build_query_sensor_frame(req_id, setting, sub)
    try:
        # Sessions take a per-request timeout; wait_for also bounds a stalled socket write.
        return await asyncio.wait_for(session._request(frame, req_id, timeout=timeout), timeout + 1)
    except asyncio.TimeoutError:
        return None


async def _probe(session, setting: int, sub: int, timeout: float) -> bytes | None:
    """One record: its bytes, or None when the tracker says it has no such record.

    A reply is matched by request id. Status 1 carries the value; any other status is the
    tracker's refusal. Silence is neither: one more attempt (a read changes nothing), then
    the whole backup fails rather than pass an unknown record off as absent.
    """
    for _attempt in range(2):
        answer = await _ask(session, setting, sub, timeout)
        if answer is None:
            continue
        if answer.get("subcmd") != 1:
            return None
        data = answer.get("data") or b""
        if len(data) < 3 or data[0] | (data[1] << 8) != setting or data[2] != len(data) - 3:
            raise ProfileReadError(f"Некорректный ответ на чтение {setting}/{sub}")
        return bytes(data[3:])
    raise ProfileReadError(f"Трекер не ответил на чтение {setting}/{sub}")


# How many subs a dynamic id usually has: only to make the progress figure move evenly.
_USUAL_SUBS = {4001: 25, 5500: 128}


def _weight(setting: int, subs, dynamic: bool) -> int:
    return _USUAL_SUBS.get(setting, 8) if dynamic else len(subs)


async def read_profile(session, *, include_secrets: bool = False, timeout: float = 6.0, progress=None):
    """Read every configurator setting the tracker has. Returns (records, missing count).

    progress(percent, records) is called after every request; the percentage is approximate,
    because how many subs a dynamic id has is known only once they are read.
    """
    rows = plan(include_secrets)
    total = sum(_weight(*row) for row in rows)
    records, missing, before = [], 0, 0
    for setting, subs, dynamic in rows:
        weight = _weight(setting, subs, dynamic)
        for position, sub in enumerate(subs):
            value = await _probe(session, setting, sub, timeout)
            if progress is not None:
                progress(min(99, (before + min(position + 1, weight)) * 100 // total), len(records))
            if value is None:
                missing += 1
                if dynamic:
                    break
                continue
            records.append((setting, sub, value))
        before += weight
    if not records:
        raise ProfileReadError("Трекер не отдал ни одной настройки")
    return records, missing


def profile_name(records) -> str:
    for setting, sub, data in records:
        if setting == PROFILE_NAME and sub == 0:
            return data.split(b"\0", 1)[0].decode("latin1", errors="replace")
    return ""
