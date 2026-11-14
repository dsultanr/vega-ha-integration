import asyncio
import importlib
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import AsyncMock

pkg = types.ModuleType('pull_tests_vega')
pkg.__path__ = [str(Path(__file__).resolve().parents[1] / 'custom_components/vega')]
sys.modules[pkg.__name__] = pkg
pull = importlib.import_module(pkg.__name__ + '.sensor_pull')

class PullTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_selected_ids_serially_without_write_or_cache(self):
        active = 0
        async def query(sid, sub, timeout):
            nonlocal active
            self.assertEqual(active, 0)
            active += 1
            await asyncio.sleep(0)
            active -= 1
            return {'id': sid, 'raw_hex': (struct.pack('<f', 22) if sid == 2906 else b'\x03').hex()}
        session = types.SimpleNamespace(query_sensor=AsyncMock(side_effect=query), set_setting=AsyncMock())
        report = await pull.read_sensors(session, [2906, 2912, 2912])
        self.assertTrue(report['complete'])
        self.assertEqual([s['value'] for s in report['samples']], [22, 3])
        self.assertFalse(report['can_sample_time_known'])
        for sample in report['samples']:
            self.assertGreaterEqual(sample['received_at'], sample['requested_at'])
        session.set_setting.assert_not_called()

    async def test_timeout_wrong_id_empty_and_bad_float_do_not_become_success(self):
        session = types.SimpleNamespace(query_sensor=AsyncMock(side_effect=[
            asyncio.TimeoutError(), {'id': 2913, 'raw_hex': '03'},
            {'id': 2913, 'raw_hex': ''}, {'id': 2906, 'raw_hex': '0000c07f'}]))
        report = await pull.read_sensors(session, [2918, 2912, 2913, 2906])
        self.assertFalse(report['complete'])
        self.assertEqual([s['error'] for s in report['samples']],
                         ['timeout', 'unexpected_sensor', 'ValueError', 'ValueError'])
        self.assertTrue(all('value' not in s for s in report['samples']))

    async def test_climate_timeout_is_not_zero_or_success(self):
        session = types.SimpleNamespace(query_sensor=AsyncMock(side_effect=[
            {'id': 2924, 'raw_hex': 'ff00000000000000'},
            {'id': 2925, 'raw_hex': '0100000000000000'}]))
        report = await pull.read_sensors(session, [2924, 2925])
        self.assertFalse(report['complete'])
        self.assertEqual(report['samples'][0]['error'], 'sensor_unavailable')
        self.assertNotIn('value', report['samples'][0])
        self.assertEqual(report['samples'][1]['value'], 1)

    async def test_configuration_ids_and_invalid_batches_rejected_before_io(self):
        session = types.SimpleNamespace(query_sensor=AsyncMock())
        for ids in ([], [5502], [1347], [True], [2912]*17):
            with self.assertRaises(ValueError):
                await pull.read_sensors(session, ids)
        session.query_sensor.assert_not_called()

    async def test_cancellation_propagates(self):
        session = types.SimpleNamespace(query_sensor=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await pull.read_sensors(session, [2912])

    async def test_error_text_is_not_exposed(self):
        session = types.SimpleNamespace(query_sensor=AsyncMock(side_effect=RuntimeError('private detail')))
        report = await pull.read_sensors(session, [2912])
        self.assertNotIn('private detail', str(report))

if __name__ == '__main__': unittest.main()
