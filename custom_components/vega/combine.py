"""Wialon Combine v1.1.4 decoder + S1 server framing for the Vega tracker.

Ported from the platform's reference codec and golden decoder (validated
against internal/providers/vega/combine/*.go and tests/fixtures/vega/combine):

  * CRC-16/ARC (reflected 0x8005, init 0) over Head..last Data byte;
  * frame  [Head 0x2424][Type* ext1][Seq 2 BE][Len** ext2][Data][CRC16 2];
    keep-alive is only Head+Type+Seq (no Len/Data/CRC);
  * data packet = messages of records (position / IO / params / LBS / WiFi /
    blocks); a param carries a Vega sensor id and a value already scaled by its
    exponent;
  * server acceptance response EncodeAck(seq) = 40 40 00 seqHi seqLo — the ONLY
    thing we ever send back (never commands).

Stdlib only; no Home Assistant imports, so it is unit-testable on its own.
"""
from __future__ import annotations

import datetime as _dt
import struct

HEAD = b"\x24\x24"  # "$$"

TYPE_LOGIN = 0
TYPE_DATA = 1
TYPE_KEEPALIVE = 2
TYPE_ACK = 3
_TYPE_NAME = {0: "login", 1: "data", 2: "keepalive", 3: "ack"}

MAX_DATA_LEN = 64 << 10  # reject larger frames as corruption, do not buffer

ACK_ACCEPTED = 0x00


class ShortFrame(Exception):
    """The buffer holds only a (valid) prefix of a frame; read more bytes."""


# --- CRC-16/ARC -----------------------------------------------------------

def _crc_table() -> list[int]:
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        t.append(c)
    return t


_CRC_TAB = _crc_table()


def crc16(data: bytes) -> int:
    """CRC-16/ARC. crc16(b'123456789') == 0xBB3D."""
    crc = 0
    for b in data:
        crc = (crc >> 8) ^ _CRC_TAB[(crc ^ b) & 0xFF]
    return crc


def encode_ack(seq: int) -> bytes:
    """Server acceptance response for a frame with sequence number seq."""
    return bytes([0x40, 0x40, ACK_ACCEPTED]) + struct.pack(">H", seq & 0xFFFF)


# --- extensible fields ----------------------------------------------------

def _ext1(b: bytes, o: int) -> tuple[int, int]:
    """One-byte extensible field (high bit -> one more byte, 15-bit value)."""
    if o >= len(b):
        raise ShortFrame
    v = b[o]
    if v & 0x80:
        if o + 1 >= len(b):
            raise ShortFrame
        return ((v & 0x7F) << 8) | b[o + 1], o + 2
    return v, o + 1


def _ext2(b: bytes, o: int) -> tuple[int, int]:
    """Two-byte extensible field (high bit -> two more bytes, 31-bit value)."""
    if o + 2 > len(b):
        raise ShortFrame
    if b[o] & 0x80:
        if o + 4 > len(b):
            raise ShortFrame
        return (struct.unpack(">I", b[o:o + 4])[0] & 0x7FFFFFFF), o + 4
    return struct.unpack(">H", b[o:o + 2])[0], o + 2


# --- Data field decode (records) ------------------------------------------

_SIZE = {0: 1, 1: 2, 2: 4, 3: 8, 4: 1, 5: 2, 6: 4, 7: 8, 8: 4, 9: 8}
_FMT = {0: ">B", 1: ">H", 2: ">I", 3: ">Q", 4: ">b", 5: ">h", 6: ">i", 7: ">q", 8: ">f", 9: ">d"}


def _cstr(b: bytes, o: int) -> tuple[str, int]:
    e = b.index(0, o)
    return b[o:e].decode("latin1"), e + 1


def _param(b: bytes, o: int) -> tuple[dict, int]:
    num, o = _ext1(b, o)
    st = b[o]
    o += 1
    base, xp = st & 0x1F, (st >> 5) & 0x07
    p: dict = {"id": num, "sensor_type": base, "exp": xp}
    if base == 10:  # string
        p["text"], o = _cstr(b, o)
        return p, o
    if base not in _FMT:
        raise ValueError(f"combine: bad sensor type {base}")
    v = struct.unpack(_FMT[base], b[o:o + _SIZE[base]])[0]
    o += _SIZE[base]
    if base < 8 and xp:
        v = v / (10 ** xp)
    p["value"] = v
    return p, o


def _params(b: bytes, o: int, extensible: bool) -> tuple[list, int]:
    if extensible:
        cnt, o = _ext1(b, o)
    else:
        cnt, o = b[o], o + 1
    out = []
    for _ in range(cnt):
        p, o = _param(b, o)
        out.append(p)
    return out, o


def _record(b: bytes, o: int) -> tuple[dict, int]:
    rt, o = _ext1(b, o)
    r: dict = {"type": rt}
    if rt == 1:
        la, lo, sp, cr, al, sa, hd = struct.unpack(">iiHHhBH", b[o:o + 17])
        o += 17
        r["position"] = {"lat": la / 1e6, "lon": lo / 1e6, "speed": sp, "course": cr,
                         "alt": al, "sats": sa, "hdop": hd / 100}
    elif rt == 14:
        la, lo, sp, cr, al, sa, hd = struct.unpack(">qqHHqBH", b[o:o + 31])
        o += 31
        r["position"] = {"lat": la / 1e16, "lon": lo / 1e16, "speed": sp, "course": cr,
                         "alt": al / 1e14, "sats": sa, "hdop": hd / 100, "extended": True}
    elif rt == 2:
        i, out = struct.unpack(">II", b[o:o + 8])
        o += 8
        r["io"] = {"inputs": i, "outputs": out}
    elif rt == 0:
        r["params"], o = _params(b, o, True)
    elif rt in (5, 6, 7, 8, 9, 10):
        r["params"], o = _params(b, o, False)
    elif rt == 4:
        n = b[o]; o += 1
        cells = []
        for _ in range(n):
            mcc, mnc, lac, cid, rx, ta = struct.unpack(">HHHHHH", b[o:o + 12])
            o += 12
            cells.append({"mcc": mcc, "mnc": mnc, "lac": lac, "cell_id": cid, "rx_level": rx, "ta": ta})
        r["lbs"] = cells
    elif rt == 13:
        n = b[o]; o += 1
        aps = []
        for _ in range(n):
            mac = b[o:o + 6].hex(":")
            rssi = struct.unpack(">b", b[o + 6:o + 7])[0]
            o += 7
            aps.append({"mac": mac, "rssi": rssi})
        r["wifi"] = aps
    elif rt in (3, 11):
        ind, o = _ext1(b, o)
        ln, o = _ext2(b, o)
        last, o = _ext1(b, o)
        blk = {"index": ind, "len": ln, "last": last}
        if rt == 3:
            blk["name"], o = _cstr(b, o)
        o += ln
        r["block"] = blk
    elif rt == 12:
        _, o = _cstr(b, o)
        r["driver_message"] = True
    else:
        raise ValueError(f"combine: unknown record type {rt}")
    return r, o


def _decode_data(data: bytes) -> list:
    """Decode a data packet's Data field into a list of messages."""
    msgs = []
    p = 0
    ln = len(data)
    while p < ln:
        t = struct.unpack(">I", data[p:p + 4])[0]
        cnt = data[p + 4]
        p += 5
        recs = []
        for _ in range(cnt):
            r, p = _record(data, p)
            recs.append(r)
        msgs.append({
            "time": _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "records": recs,
        })
    if p != ln:
        raise ValueError("combine: trailing bytes in data packet")
    return msgs


def parse_frame(b: bytes) -> tuple[dict, int]:
    """Parse the frame at the start of b. Returns (packet, consumed).

    Raises ShortFrame if b is a valid prefix (read more). Raises ValueError
    for a bad head / type / length / CRC / malformed payload (resync).
    """
    if len(b) < 2:
        if len(b) == 1 and b[0] != HEAD[0]:
            raise ValueError("combine: bad head")
        raise ShortFrame
    if b[0] != HEAD[0] or b[1] != HEAD[1]:
        raise ValueError("combine: bad head")
    typ, o = _ext1(b, 2)
    if typ > TYPE_ACK:
        raise ValueError(f"combine: unknown packet type {typ}")
    if len(b) < o + 2:
        raise ShortFrame
    seq = struct.unpack(">H", b[o:o + 2])[0]
    o += 2
    name = _TYPE_NAME[typ]
    if typ == TYPE_KEEPALIVE:
        return {"type": name, "seq": seq}, o
    ln, o = _ext2(b, o)
    if ln > MAX_DATA_LEN:
        raise ValueError(f"combine: length out of range {ln}")
    if len(b) < o + ln + 2:
        raise ShortFrame
    data = b[o:o + ln]
    crc = struct.unpack(">H", b[o + ln:o + ln + 2])[0]
    if crc16(b[:o + ln]) != crc:
        raise ValueError("combine: crc mismatch")
    pkt = {"type": name, "seq": seq, "crc": "%04x" % crc}
    try:
        if typ == TYPE_LOGIN:
            pkt["login"] = _decode_login(data)
        elif typ == TYPE_ACK:
            pkt["ack"] = {"command_type": data[0], "code": data[1]}
        else:  # data
            pkt["messages"] = _decode_data(data)
    except ShortFrame:
        # A truncated record inside a CRC-valid frame is corruption, not "short".
        raise ValueError("combine: malformed payload")
    except (IndexError, struct.error) as err:
        raise ValueError(f"combine: malformed payload ({err})")
    return pkt, o + ln + 2


def _decode_login(data: bytes) -> dict:
    ver, p = _ext1(data, 0)
    flags = data[p]
    p += 1
    idt, pwt = flags >> 4, flags & 0x0F

    def _val(kind: int, p: int) -> tuple[str, int]:
        if kind == 4:
            return _cstr(data, p)
        n = {1: 2, 2: 4, 3: 8}[kind]
        return str(int.from_bytes(data[p:p + n], "big")), p + n

    ident, p = _val(idt, p)
    if pwt:
        _, p = _val(pwt, p)  # password: decoded to advance, never kept
    return {"version": ver, "flags": flags, "id": ident}


class Stream:
    """Reassembles Combine frames from arbitrarily split TCP input.

    Not safe for concurrent use. `frames()` yields decoded packets; on a bad
    frame it resynchronises to the next 0x2424 and continues (dropping bytes),
    so one corrupt frame never stalls the stream.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.dropped = 0

    def write(self, data: bytes) -> None:
        self._buf.extend(data)

    def buffered(self) -> int:
        return len(self._buf)

    def frames(self):
        """Yield every complete packet currently buffered."""
        while self._buf:
            try:
                pkt, n = parse_frame(bytes(self._buf))
            except ShortFrame:
                return
            except ValueError:
                # Resync: skip to the next 0x2424 after the current start,
                # or keep a trailing 0x24 that may begin one.
                nxt = self._buf.find(HEAD, 1)
                if nxt >= 0:
                    skip = nxt
                elif self._buf[-1] == HEAD[0]:
                    skip = len(self._buf) - 1
                else:
                    skip = len(self._buf)
                self.dropped += skip
                del self._buf[:skip]
                continue
            del self._buf[:n]
            yield pkt
