"""Full command snapshots, restore and the climate-form vehicle gate.

No test opens a socket or writes to a tracker.
"""
import json
from pathlib import Path
import tempfile
import types
import unittest
import unittest.mock

from test_can_config import protocol, runtime_module, VegaRuntime, CommandError, FakeSession, SCENARIOS, SLOTS

climate = runtime_module.climate_profile
FIXTURES = Path(__file__).parent / "fixtures"
HV = json.loads((FIXTURES / "hv_profile.json").read_text())
CLIMATE = json.loads((FIXTURES / "climate_profile.json").read_text())


FW = climate.VERIFIED_FIRMWARE


def fw(text=FW):
    return text.encode("latin1").ljust(50, b"\0")


def decoders():
    data = {f"5500/{i}": bytes.fromhex(HV["empty"]) for i in range(128)}
    data["5500/7"] = bytes.fromhex(HV["soc"])
    data["5500/43"] = bytes.fromhex(CLIMATE["wheel"])
    return data


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        async def executor(fn, *args):
            return fn(*args)

        self.hass = types.SimpleNamespace(
            config=types.SimpleNamespace(path=lambda *p: str(Path(self.temp.name, *p))),
            async_add_executor_job=executor)
        self.runtime = VegaRuntime(self.hass, "test", 21443, "0000", "0000", 30, direct_control=True)
        self.session = FakeSession()
        self.runtime._inject_session = self.session

    def folder(self):
        return Path(self.temp.name, "vega", "command_snapshots_test")


class BackupTests(Base):
    async def test_backup_reads_everything_and_writes_nothing(self):
        result = await self.runtime.async_backup_commands("  до  правок ")
        self.assertEqual(self.session.writes, [])
        self.assertEqual(len(self.session.reads), 33)
        saved = json.loads((self.folder() / f"{result['backup_id']}.json").read_text())
        self.assertEqual(saved["note"], "до правок")
        self.assertEqual(saved["imei"], "test")
        self.assertEqual(saved["records"]["5502/4"], SLOTS[4].hex())
        self.assertEqual(saved["records"]["4001/3"], SCENARIOS[3].hex())
        self.assertEqual(len(saved["records"]), 33)
        self.assertIsNotNone(self.runtime.scenario_read_at)
        listed = (await self.runtime.async_list_command_backups())["backups"]
        self.assertEqual([b["backup_id"] for b in listed], [result["backup_id"]])
        self.assertTrue(listed[0]["this_tracker"])

    async def test_same_second_backups_do_not_overwrite(self):
        first = await self.runtime.async_backup_commands()
        with unittest.mock.patch.object(runtime_module.time, "gmtime",
                                        return_value=runtime_module.time.strptime(first["backup_id"], "%Y%m%dT%H%M%SZ")):
            second = await self.runtime.async_backup_commands()
        self.assertNotEqual(first["backup_id"], second["backup_id"])
        self.assertEqual(len(list(self.folder().glob("*.json"))), 2)

    async def test_incomplete_read_saves_no_snapshot(self):
        del self.session.settings[4001, 24]
        with self.assertRaises(CommandError):
            await self.runtime.async_backup_commands()
        self.assertFalse(self.folder().exists())

    async def test_restore_writes_only_differences_scripts_first_and_keeps_safety_copy(self):
        backup = await self.runtime.async_backup_commands()
        edited_slot = bytearray(SLOTS[4]); edited_slot[0] ^= 1
        edited_scenario = bytearray(SCENARIOS[3]); edited_scenario[42] ^= 1
        self.session.settings[5502, 4] = bytes(edited_slot)
        self.session.settings[4001, 3] = bytes(edited_scenario)
        result = await self.runtime.async_restore_commands(backup["backup_id"])
        self.assertEqual(result["written"], ["5502/4", "4001/3"])
        self.assertEqual([(sid, sub) for sid, sub, _ in self.session.writes], [(5502, 4), (4001, 3)])
        self.assertEqual(self.session.settings[5502, 4], SLOTS[4])
        self.assertEqual(self.session.settings[4001, 3], SCENARIOS[3])
        safety = json.loads((self.folder() / f"{result['safety_backup_id']}.json").read_text())
        self.assertEqual(safety["records"]["5502/4"], bytes(edited_slot).hex())
        self.assertIn(backup["backup_id"], safety["note"])

    async def test_restore_of_identical_state_is_read_only(self):
        backup = await self.runtime.async_backup_commands()
        result = await self.runtime.async_restore_commands(backup["backup_id"])
        self.assertTrue(result["unchanged"])
        self.assertEqual(self.session.writes, [])
        self.assertEqual(len(list(self.folder().glob("*.json"))), 1)

    async def test_restore_requires_direct_control(self):
        backup = await self.runtime.async_backup_commands()
        self.session.settings[5502, 4] = bytes(82)
        self.runtime.direct_control = False
        with self.assertRaisesRegex(CommandError, "Direct Control"):
            await self.runtime.async_restore_commands(backup["backup_id"])
        self.assertEqual(self.session.writes, [])

    async def test_unconfirmed_write_stops_remaining_records(self):
        backup = await self.runtime.async_backup_commands()
        self.session.settings[5502, 4] = bytes(82)
        self.session.settings[4001, 3] = bytes(50)
        self.session.mode = "mismatch"
        with self.assertRaisesRegex(CommandError, "5502/4: запись не подтверждена"):
            await self.runtime.async_restore_commands(backup["backup_id"])
        self.assertEqual(len(self.session.writes), 1)
        self.assertIsNone(self.runtime.scenario_read_at)

    async def test_bad_ids_foreign_and_damaged_copies_are_rejected_before_any_io(self):
        backup = await self.runtime.async_backup_commands()
        path = self.folder() / f"{backup['backup_id']}.json"
        good = json.loads(path.read_text())
        self.session.reads.clear()
        for backup_id in ["../secrets", "20261116T000000Z/..", "", "20261116T000000Z"]:
            with self.subTest(backup_id=backup_id), self.assertRaises(CommandError):
                await self.runtime.async_restore_commands(backup_id)
        for mutate in [lambda d: d.update(imei="other"), lambda d: d.update(version=2),
                       lambda d: d["records"].pop("4001/0"),
                       lambda d: d["records"].update({"5502/0": "00"}),
                       lambda d: d["records"].update({"5502/9": "00" * 82})]:
            document = json.loads(json.dumps(good)); mutate(document)
            path.write_text(json.dumps(document))
            with self.assertRaises(CommandError):
                await self.runtime.async_restore_commands(backup["backup_id"])
        self.assertEqual(self.session.reads, [])
        self.assertEqual(self.session.writes, [])


class ClimateGateTests(Base):
    async def test_other_or_unreadable_tracker_firmware_keeps_form_locked_without_scanning(self):
        self.session.settings.update({(5500, int(k.split("/")[1])): v for k, v in decoders().items()})
        for raw, message in [(fw("VEGA MT X CAN LTE v5 0.10b rc43"), "не проверена"), (None, "Не удалось прочитать")]:
            self.session.reads.clear()
            if raw is None:
                self.session.settings.pop((2, 0), None)
            else:
                self.session.settings[2, 0] = raw
            with self.subTest(message=message), self.assertRaisesRegex(CommandError, message):
                await self.runtime.async_check_climate_profile(climate.PROFILE)
            self.assertEqual(self.session.reads, [(2, 0)])
            self.assertFalse(self.runtime.climate_profile["verified"])

    def test_matrix_must_match_tested_vehicle(self):
        self.assertEqual(climate.verify_vehicle(decoders(), climate.PROFILE, FW)["profile"], climate.PROFILE)
        with self.assertRaisesRegex(ValueError, "только на профиле"):
            climate.verify_vehicle(decoders(), "f515_2wd", FW)
        for key in ("5500/7", "5500/43"):
            data = decoders(); data[key] = data["5500/0"]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                climate.verify_vehicle(data, climate.PROFILE, FW)
        data = decoders(); wheel = bytearray(data["5500/43"]); wheel[42] ^= 1
        data["5500/43"] = bytes(wheel)
        with self.assertRaises(ValueError):
            climate.verify_vehicle(data, climate.PROFILE, FW)
        for firmware, message in [(None, "Не удалось прочитать"), ("", "Не удалось прочитать"),
                                  ("VEGA MT X CAN LTE v5 0.10b rc41.49", "не проверена")]:
            with self.subTest(firmware=firmware), self.assertRaisesRegex(ValueError, message):
                climate.verify_vehicle(decoders(), climate.PROFILE, firmware)
        data = decoders(); del data["5500/127"]
        with self.assertRaisesRegex(ValueError, "неполный"):
            climate.verify_vehicle(data, climate.PROFILE, FW)

    async def test_form_is_locked_until_check_and_survives_restart(self):
        self.assertFalse(self.runtime.climate_profile["verified"])
        self.assertIn("4WD", self.runtime.climate_profile["verified_on"])
        self.session.settings.update({(5500, int(k.split("/")[1])): v for k, v in decoders().items()})
        self.session.settings[2, 0] = fw()
        result = await self.runtime.async_check_climate_profile(climate.PROFILE)
        self.assertTrue(result["verified"])
        self.assertEqual(self.session.writes, [])
        again = VegaRuntime(self.hass, "test", 21443, "0000", "0000", 30, direct_control=True)
        again.load_scripts()
        self.assertTrue(again.climate_profile["verified"])
        other = VegaRuntime(self.hass, "other", 21443, "0000", "0000", 30, direct_control=True)
        other.load_scripts()
        self.assertFalse(other.climate_profile["verified"])

    async def test_failed_recheck_locks_a_previously_unlocked_form(self):
        self.session.settings.update({(5500, int(k.split("/")[1])): v for k, v in decoders().items()})
        self.session.settings[2, 0] = fw()
        await self.runtime.async_check_climate_profile(climate.PROFILE)
        self.session.settings[5500, 43] = bytes.fromhex(HV["empty"])
        with self.assertRaisesRegex(CommandError, "обогрева руля"):
            await self.runtime.async_check_climate_profile(climate.PROFILE)
        self.assertFalse(self.runtime.climate_profile["verified"])
        again = VegaRuntime(self.hass, "test", 21443, "0000", "0000", 30, direct_control=True)
        again.load_scripts()
        self.assertFalse(again.climate_profile["verified"])
        self.assertEqual(self.session.writes, [])


if __name__ == "__main__":
    unittest.main()
