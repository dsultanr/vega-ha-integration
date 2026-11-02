"""Sensor platform for Vega."""
from __future__ import annotations

from typing import Any

from typing import Any as _Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DEFAULT_IMEI, DOMAIN, SENSOR_TYPES
from .coordinator import VegaCoordinator
from .entity import build_device_info
from .hv_battery import OUTPUTS as HV_OUTPUTS

TIMESTAMP_KEYS = {"position_time", "last_online"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Vega sensors."""
    coordinator: VegaCoordinator = hass.data[DOMAIN][entry.entry_id]
    added = False

    @callback
    def _sync_sensors() -> None:
        nonlocal added
        if added:
            return
        if not coordinator.imei or coordinator.imei == DEFAULT_IMEI:
            return
        added = True
        entities = [VegaSensor(coordinator, key, *spec) for key, spec in SENSOR_TYPES.items()]
        entities.append(VegaScriptsSensor(coordinator))
        async_add_entities(entities)

    _sync_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_sync_sensors))


class VegaScriptsSensor(CoordinatorEntity, SensorEntity):
    """Exposes the decoded Setting 5502 CAN scripts (for the editor card)."""

    _attr_icon = "mdi:script-text-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: VegaCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "CAN Scripts"
        self._attr_unique_id = f"vega_{coordinator.imei}_can_scripts"
        self.entity_id = f"sensor.vega_{coordinator.imei}_can_scripts"
        self._attr_device_info = build_device_info(coordinator.imei, coordinator.device_name, coordinator.entry_id)

    @property
    def native_value(self) -> int:
        return len(self.coordinator.runtime.can_scripts)

    @property
    def extra_state_attributes(self) -> dict[str, _Any]:
        runtime = self.coordinator.runtime
        return {"slots": runtime.can_scripts, "entry_id": self.coordinator.entry_id,
                "scenarios": runtime.scenarios,
                "command_bindings": runtime.command_bindings,
                "scenarios_read_at": runtime.scenario_read_at,
                "hv_setup": runtime.hv_setup, "direct_control": runtime.direct_control}


class VegaSensor(CoordinatorEntity, SensorEntity):
    """A single telemetry value from the Vega channel."""

    def __init__(
        self,
        coordinator: VegaCoordinator,
        key: str,
        name: str,
        unit: str | None,
        device_class: str | None,
        icon: str | None,
        state_key: str,
        state_class: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._state_key = state_key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_icon = icon
        self._attr_state_class = state_class
        self._attr_unique_id = f"vega_{coordinator.imei}_{key}"
        self.entity_id = f"sensor.vega_{coordinator.imei}_{key}"
        self._attr_device_info = build_device_info(coordinator.imei, coordinator.device_name, coordinator.entry_id)

    @property
    def native_value(self) -> Any:
        value = (self.coordinator.data or {}).get(self._state_key)
        if value is None:
            return None
        if self._state_key in TIMESTAMP_KEYS:
            return dt_util.parse_datetime(value)
        # Numeric sensors (those carrying a state_class) must never be handed a
        # non-numeric value, or HA raises; drop it instead of crashing.
        if self._attr_state_class is not None and not isinstance(value, (int, float)):
            return None
        return value

    @property
    def available(self) -> bool:
        # Unavailable until this specific value has been seen (no fake defaults).
        if self._state_key in HV_OUTPUTS:
            return (self.coordinator.data or {}).get(self._state_key) is not None
        return self._state_key in (self.coordinator.data or {})
