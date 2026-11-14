"""5502 field semantics from native Configurator disassembly and captured bytes.

The expected rows are independent of this codec: Configurator 1.28.46
encode 0x1401b7440 / decode 0x1401b8aa0, compared with all eight tracker slots.
"""
import copy
import json
from pathlib import Path
import struct
import unittest
import test_can_config as config
from test_can_config import protocol, SLOTS, document

FIXTURES = Path(__file__).parent / 'fixtures'
EXPECTED = json.loads((FIXTURES / 'can_slots_native_decoded.json').read_text())
NATIVE_SLOTS = {int(k): bytes.fromhex(v) for k, v in json.loads((FIXTURES / 'can_slots_20261114.json').read_text()).items()}


class NativeCodecTests(unittest.TestCase):
    def test_every_native_row_has_correct_bus_delay_count_and_dlc(self):
        for script in EXPECTED:
            decoded = protocol.decode_can_script(NATIVE_SLOTS[script['slot']])
            self.assertEqual(decoded['frame_type'], script['frame_type'])
            self.assertEqual(decoded['chain_next'], script['chain_next'])
            for i, row in enumerate(script['rows'][:len(decoded['frames'])]):
                actual = decoded['frames'][i]
                with self.subTest(slot=script['slot'], row=i):
                    for key in ('delay_ms', 'count', 'dlc'):
                        self.assertEqual(actual[key], row[key])
                    self.assertEqual(actual['bus'], row['can_label'])
                    self.assertEqual(actual['can_id'], int(row['can_id'], 16))
                    self.assertEqual(actual['data'].upper(), row['data'])
            self.assertEqual(protocol.build_can_script_from_dict(decoded), NATIVE_SLOTS[script['slot']])

    def test_preheat_zero_count_row_is_not_dropped_or_misread_as_bus_zero(self):
        row = protocol.decode_can_script(SLOTS[4])['frames'][1]
        self.assertEqual((row['bus'], row['count'], row['delay_ms'], row['dlc']), (1, 0, 50, 8))
        self.assertEqual(row['can_id'], 0x92)

    def test_profile_matches_independently_prepared_wire_bytes(self):
        profile = json.loads((Path(__file__).parents[1] / 'tools/preheat_profile.json').read_text())
        raw = protocol.build_can_script_from_dict(profile['script'])
        self.assertEqual(raw.hex(), profile['candidate_hex'])
        self.assertEqual(raw[24:32].hex(), '00580a0c00000000')
        self.assertEqual(struct.unpack_from('<H', raw, 32)[0], 0x8fa1)
        self.assertEqual(raw[23], 2)  # BCM activation repeat for wake
        self.assertEqual(raw[38], 1)  # CAN2 neutral count
        self.assertEqual(raw[53], 2)  # CAN1 HVAC count
        self.assertEqual(raw[68], 3)  # CAN1 rear/wheel count
        self.assertEqual(raw[54:62].hex(), 'fcff2fff201f5000')

    def test_explicit_bounds_extended_chain_and_short_dlc_roundtrip(self):
        spec = document(4)
        spec.update(frame_type=2, chain_next=True, repeat=255)
        spec['frames'] = [dict(delay_ms=10230, can_id=0x1fffffff, bus=3,
                               count=255, dlc=0, data='0123456789abcdef')]
        raw = protocol.build_can_script_from_dict(spec)
        self.assertEqual(raw[16], 0xc0)
        self.assertEqual(raw[17:24], bytes.fromhex('fe0fffffff1fff'))
        decoded = protocol.decode_can_script(raw)
        self.assertEqual(decoded['frames'][0]['data'], '0123456789abcdef')
        self.assertEqual(decoded['frames'][0]['dlc'], 0)
        self.assertEqual(protocol.build_can_script_from_dict(decoded), raw)

    def test_interior_padding_does_not_shift_later_rows(self):
        raw = bytearray(SLOTS[4])
        raw[32:47] = SLOTS[0][47:62]  # canonical empty row inside program
        decoded = protocol.decode_can_script(bytes(raw))
        self.assertEqual(len(decoded['frames']), 4)
        self.assertEqual(decoded['frames'][1]['count'], 0)
        self.assertEqual(protocol.build_can_script_from_dict(decoded), raw)

    def test_legacy_or_incomplete_documents_cannot_be_misinterpreted(self):
        for field in ('format_version', 'frame_type', 'chain_next'):
            spec = document(); del spec[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                protocol.build_can_script_from_dict(spec)
        for field in ('count', 'dlc'):
            spec = document(); del spec['frames'][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                protocol.build_can_script_from_dict(spec)
        for field, value in (('format_version', True), ('frame_type', 0),
                             ('frame_type', 3), ('chain_next', 1)):
            spec = document(); spec[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                protocol.build_can_script_from_dict(spec)


class NativeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = config.RuntimeTests.asyncSetUp

    async def test_legacy_editor_rejected_before_any_transport_call(self):
        spec = copy.deepcopy(self.spec); del spec['format_version']
        with self.assertRaisesRegex(Exception, 'format_version=2'):
            await self.runtime.async_write_can_script(5, spec)
        self.assertEqual(self.session.reads, [])
        self.assertEqual(self.session.writes, [])

    async def test_cache_redecoded_from_wire_and_requires_live_read(self):
        path = Path(self.runtime._scripts_path()); path.parent.mkdir(parents=True)
        path.write_text(json.dumps([dict(slot=4, raw=SLOTS[4].hex(), name='old',
            editable=True, frames=[dict(bus=0, delay_ms=2000, can_id=0x394, data='')])]))
        self.runtime.load_scripts()
        slot = self.runtime.can_scripts[0]
        self.assertFalse(slot['editable'])
        self.assertEqual(slot['format_version'], 2)
        self.assertEqual(slot['frames'][0]['delay_ms'], 5000)
        self.assertEqual(slot['frames'][0]['bus'], 1)
        await self.runtime.async_read_can_scripts()
        self.assertTrue(all(s['editable'] for s in self.runtime.can_scripts))


if __name__ == '__main__': unittest.main()
