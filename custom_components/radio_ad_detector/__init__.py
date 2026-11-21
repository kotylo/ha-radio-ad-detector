"""Self-contained radio relay, local inference and native HA entities."""
from __future__ import annotations

from dataclasses import dataclass
import logging

from aiohttp import web
import voluptuous as vol
from yarl import URL

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .assets import async_ensure_models
from .const import (
    CONF_ADVERTISED_HOST,
    CONF_OUTPUT_FORMAT,
    CONF_PLAYBACK_BUFFER,
    CONF_PORT,
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_PLAYBACK_BUFFER,
    DOMAIN,
    PLATFORMS,
)
from .radio_audio.engine import FADE_IN_SECONDS, OUTPUT_CONTENT_TYPES, STREAM_SLOTS, create_app
from .radio_audio.native_model import NativeYamnet
from .radio_audio.stream_urls import stream_signature, validate_stream_url

LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = vol.Schema({
    vol.Optional(DOMAIN): vol.Schema({
        vol.Required(CONF_ADVERTISED_HOST): cv.string,
        vol.Optional(CONF_PORT, default=8098): cv.port,
    }),
}, extra=vol.ALLOW_EXTRA)


@dataclass
class Runtime:
    coordinator: DataUpdateCoordinator
    runner: web.AppRunner
    model: NativeYamnet
    base_url: str
    signing_secret: str


def active_entry(hass):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if isinstance(getattr(entry, "runtime_data", None), Runtime):
            return entry
    raise HomeAssistantError("Radio Ad Detector is not loaded")


def proxy_details(entry, call):
    runtime = entry.runtime_data
    try:
        source = validate_stream_url(call.data["stream_url"])
    except ValueError as exc:
        raise HomeAssistantError(str(exc)) from exc
    output_format = call.data.get(
        CONF_OUTPUT_FORMAT, entry.options.get(CONF_OUTPUT_FORMAT, DEFAULT_OUTPUT_FORMAT)
    )
    params = {"url": str(source), "format": output_format}
    if "bitrate" in call.data:
        params["bitrate"] = str(call.data["bitrate"])
    slot = call.data.get("slot")
    if slot is not None:
        params["slot"] = str(slot)
    fade_in = call.data.get("fade_in_seconds", 0)
    if fade_in:
        params["fade_in_seconds"] = str(fade_in)
    params["sig"] = stream_signature(
        runtime.signing_secret, source, call.data.get("bitrate"), output_format, slot, fade_in,
    )
    return {
        "url": str(URL(runtime.base_url).with_path("/stream").with_query(params)),
        "media_content_type": OUTPUT_CONTENT_TYPES[output_format],
    }


async def async_setup(hass: HomeAssistant, config):
    async def get_proxy_url(call: ServiceCall):
        return proxy_details(active_entry(hass), call)

    async def play(call: ServiceCall):
        proxy = proxy_details(active_entry(hass), call)
        await hass.services.async_call("media_player", "play_media", {
            "entity_id": call.data["entity_id"], "media_content_id": proxy["url"],
            "media_content_type": proxy["media_content_type"],
            "extra": {"stream_type": "LIVE"},
        }, blocking=True, context=call.context)

    fields = {
        vol.Required("stream_url"): cv.string,
        vol.Optional("bitrate"): vol.All(vol.Coerce(int), vol.Range(min=32, max=512)),
        vol.Optional(CONF_OUTPUT_FORMAT): vol.In(OUTPUT_CONTENT_TYPES),
        vol.Optional("slot"): vol.All(vol.In(STREAM_SLOTS), vol.Coerce(int)),
    }
    hass.services.async_register(DOMAIN, "get_proxy_url", get_proxy_url, schema=vol.Schema(fields), supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "play", play, schema=vol.Schema({
        **fields,
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("fade_in_seconds"): vol.All(vol.In(FADE_IN_SECONDS), vol.Coerce(int)),
    }))
    if DOMAIN in config:
        hass.async_create_task(hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "import"}, data=dict(config[DOMAIN]),
        ))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    model = runner = None
    try:
        model_config = await async_ensure_models(hass)
        model = await hass.async_add_executor_job(NativeYamnet, model_config)
        app = create_app(
            {"streams": {}, "allow_custom_urls": True, "signing_secret": entry.data["signing_secret"]},
            model,
            get_playback_buffer=lambda: entry.options.get(CONF_PLAYBACK_BUFFER, DEFAULT_PLAYBACK_BUFFER),
        )
        coordinator = DataUpdateCoordinator(hass, LOGGER, name="Radio Ad Detector")
        radio = app["radio"]
        radio.state_callback = coordinator.async_set_updated_data
        coordinator.async_set_updated_data(radio.snapshot())
        runner = web.AppRunner(app, handler_cancellation=True, access_log=None, shutdown_timeout=5)
        await runner.setup()
        await web.TCPSite(runner, None, int(entry.data[CONF_PORT])).start()
        host = entry.data[CONF_ADVERTISED_HOST]
        base_url = str(URL.build(scheme="http", host=host, port=int(entry.data[CONF_PORT])))
        entry.runtime_data = Runtime(coordinator, runner, model, base_url, entry.data["signing_secret"])
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception as exc:
        if runner is not None:
            await runner.cleanup()
        if model is not None:
            await hass.async_add_executor_job(model.close)
        entry.runtime_data = None
        raise ConfigEntryNotReady("Cannot start the local radio detector; check runtime, model download and listening port") from exc
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime = entry.runtime_data
    await runtime.runner.cleanup()
    await hass.async_add_executor_job(runtime.model.close)
    entry.runtime_data = None
    return True
