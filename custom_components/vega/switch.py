"""Switch platform for Vega: Proxy Mode (learn) and Direct Control."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DIRECT_CONTROL,
    CONF_PROXY_MODE,
    DEFAULT_DIRECT_CONTROL,
    DEFAULT_IMEI,
    DEFAULT_PROXY_MODE,
    DOMAIN,
)
from .coordinator import VegaCoordinator
from .entity import build_device_info

# key -> (name, icon, option key, default)
SWITCHES = {
    "proxy_mode": ("Proxy Mode (learn)", "mdi:lan-connect", CONF_PROXY_MODE, DEFAULT_PROXY_MODE),
    "direct_control": ("Direct Control", "mdi:car-cog", CONF_DIRECT_CONTROL, DEFAULT_DIRECT_CONTROL),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: VegaCoordinator = hass.data[DOMAIN][entry.entry_id]
    added = False

    @callback
    def _sync_switches() -> None:
        nonlocal added
        if added:
            return
        if not coordinator.imei or coordinator.imei == DEFAULT_IMEI:
            return
        added = True
        async_add_entities(
            VegaSwitch(coordinator, entry, key, *spec) for key, spec in SWITCHES.items()
        )

    _sync_switches()
    entry.async_on_unload(coordinator.async_add_listener(_sync_switches))


class VegaSwitch(CoordinatorEntity, SwitchEntity):
    """A config toggle stored in the entry options; updates runtime dynamically."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: VegaCoordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
        icon: str,
        opt_key: str,
        default: bool,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        self._opt_key = opt_key
        self._default = default
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"vega_{coordinator.imei}_{key}"
        self.entity_id = f"switch.vega_{coordinator.imei}_{key}"
        self._attr_device_info = build_device_info(coordinator.imei, coordinator.device_name, coordinator.entry_id)

    @property
    def is_on(self) -> bool:
        if self._key == "direct_control":
            return bool(self.coordinator.runtime.direct_control)
        if self._key == "proxy_mode":
            return bool(self.coordinator.runtime.proxy_mode)
        if self._opt_key in self._entry.options:
            return bool(self._entry.options[self._opt_key])
        return bool(self._entry.data.get(self._opt_key, self._default))

    async def _set(self, value: bool) -> None:
        if self._key == "direct_control":
            self.coordinator.runtime.direct_control = value
            if value:   # first time on this tracker: learn which setting runs which script
                self.hass.async_create_task(self.coordinator.runtime.async_prime_command_scenarios())
        elif self._key == "proxy_mode":
            self.coordinator.runtime.proxy_mode = value

        cur = self.hass.config_entries.async_get_entry(self._entry.entry_id) or self._entry
        options = {**cur.options, self._opt_key: value}
        self.hass.config_entries.async_update_entry(cur, options=options)

        self.coordinator.runtime._publish()
        self.coordinator.async_update_listeners()
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)
