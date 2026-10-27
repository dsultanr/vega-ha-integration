"""Config and options flow for the Vega integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_IMEI,
    CONF_PIN,
    CONF_PIN2,
    CONF_POLL_INTERVAL,
    CONF_PORT,
    CONF_UPSTREAM_HOST,
    CONF_UPSTREAM_PORT,
    DEFAULT_IMEI,
    DEFAULT_PIN,
    DEFAULT_PIN2,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_UPSTREAM_HOST,
    DEFAULT_UPSTREAM_PORT,
    DOMAIN,
)

# IMEI and PIN can be specified up-front or learned automatically in proxy mode.
# If IMEI is left blank, entity registration is deferred until the tracker connects.
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(int, vol.Range(min=1, max=65535)),
        vol.Optional(CONF_IMEI, default=""): str,
        vol.Optional(CONF_UPSTREAM_HOST, default=DEFAULT_UPSTREAM_HOST): str,
        vol.Optional(CONF_UPSTREAM_PORT, default=DEFAULT_UPSTREAM_PORT): vol.All(int, vol.Range(min=1, max=65535)),
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Vega."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        if user_input is not None:
            raw_imei = str(user_input.get(CONF_IMEI, "")).strip()
            imei = raw_imei or DEFAULT_IMEI
            await self.async_set_unique_id(imei)
            self._abort_if_unique_id_configured()
            data = {**user_input}
            if raw_imei:
                data[CONF_IMEI] = raw_imei
                data[CONF_LEARNED_IMEI] = raw_imei
            return self.async_create_entry(title=f"Vega {raw_imei}" if raw_imei else "Vega", data=data)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "OptionsFlowHandler":
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Allow changing the port, IMEI, PIN, poll interval and upstream after setup."""

    def __init__(self) -> None:
        pass

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            # Merge so the Proxy Mode / Direct Control switch states (also stored in
            # options) and learned data are preserved across an options save.
            data = {**self.config_entry.options, **user_input}
            raw_imei = str(user_input.get(CONF_IMEI, "")).strip()
            if raw_imei:
                data[CONF_IMEI] = raw_imei
            return self.async_create_entry(title="", data=data)

        current = {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(CONF_PORT, default=current.get(CONF_PORT, DEFAULT_PORT)): vol.All(int, vol.Range(min=1, max=65535)),
                vol.Optional(CONF_IMEI, default=current.get(CONF_IMEI, current.get("learned_imei", ""))): str,
                vol.Required(CONF_PIN, default=current.get(CONF_PIN, DEFAULT_PIN)): str,
                vol.Optional(CONF_PIN2, default=current.get(CONF_PIN2, DEFAULT_PIN2)): str,
                vol.Optional(CONF_POLL_INTERVAL, default=current.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)): vol.All(int, vol.Range(min=5, max=3600)),
                vol.Optional(CONF_UPSTREAM_HOST, default=current.get(CONF_UPSTREAM_HOST, DEFAULT_UPSTREAM_HOST)): str,
                vol.Optional(CONF_UPSTREAM_PORT, default=current.get(CONF_UPSTREAM_PORT, DEFAULT_UPSTREAM_PORT)): vol.All(int, vol.Range(min=1, max=65535)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
