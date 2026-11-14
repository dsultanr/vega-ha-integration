"""Offline plan for passive F515 climate telemetry; no CAN commands or I/O.

Setting 5500 location byte: bit 0 Motorola, bits 2..4 byte index,
bits 5..7 bit index. Both signals fit inside one byte.
"""
from __future__ import annotations

import binascii
import struct

from .hv_profile import _crc, _same_sensor, empty_slot

SENSOR_IDS = (2924, 2925)
PERIOD = bytes.fromhex("00050000")
READS = ([(5500, slot, 84) for slot in range(128)]
         + [(5503, group, 233) for group in range(6)]
         + [(3004, sid, 4) for sid in SENSOR_IDS])


def sensor_record(sid: int, bus: int) -> bytes:
    if sid not in SENSOR_IDS or bus not in (1, 2, 3):
        raise ValueError("Unsupported climate sensor or CAN bus")
    seat = sid == 2924
    raw = bytearray(84)
    raw[0], raw[1], raw[8] = 8, bus, 1  # tested record format, UINT8
    struct.pack_into("<H", raw, 9, sid)
    name = "Rear right seat heat" if seat else "Climate air distribution"
    raw[11:42] = name.encode("ascii").ljust(31, b"\0")
    struct.pack_into("<I", raw, 42, 0x3D7 if seat else 0x378)
    struct.pack_into("<Q", raw, 46, 255)  # unavailable after 15s without frame
    raw[54:57] = bytes((15, 3, 0x0D if seat else 0x91))
    struct.pack_into("<QQff", raw, 57, 0, 7, 1, 0)
    return _crc(raw)


def prepare(snapshot: dict[str, bytes]) -> dict[str, bytes]:
    """Return ordered setting writes, rejecting occupied IDs and unknown formats."""
    for setting, sub, size in READS:
        key = f"{setting}/{sub}"
        if key not in snapshot or len(snapshot[key]) != size:
            raise ValueError(f"Incomplete snapshot: {key}")
    slots = [snapshot[f"5500/{i}"] for i in range(128)]
    for i, raw in enumerate(slots):
        if (raw[0] not in (8, 9) or raw[1] not in (0, 1, 2, 3)
                or raw[2:6] != bytes(4)
                or raw[6:8] != struct.pack("<H", binascii.crc_hqx(raw[42:], 65535))):
            raise ValueError(f"Unknown CAN format or invalid CRC: 5500/{i}")
    for group in range(6):
        raw = snapshot[f"5503/{group}"]
        if raw[:6] != bytes.fromhex("020900000000"):
            raise ValueError(f"Unknown ISO-TP format: 5503/{group}")
        for start in (8, 83, 158):
            if int.from_bytes(raw[start+10:start+12], "little") in SENSOR_IDS:
                raise ValueError("Climate sensor ID occupied in ISO-TP")
    # Existing wheel heater uses the same 3-bit unsigned extraction. Validate
    # the full decoder body and infer the bus from it, not from its slot number.
    anchors = [r for r in slots if int.from_bytes(r[9:11], "little") == 2918]
    expected = bytearray(sensor_record(2924, 1))
    struct.pack_into("<I", expected, 42, 0x3A9)
    expected[46:57] = bytes(9) + bytes((3, 1))
    if (len(anchors) != 1 or anchors[0][0] not in (8, 9) or anchors[0][8] != 1
            or anchors[0][1] not in (1, 2, 3) or anchors[0][42:] != expected[42:]):
        raise ValueError("Existing heater decoder does not match the F515 profile")
    bus = anchors[0][1]
    free = [i for i, raw in enumerate(slots) if raw == empty_slot()]
    changes = {}
    for sid in SENSOR_IDS:
        desired = sensor_record(sid, bus)
        matches = [i for i, r in enumerate(slots) if int.from_bytes(r[9:11], "little") == sid]
        if matches:
            if len(matches) != 1 or not _same_sensor(slots[matches[0]], desired):
                raise ValueError(f"Climate sensor ID {sid} occupied in CAN")
        else:
            if not free:
                raise ValueError("Not enough empty CAN slots")
            changes[f"5500/{free.pop(0)}"] = desired
    for sid in SENSOR_IDS:
        key = f"3004/{sid}"
        if snapshot[key] != PERIOD:
            changes[key] = PERIOD
    return changes
