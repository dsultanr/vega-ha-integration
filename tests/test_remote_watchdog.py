"""The Remote watchdog clears the output this tracker's scenarios hold, or nothing.

Run with: python3 -B tests/test_remote_watchdog.py
"""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "vega_remote_watchdog_tests"
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT / "custom_components" / "vega")]
sys.modules[PACKAGE] = pkg
try:
    import homeassistant.core  # noqa: F401
except ImportError:
    for name in ("homeassistant", "homeassistant.core", "homeassistant.exceptions"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["homeassistant.core"].HomeAssistant = object
    sys.modules["homeassistant.exceptions"].HomeAssistantError = type(
        "HomeAssistantError", (Exception,), {})
    const = types.ModuleType(PACKAGE + ".const")
    const.DEFAULT_IMEI = "000000000000000"
    const.ONLINE_TIMEOUT = 180
    const.SENSOR_KEY_MAP = {}
    const.DOWNLINK_EVENT = "vega_downlink"
    sys.modules[const.__name__] = const

protocol = importlib.import_module(PACKAGE + ".protocol")
runtime_module = importlib.import_module(PACKAGE + ".runtime")

# ISpace_4wd as read from a tracker; preheat (1347) and cooling (1346) switch output 1 on, no timer.
_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "scenarios.json").read_text())
RAW_4WD = {int(i): bytes.fromhex(raw) for i, raw in _FIXTURE.items()}
# Factory i-Space 2025 scenarios (ISpace_09072025, ISpace_11092025 and ISpace_2WD_17072026 are
# identical): commands pulse outputs on timers, output 1 follows the 12 V voltage.
RAW_2025 = {i: bytes(50) for i in range(25)}
RAW_2025.update({i: bytes.fromhex(h) for i, h in {
    0: "e40433333b4100013e050000000001000000000000000000000000000000000000000000000000000000010f000000000000",
    1: "3f05000000000100000000000000000000000000000000000000000000000000000000000000000000000500000000000000",
    2: "3f050000803f0000000000000000000000000000000000000000000000000000000000000000000000000000050000000000",
    3: "4205000000000100000000000000000000000000000000000000000000000000000000000000000000000303000000000000",
    4: "4205000000000100000000000000000000000000000000000000000000000000000000000000000000000601000000000004",
    5: "4305000000000100000000000000000000000000000000000000000000000000000000000000000000000303000000000000",
    6: "4305000000000100000000000000000000000000000000000000000000000000000000000000000000000701000000000008",
    7: "4405000000000100000000000000000000000000000000000000000000000000000000000000000000000303000000000018",
    8: "4405000000000100000000000000000000000000000000000000000000000000000000000000000000000803000000000000",
    9: "4405000000000100000000000000000000000000000000000000000000000000000000000000000000000701000000000000",
    10: "4505000000000100000000000000000000000000000000000000000000000000000000000000000000000303000000000000",
    11: "4505000000000100000000000000000000000000000000000000000000000000000000000000000000000901000000000020",
    12: "e404000010410000000000000000000000000000000000000000000000000000000000000000000000000000010000000000",
}.items()})


def _decode(raws):
    return [protocol.decode_scenario(raws[i], i) for i in range(25)]


def _with_actions(raws, index, **actions):
    """The same scenarios with one scenario's output actions changed."""
    scenario = protocol.decode_scenario(raws[index], index)
    document = json.loads(json.dumps(scenario["document"]))
    document["actions"].update(actions)
    changed = dict(raws)
    changed[index] = protocol.build_scenario_from_dict(document, raws[index])
    return changed


class HoldSettingTests(unittest.TestCase):
    def test_4wd_holds_remote_with_output_1(self):
        self.assertEqual(protocol.remote_hold_setting(_decode(RAW_4WD)), 1341)

    def test_2025_profiles_hold_nothing(self):
        self.assertIsNone(protocol.remote_hold_setting(_decode(RAW_2025)))

    def test_unread_or_partial_scenarios_give_nothing(self):
        self.assertIsNone(protocol.remote_hold_setting([]))
        self.assertIsNone(protocol.remote_hold_setting(_decode(RAW_4WD)[:24]))

    def test_the_output_number_follows_the_scenarios(self):
        starters = [i for i, s in enumerate(_decode(RAW_4WD))
                    if s["document"]["actions"]["external_output_on"] == 1]
        self.assertEqual(len(starters), 2)  # preheat and cooling
        raws = RAW_4WD
        for index in starters:
            raws = _with_actions(raws, index, external_output_on=4)
        self.assertEqual(protocol.remote_hold_setting(_decode(raws)), 1344)

    def test_a_timed_output_is_not_a_hold(self):
        raws = RAW_4WD
        for index, s in enumerate(_decode(RAW_4WD)):
            if s["document"]["actions"]["external_output_on"] == 1:
                raws = _with_actions(raws, index, external_output_on_seconds=5)
        self.assertIsNone(protocol.remote_hold_setting(_decode(raws)))

    def test_two_different_held_outputs_are_ambiguous(self):
        first = next(i for i, s in enumerate(_decode(RAW_4WD))
                     if s["document"]["actions"]["external_output_on"] == 1)
        raws = _with_actions(RAW_4WD, first, external_output_on=4)
        self.assertIsNone(protocol.remote_hold_setting(_decode(raws)))


class _Session:
    def __init__(self, states):
        self.states = list(states)
        self.queries = []
        self.writes = []

    async def query_sensor(self, sensor_id, sub_id=0):
        self.queries.append(sensor_id)
        value = self.states.pop(0)
        return None if value is None else {"name": "key", "value": value}

    async def set_setting(self, setting_id, sub_id, value):
        self.writes.append((setting_id, sub_id, bytes(value)))
        return {"success": True}


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        async def executor(fn, *a):
            return fn(*a)
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(path=lambda *p: str(Path(self.temp.name, *p))),
            async_add_executor_job=executor)
        self.rt = runtime_module.VegaRuntime(hass, "test", 21443, "0000", "0000", 30,
                                             direct_control=True)

    async def _run(self, raws, states):
        self.rt._command_scenarios = _decode(raws) if raws else []
        session = _Session(states)
        for _ in states:
            await self.rt._remote_state_watchdog(session)
        return session

    async def test_4wd_clears_output_1_when_remote_ends(self):
        session = await self._run(RAW_4WD, [1, 3, 3, 1, 1])
        self.assertEqual(session.writes, [(1341, 0, b"\x00")])
        self.assertEqual(session.queries, [protocol.REMOTE_STATE_SENSOR] * 5)

    async def test_4wd_writes_nothing_without_a_3_to_other_transition(self):
        session = await self._run(RAW_4WD, [1, 1, 3, 3, None, 1])
        self.assertEqual(session.writes, [])  # the unanswered query loses the transition

    async def test_2025_profile_is_neither_asked_nor_written(self):
        session = await self._run(RAW_2025, [3, 1, 3, 1])
        self.assertEqual(session.queries, [])
        self.assertEqual(session.writes, [])

    async def test_unread_scenarios_are_neither_asked_nor_written(self):
        session = await self._run(None, [3, 1])
        self.assertEqual(session.queries, [])
        self.assertEqual(session.writes, [])

    async def test_the_cleared_output_is_the_one_the_scenarios_hold(self):
        raws = RAW_4WD
        for index, s in enumerate(_decode(RAW_4WD)):
            if s["document"]["actions"]["external_output_on"] == 1:
                raws = _with_actions(raws, index, external_output_on=4)
        session = await self._run(raws, [3, 1])
        self.assertEqual(session.writes, [(1344, 0, b"\x00")])

    async def test_state_seen_before_the_scenarios_changed_is_forgotten(self):
        await self._run(RAW_4WD, [3])
        await self._run(RAW_2025, [None])   # hold disappears: nothing asked, state dropped
        session = await self._run(RAW_4WD, [1])
        self.assertEqual(session.writes, [])


if __name__ == "__main__":
    unittest.main()
