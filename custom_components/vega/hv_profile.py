"""Plan passive F515 HV telemetry settings from a complete tracker snapshot.

No I/O here. Only known empty slots may be allocated. Existing IDs must match
the tested profile exactly (names may differ); never replace another sensor.
"""
from __future__ import annotations

import binascii
from dataclasses import dataclass
import struct

PROFILE = "f515_ispace_4wd"
PROFILE_NAME = "F515 / i-Space 4WD"
# The HV decoders read CAN 0x104, the frame the SoC decoder already uses. A tracker with the
# ISpace_11092025 CAN profile carries a byte-identical SoC decoder, so the same plan builds for
# it, but voltage and current have not been checked against that car.
PROFILE_2025 = "ispace_2025"
PROFILES = {PROFILE: PROFILE_NAME, PROFILE_2025: "i-Space 2025 (не проверено на автомобиле)"}
SENSOR_IDS = (2922, 2923)
PERIOD = bytes.fromhex("00050000")
READS = ([(5500, slot, 84) for slot in range(128)]
         + [(5503, group, 233) for group in range(6)]
         + [(3004, sid, 4) for sid in SENSOR_IDS])


def _crc(raw: bytearray) -> bytes:
    struct.pack_into("<H", raw, 6, binascii.crc_hqx(raw[42:], 65535))
    return bytes(raw)


def empty_slot() -> bytes:
    raw = bytearray(84)
    raw[0] = raw[8] = 8
    raw[55] = 64
    struct.pack_into("<QQff", raw, 57, 0, 0xffffffffffffffff, 1, 0)
    return _crc(raw)


def sensor_record(sid: int, bus: int) -> bytes:
    if sid not in SENSOR_IDS or bus not in (1, 2, 3):
        raise ValueError("Неподдерживаемый датчик или CAN-шина")
    voltage = sid == 2922
    raw = bytearray(84)
    raw[0], raw[1], raw[8] = 8, bus, 4  # tested record format, bus, FLOAT32
    struct.pack_into("<H", raw, 9, sid)
    name = "HV battery voltage (V)" if voltage else "HV battery current (A)"
    raw[11:42] = name.encode("ascii").ljust(31, b"\0")
    struct.pack_into("<I", raw, 42, 0x104)
    raw[54:57] = bytes((15, 14, 0x45 if voltage else 0xd5))
    struct.pack_into("<QQff", raw, 57, 0, 0x3fff, 0.1, 0 if voltage else -400)
    return _crc(raw)


def _same_sensor(left: bytes, right: bytes) -> bool:
    return left[:11] == right[:11] and left[42:] == right[42:]


@dataclass
class Plan:
    originals: dict[str, bytes]
    changes: dict[str, bytes]
    bus: int
    sensors: list[dict]
    profile: str = PROFILE

    def summary(self) -> dict:
        return {"profile": self.profile, "profile_name": PROFILES[self.profile],
                "configured": not self.changes, "bus": self.bus,
                "period_seconds": 5, "sensors": self.sensors,
                "change_count": len(self.changes)}


def soc_bus(slots: list[bytes]) -> int:
    """Bus of the single known SoC decoder; identifies the tested CAN matrix."""
    socs = [r for r in slots if int.from_bytes(r[9:11], "little") == 2803]
    if len(socs) != 1:
        raise ValueError("Не найден единственный датчик SoC для этого профиля")
    soc = socs[0]
    if not (soc[0] == 8 and soc[1] in (1, 2, 3) and soc[2:6] == bytes(4)
            and soc[8] == 1 and soc[42:46] == struct.pack("<I", 0x104)
            and soc[55:57] == bytes((10, 0x99))
            and soc[57:81] == struct.pack("<QQff", 0, 1023, 0.1, 0)
            and soc[81:] == bytes(3)):
        raise ValueError("Датчик SoC не соответствует проверенной раскладке CAN 0x104")
    return soc[1]


def prepare(snapshot: dict[str, bytes], profile: str) -> Plan:
    if profile not in PROFILES:
        raise ValueError("Выберите поддерживаемый профиль автомобиля")
    for setting, sub, size in READS:
        key = f"{setting}/{sub}"
        if key not in snapshot or len(snapshot[key]) != size:
            raise ValueError(f"{key}: неполный снимок настроек; перечитайте трекер")
    slots = [snapshot[f"5500/{i}"] for i in range(128)]
    for i, raw in enumerate(slots):
        if raw[0] not in (8, 9) or raw[1] not in (0, 1, 2, 3) or raw[2:6] != bytes(4):
            raise ValueError(f"5500/{i}: неизвестный формат CAN; запись отменена")
        if raw[6:8] != struct.pack("<H", binascii.crc_hqx(raw[42:], 65535)):
            raise ValueError(f"5500/{i}: контрольная сумма не совпадает")
    for group in range(6):
        raw = snapshot[f"5503/{group}"]
        if not any(raw):
            continue  # An unused group is all zeros; it holds no sensors.
        if raw[:6] != bytes.fromhex("020900000000"):
            raise ValueError(f"5503/{group}: неизвестный формат ISO-TP; запись отменена")
        for start in (8, 83, 158):
            sid = int.from_bytes(raw[start + 10:start + 12], "little")
            if sid in SENSOR_IDS:
                raise ValueError(f"Датчик {sid} уже занят в ISO-TP (5503/{group})")

    bus = soc_bus(slots)
    free = [i for i, raw in enumerate(slots) if raw == empty_slot()]
    changes, sensors = {}, []
    for sid, name in ((2922, "Напряжение ВВБ"), (2923, "Ток ВВБ")):
        matches = [i for i, raw in enumerate(slots) if int.from_bytes(raw[9:11], "little") == sid]
        desired = sensor_record(sid, bus)
        if matches:
            if len(matches) != 1 or not _same_sensor(slots[matches[0]], desired):
                raise ValueError(f"Датчик {sid} уже занят другой настройкой CAN")
            slot = matches[0]
        else:
            if not free:
                raise ValueError("Нет двух свободных слотов CAN для измерения мощности ВВБ")
            slot = free.pop(0)
            changes[f"5500/{slot}"] = desired
        sensors.append({"name": name, "sensor_id": sid, "slot": slot, "existing": bool(matches)})
    # Install both decoders before enabling regular transmissions.
    for sid in SENSOR_IDS:
        key = f"3004/{sid}"
        if snapshot[key] != PERIOD:
            changes[key] = PERIOD
    return Plan(dict(snapshot), changes, bus, sensors, profile)
