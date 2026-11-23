"""New-tracker onboarding and uncertain writes, without any vehicle connection."""
import asyncio
import json
from pathlib import Path
import struct
import tempfile
import types
import unittest
from unittest.mock import patch

from test_can_config import runtime_module, VegaRuntime, CommandError

profile = runtime_module.hv_profile
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/hv_profile.json').read_text())


def snapshot():
    data = {f'5500/{i}': bytes.fromhex(FIXTURE['empty']) for i in range(128)}
    data['5500/7'] = bytes.fromhex(FIXTURE['soc'])  # Deliberately not slot 3.
    for i, raw in enumerate(FIXTURE['isotp']):
        data[f'5503/{i}'] = bytes.fromhex(raw)
    for sid in profile.SENSOR_IDS:
        data[f'3004/{sid}'] = bytes(4)
    return data


class ProfileTests(unittest.TestCase):
    def test_encoder_matches_real_tracker_bytes(self):
        self.assertEqual(profile.empty_slot().hex(), FIXTURE['empty'])
        for sid in profile.SENSOR_IDS:
            self.assertEqual(profile.sensor_record(sid, 1).hex(), FIXTURE[str(sid)])

    def test_new_tracker_uses_free_slots_without_fixed_48_49(self):
        plan = profile.prepare(snapshot(), profile.PROFILE)
        self.assertEqual([s['slot'] for s in plan.sensors], [0, 1])
        self.assertEqual(list(plan.changes), ['5500/0', '5500/1', '3004/2922', '3004/2923'])

    def test_bus_comes_from_soc(self):
        data = snapshot()
        soc = bytearray(data['5500/7']); soc[1] = 2; data['5500/7'] = bytes(soc)
        plan = profile.prepare(data, profile.PROFILE)
        self.assertEqual(plan.bus, 2)
        self.assertEqual(plan.changes['5500/0'][1], 2)

    def test_recognizes_configured_profile_and_preserves_custom_names(self):
        data = snapshot()
        plan = profile.prepare(data, profile.PROFILE)
        data.update(plan.changes)
        raw = bytearray(data['5500/0']); raw[11:42] = b'My battery'.ljust(31, b'\0')
        data['5500/0'] = bytes(raw)
        self.assertFalse(profile.prepare(data, profile.PROFILE).changes)

    def test_partial_install_can_be_completed_without_rewriting_first_sensor(self):
        data = snapshot()
        data['5500/61'] = profile.sensor_record(2922, 1)
        plan = profile.prepare(data, profile.PROFILE)
        self.assertNotIn('5500/61', plan.changes)
        self.assertEqual([s['slot'] for s in plan.sensors], [61, 0])

    def test_occupied_can_id_is_never_repurposed(self):
        data = snapshot()
        raw = bytearray(data['5500/7']); struct.pack_into('<H', raw, 9, 2922)
        data['5500/33'] = bytes(raw)
        with self.assertRaisesRegex(ValueError, 'занят'):
            profile.prepare(data, profile.PROFILE)

    def test_duplicate_id_and_isotp_collision_are_rejected(self):
        for kind in ('duplicate', 'isotp'):
            data = snapshot()
            if kind == 'duplicate':
                data['5500/3'] = data['5500/4'] = profile.sensor_record(2923, 1)
            else:
                raw = bytearray(data['5503/5']); struct.pack_into('<H', raw, 18, 2923)
                data['5503/5'] = bytes(raw)
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'занят'):
                profile.prepare(data, profile.PROFILE)

    def test_unused_isotp_groups_are_accepted(self):
        # A factory tracker keeps groups 3..5 as 233 zero bytes.
        data = snapshot()
        expected = profile.prepare(snapshot(), profile.PROFILE).changes
        for group in (3, 4, 5):
            data[f'5503/{group}'] = bytes(233)
        self.assertEqual(profile.prepare(data, profile.PROFILE).changes, expected)
        raw = bytearray(233); raw[20] = 1
        data['5503/5'] = bytes(raw)
        with self.assertRaisesRegex(ValueError, 'ISO-TP'):
            profile.prepare(data, profile.PROFILE)

    def test_second_profile_builds_the_same_plan_and_keeps_its_name(self):
        first = profile.prepare(snapshot(), profile.PROFILE)
        second = profile.prepare(snapshot(), profile.PROFILE_2025)
        self.assertEqual(second.changes, first.changes)
        self.assertEqual(second.summary()['profile'], profile.PROFILE_2025)
        self.assertEqual(first.summary()['profile_name'], profile.PROFILE_NAME)
        with self.assertRaisesRegex(ValueError, 'профиль'):
            profile.prepare(snapshot(), 'other')
        # The climate form stays limited to the tested vehicle.
        with self.assertRaises(ValueError):
            runtime_module.climate_profile.check_firmware(profile.PROFILE_2025, runtime_module.climate_profile.VERIFIED_FIRMWARE)

    def test_full_tracker_is_rejected(self):
        data = snapshot()
        for i in range(128):
            if i != 7:
                raw = bytearray(data['5500/7']); struct.pack_into('<H', raw, 9, 3000 + i)
                data[f'5500/{i}'] = bytes(raw)
        with self.assertRaisesRegex(ValueError, 'свободных'):
            profile.prepare(data, profile.PROFILE)

    def test_missing_soc_unknown_format_bad_crc_and_incomplete_read_fail_closed(self):
        cases = []
        data = snapshot(); data['5500/7'] = profile.empty_slot(); cases.append(data)
        data = snapshot(); raw = bytearray(data['5503/0']); raw[0] = 3
        data['5503/0'] = bytes(raw); cases.append(data)
        data = snapshot(); raw = bytearray(data['5500/7']); raw[55] = 11
        data['5500/7'] = profile._crc(raw); cases.append(data)
        data = snapshot(); data['5500/22'] = bytes(84); cases.append(data)
        data = snapshot(); del data['5503/5']; cases.append(data)
        data = snapshot(); raw = bytearray(data['5500/7']); raw[0] = 10
        data['5500/7'] = bytes(raw); cases.append(data)
        for i, data in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(ValueError):
                profile.prepare(data, profile.PROFILE)


class FakeSession:
    authenticated = True

    def __init__(self):
        self.last_seen = runtime_module.time.time()
        self.authenticated_event = asyncio.Event(); self.authenticated_event.set()
        self.data = snapshot()
        self.writes = []
        self.reads = []
        self.fail_key = None
        self.lost_ack = False
        self.before_write = None
        self.after_read = None

    async def query_sensor(self, setting, sub):
        key = f'{setting}/{sub}'
        self.reads.append(key)
        data = self.data[key]
        if self.after_read:
            self.after_read(key)
        return {'id': setting, 'raw_hex': data.hex()}

    async def set_setting(self, setting, sub, raw):
        key = f'{setting}/{sub}'
        if self.before_write:
            self.before_write(key)
        self.writes.append(key)
        if self.fail_key == key:
            return {'success': False}
        self.data[key] = raw
        if self.lost_ack:
            raise OSError('Lost ACK')
        return {'success': True}


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        async def executor(fn, *args):
            return fn(*args)
        hass = types.SimpleNamespace(config=types.SimpleNamespace(
            path=lambda *p: str(Path(self.temp.name, *p))), async_add_executor_job=executor)
        self.rt = VegaRuntime(hass, '123456789012345', 21443, '0000', '0000', 30, direct_control=True)
        self.session = FakeSession(); self.rt._inject_session = self.session
        self.addAsyncCleanup(self.rt.stop)
        self.updates = []
        self.rt.set_update_callback(lambda _: self.updates.append(dict(self.rt.hv_setup)))

    async def prepare(self):
        return await self.rt.async_prepare_hv_battery(profile.PROFILE)

    def audit(self):
        path = Path(self.temp.name, 'vega', 'hv_battery_backups_123456789012345.jsonl')
        return [json.loads(line) for line in path.read_text().splitlines()]

    async def test_read_plan_is_read_only_and_reports_progress(self):
        plan = await self.prepare()
        self.assertEqual(len(self.session.reads), len(profile.READS))
        self.assertEqual(self.session.writes, [])
        self.assertEqual(plan['change_count'], 4)
        self.assertTrue(any(row.get('progress') == 136 for row in self.updates))
        self.assertFalse(Path(self.temp.name, 'vega').exists())

    async def test_new_tracker_backup_before_write_and_repeat_is_noop(self):
        result = await self.prepare()
        self.session.before_write = lambda _: self.assertEqual(len(self.audit()[0]['snapshot']), 136)
        result = await self.rt.async_configure_hv_battery(result['plan_id'])
        self.assertTrue(result['verified'])
        self.assertEqual(self.session.writes, result['written'])
        self.assertEqual([r['key'] for r in self.audit() if r['phase'] == 'verified'], result['written'])
        result = await self.prepare()
        self.assertTrue(result['configured'])
        count = len(self.session.writes)
        result = await self.rt.async_configure_hv_battery(result['plan_id'])
        self.assertTrue(result['unchanged'])
        self.assertEqual(len(self.session.writes), count)

    async def test_changed_unrelated_slot_blocks_all_writes(self):
        result = await self.prepare()
        self.session.data['5500/70'] = profile.sensor_record(2922, 1)
        with self.assertRaisesRegex(CommandError, 'изменились'):
            await self.rt.async_configure_hv_battery(result['plan_id'])
        self.assertEqual(self.session.writes, [])

    async def test_direct_control_off_allows_read_but_never_writes(self):
        self.rt.direct_control = False
        result = await self.prepare()
        with self.assertRaisesRegex(CommandError, 'Direct Control'):
            await self.rt.async_configure_hv_battery(result['plan_id'])
        self.assertEqual(self.session.writes, [])

    async def test_lost_ack_is_verified_without_repeating_write(self):
        result = await self.prepare(); self.session.lost_ack = True
        result = await self.rt.async_configure_hv_battery(result['plan_id'])
        self.assertTrue(result['verified'])
        self.assertEqual(len(self.session.writes), 4)

    async def test_partial_failure_stops_and_consumes_plan(self):
        result = await self.prepare(); self.session.fail_key = '5500/1'
        with self.assertRaisesRegex(CommandError, 'Подтверждённые записи: 5500/0'):
            await self.rt.async_configure_hv_battery(result['plan_id'])
        self.assertEqual(self.session.writes, ['5500/0', '5500/1'])
        self.assertEqual(self.rt.hv_setup['uncertain'], '5500/1')
        with self.assertRaisesRegex(CommandError, 'устарел'):
            await self.rt.async_configure_hv_battery(result['plan_id'])
        self.session.fail_key = None
        retry = await self.prepare()
        self.assertEqual(retry['change_count'], 3)

    async def test_backup_failure_prevents_write(self):
        result = await self.prepare()
        with patch.object(self.rt, '_hv_audit', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(CommandError, 'disk full'):
                await self.rt.async_configure_hv_battery(result['plan_id'])
        self.assertEqual(self.session.writes, [])

    async def test_disabling_direct_control_after_backup_prevents_write(self):
        result = await self.prepare()
        audit = self.rt._hv_audit
        async def backup(record):
            await audit(record)
            self.rt.direct_control = False
        with patch.object(self.rt, '_hv_audit', side_effect=backup):
            with self.assertRaisesRegex(CommandError, 'Direct Control'):
                await self.rt.async_configure_hv_battery(result['plan_id'])
        self.assertEqual(self.session.writes, [])

    async def test_expired_forged_and_other_device_plans_cannot_write(self):
        for kind in ('expired', 'forged', 'other_device'):
            result = await self.prepare()
            if kind == 'expired': self.rt._hv_plan['expires'] = 0
            if kind == 'forged': result['plan_id'] = 'bad'
            if kind == 'other_device': self.rt.imei = '234567890123456'
            with self.subTest(kind=kind), self.assertRaisesRegex(CommandError, 'устарел'):
                await self.rt.async_configure_hv_battery(result['plan_id'])
            self.rt.imei = '123456789012345'
        self.assertEqual(self.session.writes, [])

    async def test_offline_and_unauthenticated_connections_cannot_scan(self):
        self.session.authenticated_event.clear()
        with self.assertRaisesRegex(CommandError, 'авторизации'):
            await self.prepare()
        self.rt._inject_session = None
        with self.assertRaisesRegex(CommandError, 'Vega Binary'):
            await self.prepare()
        self.assertEqual(self.session.reads, [])

    async def test_scan_failure_and_unknown_profile_never_write(self):
        with self.assertRaisesRegex(CommandError, 'профиль'):
            await self.rt.async_prepare_hv_battery('unknown')
        self.assertEqual(self.session.reads, [])
        self.session.data['5503/1'] = b''
        with self.assertRaisesRegex(CommandError, 'байт'):
            await self.prepare()
        self.assertIsNone(self.rt._hv_plan)
        self.assertEqual(self.session.writes, [])

    async def test_reconnection_during_scan_invalidates_entire_plan(self):
        def reconnect(key):
            if key == '5500/40':
                self.rt._inject_session = FakeSession()
        self.session.after_read = reconnect
        with self.assertRaisesRegex(CommandError, 'Соединение'):
            await self.prepare()
        self.assertIsNone(self.rt._hv_plan)
        self.assertEqual(self.session.writes, [])

    async def test_slot_change_after_backup_is_detected_before_write(self):
        result = await self.prepare()
        audit = self.rt._hv_audit
        async def backup(record):
            await audit(record)
            if record['phase'] == 'backup':
                self.session.data['5500/0'] = profile.sensor_record(2923, 1)
        with patch.object(self.rt, '_hv_audit', side_effect=backup):
            with self.assertRaisesRegex(CommandError, 'перед записью'):
                await self.rt.async_configure_hv_battery(result['plan_id'])
        self.assertEqual(self.session.writes, [])

    async def test_cancellation_after_sending_marks_uncertain_and_does_not_retry(self):
        result = await self.prepare()
        async def interrupted(setting, sub, raw):
            self.session.writes.append(f'{setting}/{sub}')
            raise asyncio.CancelledError()
        with patch.object(self.session, 'set_setting', side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await self.rt.async_configure_hv_battery(result['plan_id'])
        self.assertEqual(self.session.writes, ['5500/0'])
        self.assertEqual(self.rt.hv_setup['uncertain'], '5500/0')
        self.assertIsNone(self.rt._hv_plan)


if __name__ == '__main__':
    unittest.main(verbosity=2)
