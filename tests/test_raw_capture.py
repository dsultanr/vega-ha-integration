"""Offline durability and relay-order checks; no connection to a vehicle."""
import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('raw_capture', Path(__file__).resolve().parents[1] / 'custom_components/vega/raw_capture.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class RawCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        async def executor(fn, *args):
            return await asyncio.to_thread(fn, *args)
        self.hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda name: str(Path(self.tmp.name)/name)), async_add_executor_job=executor)
        self.cap = m.RawCapture(self.hass)

    def records(self):
        return [json.loads(line) for p in sorted(self.cap.directory.glob('*.jsonl')) for line in p.read_text().splitlines()]

    async def test_arbitrary_fragmented_large_data_and_directions(self):
        chunks = [('t2c',b'initial'), ('c2t',bytes(range(256))*30), ('t2c',b'\xff\0bad-crc')]
        with patch.object(m.os, 'fsync', wraps=os.fsync) as sync:
            for direction, data in chunks:
                await self.cap.feed(direction,data)
            self.assertGreaterEqual(sync.call_count,4)
        records=self.records()
        self.assertEqual([(r['direction'],base64.b64decode(r['base64'])) for r in records],chunks)
        self.assertEqual([r['seq'] for r in records],[1,2,3])
        self.assertEqual(next(self.cap.directory.glob('*.jsonl')).stat().st_mode & 0o777,0o600)

    async def test_rotation_and_sessions_never_overwrite(self):
        with patch.object(m,'SEGMENT_BYTES',1):
            await self.cap.feed('c2t',b'one')
            await self.cap.feed('c2t',b'two')
        other=m.RawCapture(self.hass)
        await other.feed('c2t',b'three')
        self.assertEqual(len(list(self.cap.directory.glob('*.jsonl'))),3)
        self.assertEqual({base64.b64decode(r['base64']) for r in self.records()},{b'one',b'two',b'three'})

    async def test_failure_reported_and_subsequent_writes_continue(self):
        errors=[]
        self.cap._error=lambda error:errors.append(str(error))
        with patch.object(m.os,'write',side_effect=OSError('disk full')):
            await self.cap.feed('c2t',b'lost')
        await self.cap.feed('c2t',b'next')
        self.assertEqual(errors,['disk full'])
        self.assertEqual(base64.b64decode(self.records()[0]['base64']),b'next')

    async def test_partial_write_retried(self):
        write=os.write
        with patch.object(m.os,'write',side_effect=lambda fd,data:write(fd,data[:7])):
            await self.cap.feed('c2t',b'preserve every byte')
        self.assertEqual(base64.b64decode(self.records()[0]['base64']),b'preserve every byte')

if __name__=='__main__':unittest.main()
