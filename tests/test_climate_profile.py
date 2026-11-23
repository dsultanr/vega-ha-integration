"""Collision protection and decoder extraction for permanent climate telemetry."""
import importlib
import json
from pathlib import Path
import struct
import sys
import types
import unittest

pkg = types.ModuleType('climate_tests_vega')
pkg.__path__ = [str(Path(__file__).resolve().parents[1] / 'custom_components/vega')]
sys.modules[pkg.__name__] = pkg
profile = importlib.import_module(pkg.__name__ + '.climate_profile')
protocol = importlib.import_module(pkg.__name__ + '.protocol')
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/climate_profile.json').read_text())


def snapshot():
    data = {f'5500/{i}': bytes.fromhex(FIXTURE['empty']) for i in range(128)}
    data['5500/43'] = bytes.fromhex(FIXTURE['wheel'])
    data.update({f'5503/{i}': bytes.fromhex(r) for i, r in enumerate(FIXTURE['isotp'])})
    data.update({f'3004/{sid}': bytes(4) for sid in profile.SENSOR_IDS})
    return data


class ClimateProfileTests(unittest.TestCase):
    def test_free_slots_idempotency_and_partial_install(self):
        data = snapshot()
        changes = profile.prepare(data)
        self.assertEqual(list(changes), ['5500/0', '5500/1', '3004/2924', '3004/2925'])
        data['5500/0'] = changes['5500/0']
        self.assertNotIn('5500/0', profile.prepare(data))
        data.update(changes)
        self.assertEqual(profile.prepare(data), {})

    def test_unused_isotp_groups_are_accepted(self):
        data = snapshot()
        expected = profile.prepare(snapshot())
        for group in (3, 4, 5):
            data[f'5503/{group}'] = bytes(233)
        self.assertEqual(profile.prepare(data), expected)
        raw = bytearray(233); raw[20] = 1
        data['5503/5'] = bytes(raw)
        with self.assertRaisesRegex(ValueError, 'ISO-TP'):
            profile.prepare(data)

    def test_can_and_isotp_collisions(self):
        for mode in ('can', 'duplicate', 'isotp'):
            data = snapshot()
            if mode == 'isotp':
                raw = bytearray(data['5503/0'])
                struct.pack_into('<H', raw, 18, 2924)
                data['5503/0'] = bytes(raw)
            elif mode == 'duplicate':
                data['5500/0'] = data['5500/1'] = profile.sensor_record(2924, 1)
            else:
                raw = bytearray(data['5500/43'])
                struct.pack_into('<H', raw, 9, 2924)
                data['5500/0'] = bytes(raw)
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'occupied'):
                profile.prepare(data)

    def test_invalid_snapshot_and_full_matrix(self):
        for mode in ('short', 'crc', 'anchor', 'full'):
            data = snapshot()
            if mode == 'short':
                del data['5500/127']
            elif mode == 'crc':
                data['5500/4'] = bytes(84)
            elif mode == 'anchor':
                data['5500/43'] = data['5500/0']
            else:
                for i in range(128):
                    if i != 43:
                        raw = bytearray(data['5500/43'])
                        struct.pack_into('<H', raw, 9, 5000+i)
                        data[f'5500/{i}'] = bytes(raw)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                profile.prepare(data)

    def test_bus_inferred_and_signal_bits_do_not_include_neighbors(self):
        data = snapshot()
        raw = bytearray(data['5500/43']); raw[1] = 2; data['5500/43'] = bytes(raw)
        self.assertEqual(profile.prepare(data)['5500/0'][1], 2)
        # Captured face/feet status bytes include fan=8 in the low nibble.
        for sid, byte, bit, samples in ((2924, 3, 0, [(0xA0, 0), (0xA3, 3), (0xA2, 2)]),
                                       (2925, 4, 4, [(0x18, 1), (0x38, 3)])):
            record = profile.sensor_record(sid, 1)
            self.assertEqual((record[56] >> 2) & 7, byte)
            self.assertEqual(record[56] >> 5, bit)
            self.assertEqual(record[55], 3)
            self.assertEqual(record[46:55], bytes.fromhex('ff000000000000000f'))
            for value, expected in samples:
                self.assertEqual((value >> bit) & ((1 << record[55])-1), expected)

    def test_timeout_and_invalid_seat_codes_are_unavailable(self):
        for sid in profile.SENSOR_IDS:
            self.assertIsNone(protocol.decode_sensor(sid, b'\xff'+bytes(7))['value'])
            self.assertIsNone(protocol.map_enum(sid, 255))  # Combine path
            self.assertEqual(protocol.decode_sensor(sid, b'\x03'+bytes(7))['value'], 3)
        self.assertIsNone(protocol.decode_sensor(2924, b'\x07')['value'])


if __name__ == '__main__': unittest.main()
