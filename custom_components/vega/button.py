"""Button platform for Vega direct commands (evolute-style block/pending UX)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BUTTON_TYPES, DEFAULT_IMEI, DOMAIN
from .coordinator import VegaCoordinator
from .entity import build_device_info
from .runtime import CommandError

_LOGGER = logging.getLogger(__name__)
PENDING_ICON = "mdi:timer-sand"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: VegaCoordinator = hass.data[DOMAIN][entry.entry_id]
    added_keys: set[str] = set()

    @callback
    def _sync_buttons() -> None:
        if not coordinator.imei or coordinator.imei == DEFAULT_IMEI:
            return
        if not coordinator.runtime.can_scripts:
            return

        to_add: list[VegaButton] = []
        for key, (name, action, icon, confirm_key) in BUTTON_TYPES.items():
            if key in added_keys:
                continue
            added_keys.add(key)
            to_add.append(
                VegaButton(coordinator, key, name, action, icon, confirm_key)
            )

        if to_add:
            async_add_entities(to_add)

    # Buttons are NOT added by default until settings/scripts are read from tracker.
    # If scripts were already restored from disk on startup, add them now.
    _sync_buttons()

    # Dynamically register buttons when scripts are read from the tracker.
    entry.async_on_unload(coordinator.async_add_listener(_sync_buttons))


class VegaButton(CoordinatorEntity, ButtonEntity):
    """A direct command, guarded like the evolute buttons (block + pending)."""

    def __init__(self, coordinator, key, name, action, icon, confirm_key):
        super().__init__(coordinator)
        self._action = action
        self._confirm_key = confirm_key
        self._idle_icon = icon
        self._fallback_name = name
        self._attr_unique_id = f"vega_{coordinator.imei}_{key}"
        self.entity_id = f"button.vega_{coordinator.imei}_{key}"
        self._attr_device_info = build_device_info(coordinator.imei, coordinator.device_name, coordinator.entry_id)

    @property
    def name(self) -> str:
        # Only an unambiguous route read from 4001 can supply a slot name.
        slot = self.coordinator.runtime.command_slot(self._action)
        if slot is not None:
            slot_name = self.coordinator.runtime.slot_name(slot)
            if slot_name:
                return slot_name
        return self._fallback_name

    @property
    def _pending(self) -> bool:
        return self.coordinator.is_command_pending(self._action)

    @property
    def icon(self) -> str:
        return PENDING_ICON if self._pending else self._idle_icon

    @property
    def available(self) -> bool:
        # Needs direct control on, the car online, and the command not blocked.
        if not self.coordinator.runtime.direct_control:
            return False
        if not (self.coordinator.data or {}).get("is_online"):
            return False
        return not self.coordinator.is_command_blocked(self._action)

    async def async_press(self) -> None:
        reason = self.coordinator.command_block_reason(self._action)
        if reason:
            raise HomeAssistantError(f"Команда недоступна: {reason}")
        _LOGGER.info("Vega button pressed: %s", self._action)
        try:
            await self.coordinator.async_send_command(self._action, self._confirm_key)
        except CommandError as err:
            raise HomeAssistantError(str(err)) from err

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "command": self._action,
            "pending": self._pending,
            "blocked": self.coordinator.is_command_blocked(self._action),
            "script_binding": self.coordinator.runtime.command_bindings[self._action],
            "sends": self.coordinator.runtime.command_resolution[self._action],
        }
        reason = self.coordinator.command_block_reason(self._action)
        if reason:
            attrs["blocked_reason"] = reason
        if self._confirm_key:
            attrs["confirm_key"] = self._confirm_key
        return attrs
