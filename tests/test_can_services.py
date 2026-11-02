"""Exercise real HA service registration/dispatch, using fake runtimes only."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from custom_components.vega import _register_services, _register_editor_panel, _register_card_resource, async_unload_entry
from custom_components.vega.const import PANEL_PATH, CARD_VERSION
from custom_components.vega.entity import build_device_info


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.runtimes = {
            entry: types.SimpleNamespace(
                async_write_can_script=AsyncMock(return_value={"verified": True, "slot": 5}),
                async_read_can_scripts=AsyncMock(return_value=[{}] * 8),
                async_write_commands=AsyncMock(return_value={"verified": True, "written": ["4001/3"]}),
                async_prepare_hv_battery=AsyncMock(return_value={"plan_id": "plan-two", "configured": False}),
                async_configure_hv_battery=AsyncMock(return_value={"verified": True, "written": ["5500/0"]}),
                scenario_read_at="today",
            ) for entry in ("one", "two")
        }
        self.hass.data["vega"] = {entry: types.SimpleNamespace(runtime=runtime)
                                  for entry, runtime in self.runtimes.items()}
        _register_services(self.hass)

    async def test_multiple_trackers_require_target_without_broadcasting(self):
        with self.assertRaises(ServiceValidationError):
            await self.hass.services.async_call("vega", "write_can_script", {"slot": 5, "script": {}},
                                                blocking=True, return_response=True)
        for runtime in self.runtimes.values():
            runtime.async_write_can_script.assert_not_called()

    async def test_explicit_target_and_revision_reach_only_selected_tracker(self):
        response = await self.hass.services.async_call(
            "vega", "write_can_script", {"entry_id": "two", "slot": 5, "script": {}, "expected_raw": "ab" * 82},
            blocking=True, return_response=True)
        self.assertEqual(response, {"verified": True, "slot": 5})
        self.runtimes["one"].async_write_can_script.assert_not_called()
        self.runtimes["two"].async_write_can_script.assert_awaited_once_with(5, {}, "ab" * 82)

    async def test_single_tracker_preserves_existing_service_calls(self):
        self.hass.data["vega"].pop("two")
        await self.hass.services.async_call("vega", "write_can_script", {"slot": 5, "script": {}}, blocking=True)
        self.runtimes["one"].async_write_can_script.assert_awaited_once_with(5, {}, None)

    async def test_unknown_target_does_not_fall_back_to_other_tracker(self):
        with self.assertRaises(ServiceValidationError):
            await self.hass.services.async_call("vega", "read_can_scripts", {"entry_id": "missing"},
                                                blocking=True, return_response=True)
        for runtime in self.runtimes.values():
            runtime.async_read_can_scripts.assert_not_called()

    async def test_read_returns_scenario_availability(self):
        self.runtimes["one"].scenario_read_at = None
        response = await self.hass.services.async_call("vega", "read_can_scripts", {"entry_id": "one"},
                                                      blocking=True, return_response=True)
        self.assertEqual(response, {"slots": 8, "scenarios_read": False})
        self.runtimes["two"].async_read_can_scripts.assert_not_called()

    async def test_command_bundle_targets_one_tracker_and_returns_verification(self):
        document = {"scenarios": [], "scripts": []}
        expected = {"4001/3": "00" * 50}
        result = await self.hass.services.async_call("vega", "write_commands",
            {"entry_id": "two", "document": document, "expected": expected}, blocking=True, return_response=True)
        self.assertTrue(result["verified"])
        self.runtimes["two"].async_write_commands.assert_awaited_once_with(document, expected)
        self.runtimes["one"].async_write_commands.assert_not_called()

    async def test_commands_read_alias_is_registered(self):
        result = await self.hass.services.async_call("vega", "read_commands", {"entry_id": "one"}, blocking=True, return_response=True)
        self.assertEqual(result, {"slots": 8, "scenarios_read": True})

    async def test_hv_setup_targets_selected_tracker_and_returns_plan(self):
        result = await self.hass.services.async_call("vega", "prepare_hv_battery",
            {"entry_id": "two", "profile": "f515_ispace_4wd"}, blocking=True, return_response=True)
        self.assertEqual(result['plan_id'], 'plan-two')
        self.runtimes['two'].async_prepare_hv_battery.assert_awaited_once_with('f515_ispace_4wd')
        result = await self.hass.services.async_call("vega", "configure_hv_battery",
            {"entry_id": "two", "plan_id": result['plan_id']}, blocking=True, return_response=True)
        self.assertTrue(result['verified'])
        self.runtimes['two'].async_configure_hv_battery.assert_awaited_once_with('plan-two')
        self.runtimes['one'].async_prepare_hv_battery.assert_not_called()
        self.runtimes['one'].async_configure_hv_battery.assert_not_called()

    async def test_hv_setup_never_broadcasts_or_uses_unknown_target(self):
        for target in ({}, {'entry_id': 'missing'}):
            with self.assertRaises(ServiceValidationError):
                await self.hass.services.async_call('vega', 'configure_hv_battery',
                    {**target, 'plan_id': 'plan'}, blocking=True, return_response=True)
        for runtime in self.runtimes.values():
            runtime.async_configure_hv_battery.assert_not_called()

    async def test_one_hidden_admin_panel_serves_multiple_devices(self):
        from homeassistant.components import frontend
        await _register_editor_panel(self.hass)
        panel = self.hass.data[frontend.DATA_PANELS][PANEL_PATH]
        await _register_editor_panel(self.hass)
        self.assertIs(self.hass.data[frontend.DATA_PANELS][PANEL_PATH], panel)
        self.assertIsNone(panel.sidebar_title)
        self.assertTrue(panel.require_admin)
        self.assertEqual(panel.config["_panel_custom"]["module_url"],
                         f"/local/vega/vega-config-panel.js?v={CARD_VERSION}")
        for entry in ("one", "two"):
            self.assertEqual(build_device_info("test-imei", "Vega", entry)["configuration_url"],
                             f"homeassistant://{PANEL_PATH}/{entry}")

    async def test_unloading_one_tracker_keeps_panel_until_last_entry(self):
        from homeassistant.components import frontend
        await _register_editor_panel(self.hass)
        for runtime in self.runtimes.values():
            runtime.stop = AsyncMock()
        config_entries = types.SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True))
        with patch.object(self.hass, "config_entries", config_entries):
            self.assertTrue(await async_unload_entry(self.hass, types.SimpleNamespace(entry_id="one")))
            self.assertIn(PANEL_PATH, self.hass.data[frontend.DATA_PANELS])
            self.assertTrue(await async_unload_entry(self.hass, types.SimpleNamespace(entry_id="two")))
            self.assertNotIn(PANEL_PATH, self.hass.data[frontend.DATA_PANELS])

    async def test_panel_module_is_not_loaded_as_a_dashboard_resource(self):
        resources = types.SimpleNamespace(loaded=True, async_items=lambda: [], async_create_item=AsyncMock())
        self.hass.data["lovelace"] = {"resources": resources}
        await _register_card_resource(self.hass)
        urls = [c.args[0]["url"] for c in resources.async_create_item.await_args_list]
        self.assertEqual(len(urls), 2)
        self.assertFalse(any("config-panel" in u for u in urls))


if __name__ == "__main__":
    unittest.main()
