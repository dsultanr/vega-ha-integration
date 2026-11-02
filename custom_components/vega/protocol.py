"""Pure Vega MT X CAN / Wialon IPS protocol logic.

Ported verbatim (where noted) from the standalone ``vega_bridge.py`` that ran on
cloudru-iot, trimmed to what the Home Assistant integration needs: no HTTP API,
no TLS passthrough, no Traccar forwarding, no Evolute cloud / MITM fallback.

Stdlib only — this module must stay importable without Home Assistant so it can
be unit-tested on its own.
"""
from __future__ import annotations

import struct
import math
import re
import time
import zlib

# --- Protocol command bytes ---
CMD_COMMAND = 0x01
CMD_COMMAND_ANS = 0x02
CMD_AUTORIZATION = 0x05
CMD_AUTORIZATION_ANS = 0x06
CMD_GET_FILE = 0x08
CMD_GET_FILE_ANS = 0x09
# 0x0A/0x0B are GET_DEV_INF / _ANS (the ANS carries the IMEI as ASCII digits).
CMD_GET_DEV_INF = 0x0A
CMD_GET_DEV_INF_ANS = 0x0B


def parse_authorization_pin(payload: bytes) -> str | None:
    """Extract the PIN from a CMD_AUTORIZATION payload (ASCII digits + padding)."""
    digits = ""
    for b in payload:
        if 0x30 <= b <= 0x39:
            digits += chr(b)
        elif digits:
            break
    return digits or None


def parse_imei(payload: bytes) -> str | None:
    """Extract an IMEI (>=10 ASCII digits) from a GET_DEV_INF_ANS payload."""
    best = ""
    cur = ""
    for b in payload:
        if 0x30 <= b <= 0x39:
            cur += chr(b)
            if len(cur) > len(best):
                best = cur
        else:
            cur = ""
    return best if len(best) >= 10 else None

# Levelled external output that holds a Remote session. It does not self-clear,
# so leaving it armed both swallows the next command (no rising edge) and drains
# the 12V battery. The runtime watchdog clears it when the session ends.
REMOTE_STATE_SENSOR = 2805  # 3 = Remote Start, 1 = idle
REMOTE_LEVEL_SETTING = 1341

CAN_SCRIPT_SIZE = 82
CAN_SCRIPT_COUNT = 8
SCENARIO_COUNT = 25
SCENARIO_SIZE = 50
SCENARIO_COMPARISONS = {"equal": 0, "greater": 1, "changed": 2}

# CAN script slots provisioned in Setting 5502 (reference / provisioning only).
CAN_SCRIPTS = {
    0: {"name": "в охрану", "desc": "Запереть центральный замок (постановка на охрану)"},
    1: {"name": "с охраны", "desc": "Отпереть центральный замок (снятие с охраны)"},
    2: {"name": "охлаждение", "desc": "Дистанционный запуск кондиционера (заводской)"},
    3: {"name": "22 auto", "desc": "Климат 22°C Auto (заводской)"},
    4: {"name": "прогрев", "desc": "Предпусковой прогрев салона и батареи (заводской)"},
    5: {"name": "RemoteOn", "desc": "Пользовательский слот; связь с выходами определяется сценариями"},
    6: {"name": "RemoteOff", "desc": "Пользовательский слот; связь с выходами определяется сценариями"},
    7: {"name": "гудок", "desc": "Подать звуковой сигнал (клаксон)"},
}

# Sensor id -> metadata, used by decode_sensor(). Expand this map to widen the
# telemetry the integration surfaces (see plan: sensor-ID discovery follow-up).
# "w" = value width in bytes (low bytes of the answer; the direct query pads to 8
# with zeros or stale garbage, so only the low w bytes are the value). "signed"
# for temperatures that can go below zero. floats read the low 4 bytes.
SENSOR_DEFS: dict[int, dict] = {
    1250: {"name": "tracker_temp", "title": "Температура трекера", "unit": "°C", "type": "float"},
    1252: {"name": "battery_12v", "title": "Бортовое напряжение 12V", "unit": "В", "type": "float"},
    1253: {"name": "backup_battery", "title": "Резервная АКБ", "unit": "В", "type": "float"},
    # Opt-in F515 CAN profile: Setting 5500 slots 48/49, CAN 0x104.
    # Values are already scaled to physical units by the tracker, FLOAT32.
    2922: {"name": "hv_battery_voltage", "title": "Напряжение ВВБ", "unit": "В", "type": "float"},
    2923: {"name": "hv_battery_current", "title": "Ток ВВБ", "unit": "А", "type": "float"},
    2005: {"name": "gsm_csq", "title": "Уровень GSM сигнала", "unit": "CSQ", "type": "int", "w": 1},
    2800: {"name": "odometer", "title": "Пробег", "unit": "км", "type": "int", "w": 2},
    2801: {"name": "speed", "title": "Скорость", "unit": "км/ч", "type": "int", "w": 1},
    2803: {"name": "soc", "title": "Заряд ВВБ", "unit": "%", "type": "int", "w": 1},
    2804: {"name": "range_ev", "title": "Запас хода EV", "unit": "км", "type": "int", "w": 2},
    2806: {"name": "ignition", "title": "Зажигание", "unit": "", "type": "bool"},
    2830: {"name": "central_lock", "title": "Центральный замок", "unit": "", "type": "bool"},
    2836: {"name": "trunk", "title": "Багажник", "unit": "", "type": "bool"},
    2850: {"name": "charger_plug", "title": "Подключение зарядного пистолета", "unit": "", "type": "bool"},
    2910: {"name": "charging_active", "title": "Процесс зарядки", "unit": "", "type": "bool"},
    2816: {"name": "outside_temp", "title": "Температура за бортом", "unit": "°C", "type": "int", "w": 1, "signed": True},
    2902: {"name": "inside_temp", "title": "Температура в салоне", "unit": "°C", "type": "int", "w": 1, "signed": True},
    # Extra IDs mapped 2026-10-24 by value cross-check against the evolute cloud
    # (same frame: fuel 83, remains_fuel 577, batt/coolant 12, climate target 22).
    2811: {"name": "fuel_percentage", "title": "Уровень топлива", "unit": "%", "type": "int", "w": 1},
    2812: {"name": "remains_mileage_fuel", "title": "Запас хода (топливо)", "unit": "км", "type": "int", "w": 2},
    2898: {"name": "battery_temp", "title": "Температура ВВБ", "unit": "°C", "type": "int", "w": 1, "signed": True},
    2901: {"name": "coolant_temp", "title": "Температура ОЖ", "unit": "°C", "type": "int", "w": 1, "signed": True},
    2906: {"name": "climate_target_temp", "title": "Климат: уставка", "unit": "°C", "type": "float"},
    # Doors: 2831-2834 are consecutive and all 0 while the car is closed (matches
    # evolute all-off). Which sub-id is which door is TENTATIVE until verified by
    # opening one and watching which flips.
    2831: {"name": "door_fl", "title": "Дверь водителя", "unit": "", "type": "bool"},
    2832: {"name": "door_fr", "title": "Дверь пассажира", "unit": "", "type": "bool"},
    2833: {"name": "door_rl", "title": "Дверь задняя левая", "unit": "", "type": "bool"},
    2834: {"name": "door_rr", "title": "Дверь задняя правая", "unit": "", "type": "bool"},
}


def decode_sensor(sid: int, val_bytes: bytes) -> dict:
    """Decode one sensor value against SENSOR_DEFS.

    Always yields a numeric value for int/float sensors, whatever byte width the
    tracker uses (a real tracker e.g. returns the odometer as an 8-byte LE int),
    so a numeric HA entity never receives a non-numeric string. Only genuinely
    unmapped sensors with odd widths fall back to a hex string.
    """
    sdef = SENSOR_DEFS.get(sid, {})
    stype = sdef.get("type", "unknown")
    scale = sdef.get("scale", 1.0)
    vlen = len(val_bytes)
    val = None
    if stype == "bool":
        if vlen >= 1:
            val = bool(val_bytes[0])
    elif stype == "float":
        # float32 in the low 4 bytes (the answer may be padded to 8).
        if vlen >= 4:
            val = round(struct.unpack("<f", val_bytes[:4])[0] * scale, 2)
    elif stype == "int":
        # Only the low `w` bytes are the value; higher bytes are padding/garbage.
        w = sdef.get("w", 1)
        signed = sdef.get("signed", False)
        if vlen >= 1:
            val = int.from_bytes(val_bytes[: min(w, vlen)], "little", signed=signed)
    else:  # unknown sensor: best-effort, never crash a numeric entity
        if vlen == 1:
            val = val_bytes[0]
        elif vlen in (2, 4, 8):
            val = int.from_bytes(val_bytes, "little")
        else:
            val = val_bytes.hex()

    return {
        "id": sid,
        "name": sdef.get("name", f"sensor_{sid}"),
        "title": sdef.get("title", f"Датчик {sid}"),
        "value": val if val is not None else val_bytes.hex(),
        "unit": sdef.get("unit", ""),
        "raw_hex": val_bytes.hex(),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# --- Vega binary frame construction (ported verbatim) ---

def build_frame(cmd: int, payload: bytes) -> bytes:
    flen = len(payload)
    body = struct.pack("<HB", flen, cmd) + payload
    crc = zlib.crc32(body)
    return body + struct.pack("<I", crc)


def build_auth_frame(pin: str) -> bytes:
    pin_bytes = pin.encode("ascii")
    pad_len = 5 if len(pin_bytes) <= 4 else 17
    payload = bytearray(pad_len)
    payload[: len(pin_bytes)] = pin_bytes
    return build_frame(CMD_AUTORIZATION, bytes(payload))


def build_auth_ack_frame(success: bool = True) -> bytes:
    return build_frame(CMD_AUTORIZATION_ANS, bytes([0x01 if success else 0x00]))


def build_query_sensor_frame(req_id: int, sensor_id: int, sub_id: int = 0) -> bytes:
    # Subcmd 0x03 = Query Sensor / Read Setting
    payload = struct.pack("<IBBHH", req_id, 0x03, 4, sensor_id, sub_id)
    return build_frame(CMD_COMMAND, payload)


def build_set_setting_frame(req_id: int, setting_id: int, sub_id: int, val: bytes) -> bytes:
    # Subcmd 0x02 = Set Setting
    sub_payload = struct.pack("<HHB", setting_id, sub_id, len(val)) + val
    payload = struct.pack("<IBB", req_id, 0x02, len(sub_payload)) + sub_payload
    return build_frame(CMD_COMMAND, payload)


def format_script_slot(delay_ms: int, can_id: int, bus: int, payload: bytes) -> bytes:
    """Format one 15-byte slot in a Setting 5502 buffer."""
    delay_word = (delay_ms & 0x7FFF) | 0x8000
    p8 = (payload[:8] if payload else b"").ljust(8, b"\x00")
    return struct.pack("<HIB", delay_word, can_id, bus) + p8


def explain_can_frame(bus: int, can_id: int, data: bytes | str) -> str:
    """Provide a human-readable explanation of a known CAN frame for Evolute / Seres / DFSK."""
    if isinstance(data, str):
        data = bytes.fromhex(data)
    else:
        data = bytes(data)

    pfx = f"0x{can_id:03X}"
    bus_label = "Body" if bus == 1 else ("EV/Diag" if bus == 2 else f"Bus{bus}")

    # UDS Diagnostic requests
    if can_id == 0x701:
        if data.startswith(bytes.fromhex("03220203aa21")):
            return f"UDS ({pfx}, {bus_label}): Постановка на охрану (закрыть ЦЗ)"
        if data.startswith(bytes.fromhex("032202030000")):
            return f"UDS ({pfx}, {bus_label}): Снятие с охраны (открыть ЦЗ)"
        if data.startswith(bytes.fromhex("03220203")):
            return f"UDS ({pfx}, {bus_label}): Запрос состояния/управления ЦЗ (DID 0x0203)"
        if data.startswith(bytes.fromhex("062f010303080055")):
            return f"UDS ({pfx}, {bus_label}): Включение звукового сигнала (Horn ON)"
        if data.startswith(bytes.fromhex("062f010303000055")):
            return f"UDS ({pfx}, {bus_label}): Выключение звукового сигнала (Horn OFF)"
        if data.startswith(bytes.fromhex("062f0103")):
            return f"UDS ({pfx}, {bus_label}): Управление клаксоном (IOCBI DID 0x0103)"
        return f"UDS Diag ({pfx}, {bus_label}): Диагностический запрос"

    # BCM / Central Gateway
    if can_id == 0x011:
        parts = []
        if len(data) > 0 and data[0] == 0x04:
            parts.append("сигнал клаксона")
        if len(data) >= 3:
            if data[1:3] == b"\x44\x0a":
                parts.append("включение охлаждения (A/C ON)")
            elif data[1:3] == b"\x58\x0a":
                parts.append("климат Auto 22°C")
            elif data[1:3] == b"\x80\x0a":
                parts.append("максимальный прогрев (Heating ON)")
            elif data[1:3] == b"\x00\x09":
                parts.append("сброс триггера команды (снятие импульса / idle)")
        if len(data) > 3 and data[3] == 0x5B:
            parts.append("предустановка обдува климата")
        if len(data) >= 6:
            if data[4:6] == b"\xaa\x21":
                parts.append("закрытие центрального замка (Lock)")
            elif data[4:6] == b"\x00\x02":
                parts.append("открытие центрального замка (Unlock)")
        if len(data) > 5 and data[5] == 0x05:
            parts.append("открытие замка багажника")
        if parts:
            joined = ", ".join(parts)
            return f"BCM ({pfx}, {bus_label}): {joined}"
        return f"BCM ({pfx}, {bus_label}): Команда кузовной электроники"

    # Observed in climate scripts; a standalone Remote On effect is unverified.
    if can_id == 0x092:
        if len(data) > 2 and data[2] == 0x60:
            return f"CAN ({pfx}, {bus_label}): Кадр из климатических скриптов; самостоятельный Remote On не подтверждён"
        return f"CAN ({pfx}, {bus_label}): Назначение кадра не подтверждено"

    # HVAC Comfort preset
    if can_id == 0x093:
        if data.startswith(bytes.fromhex("24c3ccc0")):
            return f"HVAC ({pfx}, {bus_label}): Уставка климата 22°C Auto"
        return f"HVAC ({pfx}, {bus_label}): Конфигурация климат-контроля"

    # Tailgate control
    if can_id == 0x09A:
        if len(data) > 0 and data[0] == 0x10:
            return f"Tailgate ({pfx}, {bus_label}): Открытие электропривода багажника"
        if len(data) > 0 and data[0] == 0x00:
            return f"Tailgate ({pfx}, {bus_label}): Закрытие электропривода багажника"
        return f"Tailgate ({pfx}, {bus_label}): Модуль двери багажника"

    # HVAC target control
    if can_id == 0x394:
        if data.startswith(bytes.fromhex("fcff0fff")):
            return f"HVAC ({pfx}, {bus_label}): Запрос максимального прогрева (HI, максимальный нагрев)"
        if data.startswith(bytes.fromhex("7cff0fff")):
            return f"HVAC ({pfx}, {bus_label}): Запрос поддержания 22°C (Auto)"
        if data.startswith(bytes.fromhex("4100")):
            return f"HVAC ({pfx}, {bus_label}): Запрос кондиционирования (Cooling LO)"
        return f"HVAC ({pfx}, {bus_label}): Управление целевым режимом климата"

    return f"CAN ({pfx}, {bus_label}): {data.hex()}"


def explain_can_script(script: dict) -> str:
    """Summarize what a CAN script does based on its constituent frames."""
    frames = script.get("frames", [])
    if not frames:
        return "Пустой скрипт"

    can_ids = {f.get("can_id") for f in frames}
    all_data = [bytes.fromhex(f.get("data", "")) for f in frames]

    has_lock = any(
        d.startswith(bytes.fromhex("03220203aa21")) or (len(d) >= 6 and d[4:6] == b"\xaa\x21")
        for d in all_data
    )
    has_unlock = any(
        d.startswith(bytes.fromhex("032202030000")) or (len(d) >= 6 and d[4:6] == b"\x00\x02")
        for d in all_data
    )
    has_horn = any(
        d.startswith(bytes.fromhex("062f0103")) or (len(d) > 0 and d[0] == 0x04)
        for d in all_data
    )
    has_preheat = any(
        d.startswith(bytes.fromhex("fcff0fff")) or (len(d) >= 3 and d[1:3] == b"\x80\x0a")
        for d in all_data
    )
    has_cooling = any(
        d.startswith(bytes.fromhex("4100")) or (len(d) >= 3 and d[1:3] == b"\x44\x0a")
        for d in all_data
    )
    has_auto22 = any(
        d.startswith(bytes.fromhex("7cff0fff"))
        or d.startswith(bytes.fromhex("24c3ccc0"))
        or (len(d) >= 3 and d[1:3] == b"\x58\x0a")
        for d in all_data
    )
    has_reset = any(f.get("can_id") == 0x011 and len(d) >= 3 and d[1:3] == b"\x00\x09"
                    for f, d in zip(frames, all_data))
    has_trunk_open = any(
        d.startswith(bytes.fromhex("1000ffff")) or (len(d) > 5 and d[5] == 0x05)
        for d in all_data
    )
    has_trunk_close = any(d.startswith(bytes.fromhex("0000ffff")) for d in all_data)

    if has_cooling:
        return "Включение охлаждения салона (HVAC + BCM)"
    if has_auto22:
        return "Включение климат-контроля 22°C Auto (HVAC + BCM)"
    if has_preheat:
        return "Включение максимального прогрева салона (HVAC + BCM)"
    if has_lock:
        return "Закрытие центрального замка / постановка на охрану (UDS + BCM)"
    if has_unlock:
        return "Открытие центрального замка / снятие с охраны (UDS + BCM)"
    if has_horn:
        return "Звуковой сигнал клаксона (UDS + BCM)"
    if has_trunk_open:
        return "Открытие багажника (BCM + Tailgate)"
    if has_trunk_close:
        return "Закрытие багажника (Tailgate)"
    if has_reset:
        return "Сброс триггера BCM; самостоятельный Remote Off не подтверждён"

    names = []
    if 0x701 in can_ids:
        names.append("UDS Diag")
    if 0x011 in can_ids:
        names.append("BCM")
    if 0x092 in can_ids:
        names.append("VCU")
    if 0x093 in can_ids or 0x394 in can_ids:
        names.append("HVAC")
    if 0x09A in can_ids:
        names.append("Tailgate")
    if names:
        joined_names = ", ".join(names)
        return f"Команды в блоки: {joined_names}"
    return f"Пользовательский CAN-скрипт ({len(frames)} кадр.)"


def decode_can_script(buf: bytes) -> dict:
    """Decode an 82-byte Setting 5502 slot buffer into a readable dict.

    Inverse of build_can_script: {name, repeat, frames:[{delay_ms, can_id, bus,
    data (hex), description}], summary}. Only non-padding frames (the 4ms/id0 filler)
    are returned.
    """
    if len(buf) != CAN_SCRIPT_SIZE:
        raise ValueError("CAN-скрипт должен содержать ровно 82 байта")
    name = buf[:16].split(b"\x00")[0].decode("cp1251", "replace")
    frames = []
    for s in range(4):
        off = 17 + s * 15
        delay_word = struct.unpack("<H", buf[off : off + 2])[0]
        can_id = struct.unpack("<I", buf[off + 2 : off + 6])[0]
        bus = buf[off + 6]
        data = buf[off + 7 : off + 15]
        delay_ms = delay_word & 0x7FFF
        # filler slot from build_can_script: delay 4ms, id 0, zero payload
        if can_id == 0 and bus == 0 and not any(data) and delay_word == 0x8004:
            continue
        frames.append(
            {
                "delay_ms": delay_ms,
                "can_id": can_id,
                "bus": bus,
                "data": data.hex(),
                "description": explain_can_frame(bus, can_id, data),
            }
        )
    repeat = buf[77] if len(buf) > 77 else 1
    res = {"name": name, "repeat": repeat, "frames": frames, "raw": buf.hex()}
    res["summary"] = explain_can_script(res)
    return res


def build_can_script_from_dict(spec: dict) -> bytes:
    """Validate an editor document before encoding; never truncate user input."""
    if not isinstance(spec, dict):
        raise ValueError("Скрипт должен быть JSON-объектом")
    _check_fields(spec, {"name", "repeat", "frames"}, {"summary", "raw", "slot"}, "script")
    name = spec["name"]
    if not isinstance(name, str) or any(ord(c) < 32 for c in name):
        raise ValueError("name: ожидается строка без управляющих символов")
    try:
        encoded = name.encode("cp1251")
    except UnicodeEncodeError as err:
        raise ValueError("name: имя должно состоять из символов Windows-1251") from err
    if len(encoded) > 16:
        raise ValueError("name: максимум 16 байт Windows-1251")
    _integer(spec["repeat"], 0, 255, "repeat")
    frames = spec["frames"]
    if not isinstance(frames, list) or len(frames) > 4:
        raise ValueError("frames: требуется список из 0–4 кадров")
    for i, frame in enumerate(frames):
        label = f"frames[{i}]"
        if not isinstance(frame, dict):
            raise ValueError(f"{label}: ожидается объект")
        _check_fields(frame, {"delay_ms", "can_id", "bus", "data"}, {"description"}, label)
        _integer(frame["delay_ms"], 0, 32767, f"{label}.delay_ms")
        _integer(frame["can_id"], 0, 0x1FFFFFFF, f"{label}.can_id")
        _integer(frame["bus"], 0, 3, f"{label}.bus")
        if not isinstance(frame["data"], str) or not re.fullmatch(r"[0-9a-fA-F]{16}", frame["data"]):
            raise ValueError(f"{label}.data: нужны ровно 8 байт (16 HEX-символов)")
    slots = [
        (
            int(f.get("delay_ms", 4)),
            int(f.get("can_id", 0)),
            int(f.get("bus", 0)),
            bytes.fromhex(f.get("data", "")),
        )
        for f in spec.get("frames", [])
    ]
    return build_can_script(spec.get("name", ""), slots, int(spec.get("repeat", 1)))


def _check_fields(value: dict, required: set, optional: set, label: str) -> None:
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ValueError(f"{label}: отсутствуют поля {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{label}: неизвестные поля {', '.join(sorted(unknown))}")


def _integer(value, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label}: требуется целое число {minimum}…{maximum}")
    return value


def validate_script_slot(slot: int) -> int:
    return _integer(slot, 0, CAN_SCRIPT_COUNT - 1, "slot")


def decode_scenario(buf: bytes, index: int) -> dict:
    """Read conditions and the CAN selector from a 50-byte Setting 4001 record.

    Windows Configurator 1.28.46 ScenarioActionDialog: word[48] >> 10 is
    the CAN selector (0 = none, 1..8 = slots 0..7). Other actions are retained
    in raw; this decoder does not claim a scenario only sends CAN frames.
    """
    if len(buf) != 50:
        raise ValueError(f"Сценарий {index}: ожидалось 50 байт")
    conditions = []
    for off in (0, 8):
        sensor, threshold, comparison = struct.unpack_from("<HfB", buf, off)
        if sensor:
            if not math.isfinite(threshold) or comparison not in (0, 1, 2):
                raise ValueError(f"Сценарий {index}: неизвестное условие")
            conditions.append({"sensor": sensor, "threshold": threshold, "comparison": comparison})
    selector = buf[49] >> 2
    if selector > CAN_SCRIPT_COUNT or buf[7] & ~1:
        raise ValueError(f"Сценарий {index}: неподдерживаемый формат")
    result = {"index": index, "conditions": conditions, "all": bool(buf[7] & 1),
              "slot": selector - 1 if selector else None, "raw": buf.hex()}
    result["document"] = scenario_document(buf)
    # Keep actions not represented by the editor verbatim (SMS, calls, built-in
    # vehicle actions, physical outputs). Never turn them into silent zeroes.
    result["other_actions_hex"] = (buf[15:42] + buf[46:49] + bytes([buf[49] & 3])).hex()
    result["has_other_actions"] = any(bytes.fromhex(result["other_actions_hex"]))
    try:
        result["editable"] = build_scenario_from_dict(result["document"], buf) == buf
    except ValueError:
        result["editable"] = False
    return result


def scenario_document(buf: bytes) -> dict:
    """Editor fields; fixed condition positions retain disabled sensor values."""
    if len(buf) != SCENARIO_SIZE:
        raise ValueError("Сценарий должен содержать ровно 50 байт")
    conditions = []
    for offset in (0, 8):
        sensor, value, comparison = struct.unpack_from("<HfB", buf, offset)
        conditions.append(None if not sensor else {
            "sensor": sensor, "comparison": next((k for k, v in SCENARIO_COMPARISONS.items() if v == comparison), "unknown"),
            "value": value,
        })
    return {"conditions": conditions, "match": "all" if buf[7] & 1 else "any", "actions": {
        "external_output_on": buf[42], "external_output_on_seconds": buf[43],
        "external_output_off": buf[44], "external_output_off_seconds": buf[45],
        "can_script": (buf[49] >> 2) - 1 if buf[49] >> 2 else None,
    }}


def build_scenario_from_dict(spec: dict, original: bytes) -> bytes:
    """Patch verified fields in a read snapshot, preserving every other byte.

    Configurator 1.28.46 ScenarioActionDialog: raw[42/43] is external output
    ON/time, raw[44/45] OFF/time. The CAN selector shares byte 49 with flags.
    This is a set of actions; firmware execution order is not established.
    """
    if not isinstance(spec, dict):
        raise ValueError("Сценарий должен быть объектом")
    _check_fields(spec, {"conditions", "match", "actions"}, set(), "scenario")
    before = scenario_document(original)
    conditions = spec["conditions"]
    if not isinstance(conditions, list) or len(conditions) != 2:
        raise ValueError("conditions: нужны два условия; неиспользуемое — null")
    buf = bytearray(original)
    for i, condition in enumerate(conditions):
        offset = i * 8
        if condition is None:
            if before["conditions"][i] is not None:
                buf[offset:offset + 7] = bytes(7)
            continue
        if not isinstance(condition, dict):
            raise ValueError("Условие должно быть объектом или null")
        _check_fields(condition, {"sensor", "comparison", "value"}, set(), f"conditions[{i}]")
        _integer(condition["sensor"], 1, 65535, "sensor")
        comparison = condition["comparison"]
        if not isinstance(comparison, str) or comparison not in SCENARIO_COMPARISONS:
            raise ValueError("comparison: equal, greater или changed")
        value = condition["value"]
        if type(value) not in (int, float):
            raise ValueError("value: требуется конечное число float32")
        try:
            encoded = struct.pack("<HfB", condition["sensor"], value, SCENARIO_COMPARISONS[comparison])
            if not math.isfinite(struct.unpack_from("<f", encoded, 2)[0]):
                raise ValueError("value: требуется конечное число float32")
        except (OverflowError, struct.error) as err:
            raise ValueError("value: число вне диапазона float32") from err
        if condition != before["conditions"][i]:
            buf[offset:offset + 7] = encoded
    if spec["match"] not in ("any", "all"):
        raise ValueError("match: any или all")
    buf[7] = (buf[7] & ~1) | (spec["match"] == "all")
    actions = spec["actions"]
    if not isinstance(actions, dict):
        raise ValueError("actions: требуется объект")
    _check_fields(actions, set(before["actions"]), set(), "actions")
    for key, offset, maximum in (("external_output_on", 42, 15), ("external_output_on_seconds", 43, 255),
                                 ("external_output_off", 44, 15), ("external_output_off_seconds", 45, 255)):
        buf[offset] = _integer(actions[key], 0, maximum, key)
    slot = actions["can_script"]
    if slot is not None:
        validate_script_slot(slot)
    buf[49] = (buf[49] & 3) | (0 if slot is None else (slot + 1) << 2)
    return bytes(buf)


def command_script_bindings(scenarios: list[dict]) -> dict:
    """Resolve configured event routes, without claiming physical execution.

    A partial read cannot establish absence/uniqueness of a route. An AND
    condition on another sensor is conditional, even if the command matches.
    """
    complete = len(scenarios) == SCENARIO_COUNT and {s["index"] for s in scenarios} == set(range(SCENARIO_COUNT))
    result = {}
    for action, spec in COMMAND_MAP.items():
        routes = []
        scenario_routes = []
        for scenario in scenarios:
            conditions = scenario["conditions"]
            matches = []
            for c in conditions:
                match = c["sensor"] == spec["setting_id"] and (
                    (c["comparison"] == 0 and spec["val"] == c["threshold"])
                    or (c["comparison"] == 1 and spec["val"] > c["threshold"])
                    or c["comparison"] == 2
                )
                matches.append(match)
            if any(matches):
                route = {"scenario": scenario["index"], "slot": scenario["slot"],
                         "conditional": scenario["all"] and not all(matches)}
                scenario_routes.append(route)
                if scenario["slot"] is not None:
                    routes.append(route)
        slots = sorted({r["slot"] for r in routes})
        status = "unavailable" if not complete else (
            "conditional" if any(r["conditional"] for r in routes) else
            "multiple" if len(slots) > 1 else "single" if slots else "none"
        )
        result[action] = {"setting_id": spec["setting_id"], "value": spec["val"],
                          "status": status, "slots": slots, "routes": routes,
                          "scenario_routes": scenario_routes}
    return result


def build_can_script(name: str, slots: list, repeat: int = 1) -> bytes:
    """Build the complete 82-byte buffer for Setting 5502."""
    name_bytes = name.encode("cp1251", errors="replace")[:16].ljust(16, b"\x00")
    hdr = b"\x20"
    slots_data = bytearray()
    for s in range(4):
        if s < len(slots):
            delay_ms, can_id, bus, p8 = slots[s]
            slots_data.extend(format_script_slot(delay_ms, can_id, bus, p8))
        else:
            slots_data.extend(format_script_slot(4, 0, 0, b"\x00" * 8))
    trailer = struct.pack("<BII", repeat, 0, 0)[:5]
    return bytes(name_bytes + hdr + slots_data + trailer)


# --- Vega binary frame parsing from a byte buffer ---

def iter_frames(buf: bytearray):
    """Yield (cmd, payload) for every complete, CRC-valid frame in ``buf``.

    Consumes the bytes it decodes from ``buf`` in place, exactly like the inline
    loop in vega_bridge.handle_vega_client. Stops when fewer than a full frame
    remains, leaving the tail in ``buf`` for the next read.
    """
    while len(buf) >= 7:
        flen = buf[0] | (buf[1] << 8)
        if flen > 2048 or flen < 0:
            buf.pop(0)
            continue
        total_len = flen + 7
        if len(buf) < total_len:
            break
        frame_bytes = bytes(buf[:total_len])
        del buf[:total_len]

        body = frame_bytes[: flen + 3]
        expected_crc = zlib.crc32(body)
        actual_crc = struct.unpack("<I", frame_bytes[flen + 3 : flen + 7])[0]
        if expected_crc != actual_crc:
            # CRC mismatch: drop the frame, keep scanning.
            continue
        cmd = frame_bytes[2]
        payload = frame_bytes[3 : 3 + flen]
        yield cmd, payload


def parse_command_ans(payload: bytes):
    """Parse a CMD_COMMAND_ANS payload. Returns a dict or None if too short.

    Mirrors the decoding in vega_bridge.process_packet: a request-reply envelope
    plus, for subcmd 0x01/0x03, an embedded sensor sample.
    """
    if len(payload) < 5:
        return None
    req_id = struct.unpack("<I", payload[:4])[0]
    subcmd = payload[4]
    data = payload[6:] if len(payload) > 6 else b""
    res_code = payload[5] if len(payload) > 5 else 0
    if len(data) > 0:
        res_code = data[0]

    result = {"req_id": req_id, "subcmd": subcmd, "result_code": res_code, "data": data}

    if subcmd in (0x01, 0x03) and len(data) >= 3:
        sid = data[0] | (data[1] << 8)
        vlen = data[2]
        val_bytes = data[3 : 3 + vlen] if len(data) >= 3 + vlen else data[3:]
        result["sensor"] = decode_sensor(sid, val_bytes)
    return result


# --- Wialon IPS telemetry parsing (NEW: bridge delegated this to Traccar) ---

def _wialon_coord(raw: str, hemi: str, deg_digits: int) -> float | None:
    """Convert a Wialon ddmm.mmmm / dddmm.mmmm coordinate to signed degrees."""
    if not raw or not hemi:
        return None
    try:
        dot = raw.index(".")
    except ValueError:
        # No decimal point: still try, treating last 2 integer digits as minutes.
        dot = len(raw)
    # Minutes are the two integer digits immediately left of the decimal point.
    deg = float(raw[: dot - 2]) if dot >= 2 else 0.0
    minutes = float(raw[dot - 2 :])
    value = deg + minutes / 60.0
    if hemi.upper() in ("S", "W"):
        value = -value
    return round(value, 6)


def _wialon_timestamp(date_s: str, time_s: str) -> str | None:
    """ddmmyy + hhmmss (UTC) -> ISO 8601 string, or None if absent/NA."""
    if not date_s or not time_s or date_s == "NA" or time_s == "NA":
        return None
    try:
        dd, mm, yy = int(date_s[0:2]), int(date_s[2:4]), int(date_s[4:6])
        hh, mi, ss = int(time_s[0:2]), int(time_s[2:4]), int(time_s[4:6])
        return f"20{yy:02d}-{mm:02d}-{dd:02d}T{hh:02d}:{mi:02d}:{ss:02d}+00:00"
    except (ValueError, IndexError):
        return None


def _parse_wialon_params(raw: str) -> dict:
    """Parse the trailing ``name:type:value,...`` params block of a #D# packet."""
    params: dict[str, object] = {}
    if not raw:
        return params
    for item in raw.split(","):
        bits = item.split(":")
        if len(bits) < 3:
            continue
        name, ptype, value = bits[0], bits[1], ":".join(bits[2:])
        try:
            if ptype == "1":
                params[name] = int(value)
            elif ptype == "2":
                params[name] = float(value)
            else:
                params[name] = value
        except ValueError:
            params[name] = value
    return params


def parse_wialon_data(line_str: str) -> dict | None:
    """Parse a Wialon IPS #D#/#SD#/#B# data record into a telemetry dict.

    Returns the navigation fields (latitude, longitude, speed, course, altitude,
    satellites, hdop, position_time) plus any named params. For a #B# blackbox
    batch, the newest (last) record wins. Returns None if the line is not a data
    record or carries no usable fix.
    """
    for tag in ("#SD#", "#D#", "#B#"):
        if line_str.startswith(tag):
            body = line_str[len(tag):]
            break
    else:
        return None

    if body.endswith("#"):
        body = body[:-1]

    # #B# is a batch of data records separated by '|'; take the most recent.
    if line_str.startswith("#B#"):
        records = [r for r in body.split("|") if r]
        if not records:
            return None
        body = records[-1]

    f = body.split(";")
    if len(f) < 10:
        return None

    out: dict[str, object] = {}
    out["position_time"] = _wialon_timestamp(f[0], f[1])
    out["latitude"] = _wialon_coord(f[2], f[3], 2)
    out["longitude"] = _wialon_coord(f[4], f[5], 3)

    def _num(idx: int, cast):
        if idx < len(f) and f[idx] not in ("", "NA"):
            try:
                return cast(f[idx])
            except ValueError:
                return None
        return None

    out["speed"] = _num(6, int)
    out["course"] = _num(7, int)
    out["altitude"] = _num(8, int)
    out["satellites"] = _num(9, int)
    out["hdop"] = _num(10, float)

    # #D# carries params beyond index 14 (inputs;outputs;adc;ibutton;params).
    if len(f) > 15:
        out["params"] = _parse_wialon_params(f[15])

    # A record with no satellites / null island is a no-fix heartbeat.
    if out["latitude"] is None or out["longitude"] is None:
        out.pop("latitude", None)
        out.pop("longitude", None)
    return out


# --- Direct command map (ported from vega_bridge /api/cmd NATIVE_CMDS) ---
# action -> (setting_id, sub_id, value_byte). Verified from cloud MITM dissection.
# The cloud / MITM fallbacks and the trunk alias are intentionally dropped.
COMMAND_MAP: dict[str, dict] = {
    "lock": {"setting_id": 1349, "sub_id": 0, "val": 1, "desc": "ЦЗ Закрыть (Setting 1349=1)"},
    "unlock": {"setting_id": 1350, "sub_id": 0, "val": 1, "desc": "ЦЗ Открыть (Setting 1350=1)"},
    "cooling": {"setting_id": 1346, "sub_id": 0, "val": 1, "desc": "Включить охлаждение (Setting 1346=1)"},
    "cooling_off": {"setting_id": 1341, "sub_id": 0, "val": 0, "desc": "Выключить охлаждение (Setting 1341=0)"},
    "preheat": {"setting_id": 1347, "sub_id": 0, "val": 1, "desc": "Включить прогрев (Setting 1347=1)"},
    "preheat_off": {"setting_id": 1341, "sub_id": 0, "val": 0, "desc": "Выключить прогрев (Setting 1341=0)"},
    "remote_on": {"setting_id": 1341, "sub_id": 0, "val": 1, "desc": "Remote ON (Setting 1341=1)"},
    "remote_off": {"setting_id": 1341, "sub_id": 0, "val": 0, "desc": "Remote OFF (Setting 1341=0)"},
    "horn": {"setting_id": 1348, "sub_id": 0, "val": 1, "desc": "Гудок (Setting 1348=1)"},
}
