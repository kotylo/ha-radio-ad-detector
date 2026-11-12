"""UI setup and reconfiguration for the self-contained audio detector."""
from __future__ import annotations

import ipaddress
import shutil
import socket
import secrets

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig

from .const import (
    CONF_ADVERTISED_HOST,
    CONF_OUTPUT_FORMAT,
    CONF_PORT,
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_PORT,
    DOMAIN,
)
from .radio_audio.engine import OUTPUT_CONTENT_TYPES
from .radio_audio.native_model import library_path


def check_local_runtime(port):
    library_path()
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg_missing")
    with socket.create_server(("0.0.0.0", port)):
        pass


def schema(defaults):
    return vol.Schema({
        vol.Required(CONF_ADVERTISED_HOST, default=defaults.get(CONF_ADVERTISED_HOST, "")): str,
        vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(vol.Coerce(int), vol.Range(min=1024, max=65535)),
    })


class RadioConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> RadioOptionsFlow:
        return RadioOptionsFlow()

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self._form("user", user_input, {})

    async def async_step_import(self, user_input):
        return await self.async_step_user(user_input)

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        return await self._form("reconfigure", user_input, entry.data)

    async def _form(self, step, user_input, defaults):
        errors = {}
        if user_input is not None:
            try:
                # Cast devices need a LAN IP, not an mDNS .local hostname.
                host = str(ipaddress.ip_address(user_input[CONF_ADVERTISED_HOST].strip()))
                address = ipaddress.ip_address(host)
                if address.is_unspecified or address.is_loopback or address.is_multicast:
                    raise ValueError("Use the LAN address")
                port = int(user_input[CONF_PORT])
                if step == "reconfigure" and port == defaults.get(CONF_PORT):
                    # The current instance already owns its configured port.
                    await self.hass.async_add_executor_job(library_path)
                else:
                    await self.hass.async_add_executor_job(check_local_runtime, port)
            except ValueError:
                errors[CONF_ADVERTISED_HOST] = "invalid_host"
            except OSError:
                errors["base"] = "port_in_use"
            except RuntimeError as exc:
                errors["base"] = "ffmpeg_missing" if str(exc) == "ffmpeg_missing" else "unsupported_platform"
            else:
                data = {CONF_ADVERTISED_HOST: host, CONF_PORT: port}
                if step == "reconfigure":
                    return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data_updates=data)
                data["signing_secret"] = secrets.token_urlsafe(32)
                return self.async_create_entry(title="Radio Ad Detector", data=data)
        return self.async_show_form(step_id=step, data_schema=schema(user_input or defaults), errors=errors)


class RadioOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data={**self.config_entry.options, **user_input})

        output_format = self.config_entry.options.get(CONF_OUTPUT_FORMAT, DEFAULT_OUTPUT_FORMAT)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_OUTPUT_FORMAT, default=output_format): SelectSelector(
                    SelectSelectorConfig(
                        options=list(OUTPUT_CONTENT_TYPES), translation_key=CONF_OUTPUT_FORMAT,
                    ),
                ),
            }),
        )
