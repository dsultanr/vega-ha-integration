"""Full profile copy as a Vega Configurator .vsf file.

No test opens a socket or writes to a tracker.
"""
import asyncio
import importlib
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
import unittest.mock

from test_can_config import protocol, runtime_module, VegaRuntime, CommandError, SCENARIOS, SLOTS

backup = runtime_module.profile_backup


def answer(setting, raw):
    """Tracker reply to a read: status 1, then id, length and value."""
    return {"subcmd": 1, "data": struct.pack("<HB", setting, len(raw)) + raw}


class Tracker:
    """A session that answers reads like the tracker: value, refusal or silence."""

    def __init__(self):
        self.settings = {(3001, sub): bytes([sub]) * 35 for sub in range(4)}
        self.settings.update({(3002, sub): bytes(62) for sub in range(2)})
        self.settings.update({(3004, sid): b"\x01\x00\x00\x01" for sid in (2, 2805, 33952)})
        self.settings[3009, 0] = b"\x01"
        self.settings[3101, 7] = b"\x05"
        self.settings.update({(3255, sub): b"secret-pin".ljust(18, b"\0") for sub in range(5)})
        self.settings.update({(4001, sub): bytes([sub]) * 50 for sub in range(25)})
        self.settings[5073, 4] = b"\x00"
        self.settings[5499, 0] = b"ISpace_test".ljust(48, b"\0")
        self.settings.update({(5502, sub): bytes([sub]) * 82 for sub in range(8)})
        self.settings[5502, 9] = b"\xee" * 82  # past a gap: a dynamic list ends at the first miss
        self.settings[5504, 21] = b"device-id".ljust(17, b"\0")
        self.frames = []
        self.silent = {}
        self.broken = set()
        self.counter = 0
        self.authenticated = True
        self.last_seen = 0

    def get_next_req_id(self):
        self.counter += 1
        return self.counter

    async def _request(self, frame, req_id, timeout=6.0):
        self.frames.append(frame)
        cmd, payload = next(protocol.iter_frames(bytearray(frame)))
        got_id, subcmd, size, setting, sub = struct.unpack("<IBBHH", payload)
        assert (cmd, got_id, subcmd, size) == (protocol.CMD_COMMAND, req_id, 3, 4)
        if self.silent.get((setting, sub), 0) > 0:
            self.silent[setting, sub] -= 1
            raise asyncio.TimeoutError
        if (setting, sub) in self.broken:
            return answer(setting + 1, b"\x00")
        raw = self.settings.get((setting, sub))
        return answer(setting, raw) if raw is not None else {"subcmd": 0, "data": b""}

    async def query_sensor(self, setting, sub=0, timeout=6.0):
        raw = self.settings.get((setting, sub))
        return protocol.decode_sensor(setting, raw) if raw is not None else None

    async def set_setting(self, *args):
        raise AssertionError("a backup must not write")


class Relay:
    """The hub's side of the cloud relay, as RemoteSession sees it through its channel."""

    def __init__(self, tracker):
        self.tracker, self.session, self.busy, self.errors = tracker, None, 0, {}
        self.writer = None

    async def send(self, message):
        assert message["type"] == "request" and message["op"] == "read"
        key = (message["setting"], message["sub"])
        reply = {"type": "response", "id": message["id"]}
        if self.busy > 0:
            self.busy -= 1
            reply["error"] = "S4 command busy"
        elif key in self.errors:
            reply["error"] = self.errors[key]
        else:
            frame = protocol.build_query_sensor_frame(7, *key)
            try:
                answer = await self.tracker._request(frame, 7)
            except asyncio.TimeoutError:
                reply["error"] = ""  # str(asyncio.TimeoutError()) on the hub
            else:
                reply["payload"] = (struct.pack("<IBB", 7, answer["subcmd"], len(answer["data"])) + answer["data"]).hex()
        self.session.pending[message["id"]].set_result(reply)


class FormatTests(unittest.TestCase):
    def test_mac_matches_the_configurator(self):
        # Vector from the tool whose files Vega Configurator 1.28.46 opens.
        records = [(3001, 0, bytes(range(35))), (5499, 0, b"ISpace_test".ljust(48, b"\0"))]
        content = backup.build(records)
        self.assertEqual(content[:8].hex(), "ae1489c1a44e5ba0")
        self.assertEqual(content[8:24], b"SETTINGS\\" + bytes(7))
        self.assertEqual(backup.parse(content), records)

    def test_damaged_file_is_rejected(self):
        content = bytearray(backup.build([(3001, 0, bytes(35))]))
        content[-1] ^= 1
        with self.assertRaises(ValueError):
            backup.parse(bytes(content))
        with self.assertRaises(ValueError):
            backup.parse(backup.build([(3001, 0, bytes(35))])[:-3])

    def test_plan_covers_the_configurator_file(self):
        plan = {setting: (subs, dynamic) for setting, subs, dynamic in backup.plan(include_secrets=True)}
        self.assertEqual(len(backup.CATALOG), 120)
        self.assertEqual(len(plan), 123)
        self.assertEqual(len(plan[3004][0]), 729)
        self.assertTrue({31, 1520, 1669, 2984, 31700, 32249} <= set(plan[3004][0]))
        self.assertEqual(plan[3004][0][-1], 33952)
        self.assertIn(7, plan[3101][0])
        self.assertIn(4, plan[5073][0])
        self.assertEqual(plan[5504][0], (21,))
        self.assertTrue({3009, 3368, 3369, 5000} <= set(plan))
        self.assertTrue(plan[4001][1] and plan[5502][1] and not plan[3001][1])
        without = {setting for setting, _subs, _dynamic in backup.plan()}
        self.assertEqual(set(plan) - without, {3255, 5504})
        self.assertEqual([row[0] for row in backup.plan()], sorted(without))


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        async def executor(fn, *args):
            return fn(*args)

        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(path=lambda *p: str(Path(self.temp.name, *p))),
            async_add_executor_job=executor)
        self.runtime = VegaRuntime(hass, "test", 21443, "0000", "0000", 30, direct_control=False)
        self.tracker = Tracker()
        self.runtime._inject_session = self.tracker

    def files(self):
        return sorted(Path(self.temp.name, "vega", "profile_backups_test").glob("*"))


class BackupTests(Base):
    async def test_copy_holds_every_record_the_tracker_has(self):
        result = await self.runtime.async_backup_profile()
        saved = backup.parse(Path(result["path"]).read_bytes())
        expected = {key: raw for key, raw in self.tracker.settings.items()
                    if key[0] not in (3255, 5504) and key != (5502, 9)}
        self.assertEqual({(setting, sub): raw for setting, sub, raw in saved}, expected)
        self.assertEqual([r[:2] for r in saved], sorted(r[:2] for r in saved))
        self.assertEqual(result["records"], len(expected))
        self.assertEqual(result["profile"], "ISpace_test")
        self.assertFalse(result["includes_secrets"])
        self.assertEqual(result["bytes"], Path(result["path"]).stat().st_size)
        self.assertEqual([p.name for p in self.files()], [result["file"]])
        self.assertTrue(result["file"].startswith("vega-test-") and result["file"].endswith(".vsf"))
        self.assertEqual(os.stat(result["path"]).st_mode & 0o777, 0o600)
        self.assertNotIn(b"secret-pin", Path(result["path"]).read_bytes())

    async def test_works_without_direct_control_and_reads_only(self):
        self.assertFalse(self.runtime.direct_control)
        await self.runtime.async_backup_profile()
        for frame in self.tracker.frames:
            _cmd, payload = next(protocol.iter_frames(bytearray(frame)))
            self.assertEqual(payload[4], 3)

    async def test_secrets_only_on_request(self):
        result = await self.runtime.async_backup_profile(include_secrets=True)
        saved = {(setting, sub) for setting, sub, _raw in backup.parse(Path(result["path"]).read_bytes())}
        self.assertTrue({(3255, 0), (3255, 4), (5504, 21)} <= saved)
        self.assertTrue(result["includes_secrets"])

    async def test_one_lost_reply_is_asked_again(self):
        self.tracker.silent[4001, 3] = 1
        result = await self.runtime.async_backup_profile()
        saved = {(setting, sub) for setting, sub, _raw in backup.parse(Path(result["path"]).read_bytes())}
        self.assertIn((4001, 3), saved)

    async def test_silence_saves_nothing(self):
        self.tracker.silent[4001, 3] = 2
        with self.assertRaises(CommandError) as raised:
            await self.runtime.async_backup_profile()
        self.assertIn("4001/3", str(raised.exception))
        self.assertEqual(self.files(), [])

    async def test_wrong_reply_saves_nothing(self):
        self.tracker.broken.add((5499, 0))
        with self.assertRaises(CommandError):
            await self.runtime.async_backup_profile()
        self.assertEqual(self.files(), [])

    async def test_tracker_that_refuses_everything_saves_nothing(self):
        self.tracker.settings.clear()
        with self.assertRaises(CommandError):
            await self.runtime.async_backup_profile()
        self.assertEqual(self.files(), [])

    async def test_offline_saves_nothing(self):
        self.runtime._inject_session = None
        with self.assertRaises(CommandError):
            await self.runtime.async_backup_profile()
        self.assertEqual(self.files(), [])

    async def test_same_second_copies_do_not_overwrite(self):
        moment = runtime_module.time.gmtime(1_800_000_000)
        with unittest.mock.patch.object(runtime_module.time, "gmtime", return_value=moment):
            first = await self.runtime.async_backup_profile()
            self.tracker.settings[3001, 0] = b"\x07" * 35
            second = await self.runtime.async_backup_profile()
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(len(self.files()), 2)

    async def test_unchanged_profile_is_not_stored_twice(self):
        first = await self.runtime.async_backup_profile()
        second = await self.runtime.async_backup_profile()
        self.assertFalse(first["unchanged"])
        self.assertTrue(second["unchanged"])
        self.assertEqual(second["path"], first["path"])
        self.assertEqual(len(self.files()), 1)
        self.tracker.settings[5502, 4] = b"\x55" * 82
        third = await self.runtime.async_backup_profile()
        self.assertFalse(third["unchanged"])
        self.assertEqual(len(self.files()), 2)

    async def test_progress_is_published_and_ends_with_the_outcome(self):
        seen = []
        self.runtime.set_update_callback(lambda _data: seen.append(dict(self.runtime.profile_read)))
        result = await self.runtime.async_backup_profile()
        running = [state for state in seen if state.get("active")]
        percents = [state["percent"] for state in running]
        self.assertEqual(percents[0], 0)
        self.assertEqual(percents, sorted(set(percents)))
        self.assertTrue(8 <= len(percents) <= 21 and percents[-1] >= 90)
        self.assertEqual(seen[-1], {"active": False, "file": result["file"], "records": result["records"],
                                    "profile": "ISpace_test", "includes_secrets": False, "unchanged": False,
                                    "time": result["time"]})

    async def test_failure_is_left_in_the_status(self):
        self.tracker.silent[4001, 3] = 2
        with self.assertRaises(CommandError):
            await self.runtime.async_backup_profile()
        self.assertFalse(self.runtime.profile_read["active"])
        self.assertIn("4001/3", self.runtime.profile_read["error"])


class RelayTests(Base):
    """The tracker reached through the cloud relay, as on the live installation."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        package = runtime_module.__package__
        try:
            import cryptography  # noqa: F401
        except ImportError:
            # Only RemoteSession is used here; the encrypted channel is replaced by Relay.
            wire = types.ModuleType(package + ".relay_wire")
            wire.MAGIC, wire.handshake = b"VHR1\n", None
            sys.modules.setdefault(wire.__name__, wire)
        cloud_relay = importlib.import_module(package + ".cloud_relay")
        self.relay = Relay(self.tracker)
        self.session = cloud_relay.RemoteSession(self.relay, self.runtime)
        self.session.ready = True
        self.relay.session = self.session
        self.runtime._inject_session = self.session

    async def test_copy_through_the_relay(self):
        self.relay.busy = 3
        self.tracker.silent[4001, 3] = 1
        result = await self.runtime.async_backup_profile()
        saved = {(setting, sub): raw for setting, sub, raw in backup.parse(Path(result["path"]).read_bytes())}
        expected = {key: raw for key, raw in self.tracker.settings.items()
                    if key[0] not in (3255, 5504) and key != (5502, 9)}
        self.assertEqual(saved, expected)

    async def test_silence_through_the_relay_saves_nothing(self):
        self.tracker.silent[4001, 3] = 2
        with self.assertRaises(CommandError) as raised:
            await self.runtime.async_backup_profile()
        self.assertIn("4001/3", str(raised.exception))
        self.assertEqual(self.files(), [])

    async def test_lost_tracker_saves_nothing(self):
        self.relay.errors[3004, 2] = "S4 offline or unauthorized"
        with self.assertRaises(CommandError) as raised:
            await self.runtime.async_backup_profile()
        self.assertIn("S4 offline", str(raised.exception))
        self.assertEqual(self.files(), [])

    async def test_relay_without_tracker_saves_nothing(self):
        self.session.ready = False
        with self.assertRaises(CommandError):
            await self.runtime.async_backup_profile()
        self.assertEqual(self.files(), [])


class ReadButtonTests(Base):
    """«Прочитать из трекера» stays quick: commands, plus the CAN profile name and firmware."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tracker.settings.update({(5502, i): raw for i, raw in SLOTS.items()})
        self.tracker.settings.pop((5502, 9))
        self.tracker.settings.update({(4001, i): raw for i, raw in SCENARIOS.items()})
        self.tracker.settings[2, 0] = b"VEGA MT X CAN LTE v5 0.10b rc41.56 'ISp4WD'".ljust(50, b"\0")
        self.reads = []
        query = self.tracker.query_sensor

        async def counted(setting, sub=0, timeout=6.0):
            self.reads.append((setting, sub))
            return await query(setting, sub, timeout)

        self.tracker.query_sensor = counted

    async def test_read_adds_two_requests_and_no_profile_copy(self):
        await self.runtime.async_read_can_scripts()
        self.assertEqual(len(self.reads), 35)
        self.assertEqual(self.reads[-2:], [(5499, 0), (2, 0)])
        self.assertEqual(self.tracker.frames, [])
        self.assertEqual(self.files(), [])
        info = self.runtime.tracker_info
        self.assertEqual(info["can_profile"], "ISpace_test")
        self.assertEqual(info["firmware"], "VEGA MT X CAN LTE v5 0.10b rc41.56 'ISp4WD'")
        self.assertEqual(len(self.runtime.scenarios), 25)

    async def test_info_survives_a_restart(self):
        await self.runtime.async_read_can_scripts()
        again = VegaRuntime(self.runtime.hass, "test", 21443, "0000", "0000", 30)
        again.load_scripts()
        self.assertEqual(again.tracker_info, self.runtime.tracker_info)

    async def test_unreadable_info_does_not_break_the_read(self):
        del self.tracker.settings[5499, 0]
        del self.tracker.settings[2, 0]
        scripts = await self.runtime.async_read_can_scripts()
        self.assertEqual(len(scripts), 8)
        self.assertIsNotNone(self.runtime.scenario_read_at)
        self.assertEqual(self.runtime.tracker_info, {})


if __name__ == "__main__":
    import unittest.mock
    unittest.main()
