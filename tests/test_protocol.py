"""Unit tests for the pure protocol layer (no Home Assistant needed).

Run directly:  python3 tests/test_protocol.py
or with pytest: pytest tests/test_protocol.py
"""
import os
import struct
import sys

# protocol.py is stdlib-only; import it without triggering the HA package __init__.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "vega"))
import protocol  # noqa: E402


def _make_command_ans(req_id: int, subcmd: int, data: bytes) -> bytes:
    payload = struct.pack("<IBB", req_id, subcmd, len(data)) + data
    return protocol.build_frame(protocol.CMD_COMMAND_ANS, payload)


def test_decode_sensor_int_soc():
    s = protocol.decode_sensor(2803, bytes([85]))
    assert s["name"] == "soc"
    assert s["value"] == 85
    assert s["unit"] == "%"


def test_decode_sensor_bool_lock():
    assert protocol.decode_sensor(2830, bytes([1]))["value"] is True
    assert protocol.decode_sensor(2830, bytes([0]))["value"] is False


def test_decode_sensor_float_voltage():
    raw = struct.pack("<f", 12.75)
    assert protocol.decode_sensor(1252, raw)["value"] == 12.75


def test_decode_sensor_tracker_metadata_and_climate_fan():
    assert protocol.decode_sensor(3, b"A011B08A\x00padding")["value"] == "A011B08A"
    assert protocol.decode_sensor(9, bytes.fromhex("7856341200000000"))["value"] == 0x12345678
    assert protocol.decode_sensor(2907, bytes([7]))["value"] == 7


def test_decode_sensor_can_matrix_names():
    assert protocol.decode_sensor(2813, bytes([3]))["value"] == "P"
    assert protocol.decode_sensor(2813, bytes([9]))["value"] == "9"
    assert protocol.decode_sensor(2813, bytes([1]))["name"] == "gear"
    assert protocol.decode_sensor(2843, bytes([1]))["value"] is True
    assert protocol.decode_sensor(2912, bytes([2]))["name"] == "seat_heater_driver"
    assert protocol.decode_sensor(2807, bytes.fromhex("2c01"))["value"] == 300


def test_decode_sensor_odometer_8byte():
    # real tracker sends the odometer as an 8-byte LE int (881f.. = 8072)
    assert protocol.decode_sensor(2800, bytes.fromhex("881f000000000000"))["value"] == 8072
    # and the Combine-derived 2-byte form decodes to the same number
    assert protocol.decode_sensor(2800, bytes([0x88, 0x1f]))["value"] == 8072


def test_decode_sensor_float_8byte_is_numeric():
    # a float-typed sensor given a non-4-byte width must still be numeric, never a
    # hex string (which would crash a numeric HA entity)
    v = protocol.decode_sensor(1252, bytes.fromhex("0c00000000000000"))["value"]
    assert isinstance(v, (int, float))


def test_frame_roundtrip_and_iter():
    f1 = _make_command_ans(1001, 0x03, bytes([0xF3, 0x0A, 0x01, 85]))  # sid 2803
    f2 = _make_command_ans(1002, 0x03, bytes([0x1E, 0x0B, 0x01, 1]))   # sid 2846? no -> 2846
    buf = bytearray(f1 + f2)
    frames = list(protocol.iter_frames(buf))
    assert len(frames) == 2
    assert len(buf) == 0  # fully consumed
    cmd, payload = frames[0]
    assert cmd == protocol.CMD_COMMAND_ANS
    parsed = protocol.parse_command_ans(payload)
    assert parsed["req_id"] == 1001
    assert parsed["sensor"]["name"] == "soc"
    assert parsed["sensor"]["value"] == 85


def test_iter_frames_partial_tail():
    f1 = _make_command_ans(1001, 0x03, bytes([0xF3, 0x0A, 0x01, 85]))
    buf = bytearray(f1 + f1[:5])  # one whole frame + a partial second
    frames = list(protocol.iter_frames(buf))
    assert len(frames) == 1
    assert len(buf) == 5  # partial tail preserved for the next read


def test_iter_frames_crc_mismatch_dropped():
    f1 = bytearray(_make_command_ans(1001, 0x03, bytes([0xF3, 0x0A, 0x01, 85])))
    f1[-1] ^= 0xFF  # corrupt CRC
    frames = list(protocol.iter_frames(bytearray(f1)))
    assert frames == []


def test_parse_wialon_data_full():
    line = "#D#200924;143000;5551.1234;N;03735.5678;E;42;180;150;9;1.2;0;0;;;soc:1:85"
    out = protocol.parse_wialon_data(line)
    assert abs(out["latitude"] - 55.852057) < 1e-5
    assert abs(out["longitude"] - 37.592797) < 1e-5
    assert out["speed"] == 42
    assert out["course"] == 180
    assert out["altitude"] == 150
    assert out["satellites"] == 9
    assert abs(out["hdop"] - 1.2) < 1e-9
    assert out["position_time"] == "2024-09-20T14:30:00+00:00"
    assert out["params"]["soc"] == 85


def test_parse_wialon_short_data():
    line = "#SD#200924;143000;5551.1234;N;03735.5678;E;0;0;150;9"
    out = protocol.parse_wialon_data(line)
    assert out["speed"] == 0
    assert "params" not in out


def test_parse_wialon_no_fix():
    line = "#SD#NA;NA;;;;;;;;0"
    out = protocol.parse_wialon_data(line)
    assert "latitude" not in out and "longitude" not in out


def test_parse_wialon_blackbox_takes_last():
    rec = "200924;143000;5551.1234;N;03735.5678;E;42;180;150;9"
    rec2 = "200924;143100;5552.0000;N;03736.0000;E;50;90;151;10"
    out = protocol.parse_wialon_data(f"#B#{rec}|{rec2}")
    assert out["speed"] == 50  # newest record wins


def test_parse_wialon_non_data_returns_none():
    assert protocol.parse_wialon_data("#L#2.0;000000000000000;;ABCD") is None


def test_decode_direct_query_padding():
    # the direct query returns 8-byte answers; only the low `w` bytes are the value
    assert protocol.decode_sensor(2816, bytes.fromhex("0b00000000000000"))["value"] == 11
    # garbage padding in the high bytes must be ignored (w=1) -> real value 10
    assert protocol.decode_sensor(2898, bytes.fromhex("0a00414536433242"))["value"] == 10
    assert protocol.decode_sensor(2901, bytes.fromhex("0a00414536433242"))["value"] == 10
    # float32 lives in the low 4 bytes (climate target = 22.0)
    assert protocol.decode_sensor(2906, bytes.fromhex("0000b04100000000"))["value"] == 22.0
    # odometer is 2 bytes
    assert protocol.decode_sensor(2800, bytes.fromhex("901f000000000000"))["value"] == 8080


def test_decode_negative_temp():
    # outside_temp is signed: 0xFE = -2 C
    assert protocol.decode_sensor(2816, bytes.fromhex("fe00000000000000"))["value"] == -2


def test_parse_authorization_pin():
    # CMD_AUTORIZATION payload shape: ascii PIN "1234" + 0x01 pad
    assert protocol.parse_authorization_pin(bytes.fromhex("3132333401")) == "1234"
    assert protocol.parse_authorization_pin(b"\x01\x00") is None


def test_parse_imei():
    # GET_DEV_INF_ANS payload: ascii IMEI then binary
    payload = bytes.fromhex("303030303030303030303030303030005cf4000000bb953ead")
    assert protocol.parse_imei(payload) == "000000000000000"


def test_command_map_shape():
    for action, spec in protocol.COMMAND_MAP.items():
        assert set(spec) >= {"setting_id", "sub_id", "val"}
        assert spec["val"] in (0, 1)
    assert protocol.COMMAND_MAP["lock"]["setting_id"] == 1349
    assert protocol.COMMAND_MAP["remote_on"]["setting_id"] == protocol.REMOTE_LEVEL_SETTING


def test_explain_can_frame_and_script():
    # Lock script
    f1 = (122, 0x701, 2, bytes.fromhex("03220203aa210000"))
    f2 = (5, 0x011, 1, bytes.fromhex("00000000aa210000"))
    raw_lock = protocol.build_can_script("в охрану", [f1, f2], repeat=1)
    dec_lock = protocol.decode_can_script(raw_lock)
    assert dec_lock["name"] == "в охрану"
    assert len(dec_lock["frames"]) == 2
    assert "закрыть ЦЗ" in dec_lock["frames"][0]["description"]
    assert "закрытие центрального замка" in dec_lock["frames"][1]["description"]
    assert "Закрытие центрального замка" in dec_lock["summary"]

    # Horn script
    f_horn1 = (122, 0x701, 1, bytes.fromhex("062f010303080055"))
    f_horn2 = (6, 0x701, 1, bytes.fromhex("062f010303000055"))
    f_horn3 = (5, 0x011, 1, bytes.fromhex("0400000000000000"))
    raw_horn = protocol.build_can_script("гудок", [f_horn1, f_horn2, f_horn3], repeat=1)
    dec_horn = protocol.decode_can_script(raw_horn)
    assert "Horn ON" in dec_horn["frames"][0]["description"]
    assert "Horn OFF" in dec_horn["frames"][1]["description"]
    assert "клаксона" in dec_horn["frames"][2]["description"]
    assert "Звуковой сигнал клаксона" in dec_horn["summary"]

    # RemoteOn script
    f_on1 = (2000, 0x394, 2, bytes.fromhex("fcff0fff001ff000"))
    f_on2 = (20, 0x092, 0, bytes.fromhex("0000600000000000"))
    f_on3 = (21, 0x011, 1, bytes.fromhex("00800a1b00000000"))
    f_on4 = (5, 0x011, 1, bytes.fromhex("0000090000000000"))
    raw_on = protocol.build_can_script("RemoteOn", [f_on1, f_on2, f_on3, f_on4], repeat=1)
    dec_on = protocol.decode_can_script(raw_on)
    assert "Запрос максимального прогрева" in dec_on["frames"][0]["description"]
    assert "самостоятельный Remote On не подтверждён" in dec_on["frames"][1]["description"]
    assert "Heating ON" in dec_on["frames"][2]["description"]
    assert "максимального прогрева" in dec_on["summary"]
    assert "запуск" not in protocol.decode_can_script(protocol.build_can_script("RemoteOn", [f_on2]))["summary"]
    assert "Remote Off не подтверждён" in protocol.decode_can_script(protocol.build_can_script("RemoteOff", [f_on4]))["summary"]

    # Build roundtrip from dict preserves binary representation
    rebuilt = protocol.build_can_script_from_dict(dec_lock)
    assert rebuilt == raw_lock


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
