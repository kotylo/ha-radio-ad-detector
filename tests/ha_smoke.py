"""Run with HA's Python against an isolated, temporary HA instance.

Usage: python ha_smoke.py /config/custom_components/radio_ad_detector
Does not connect to or change the running Home Assistant instance.
"""
import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import AsyncMock, patch
from contextlib import contextmanager

from homeassistant.core import HomeAssistant
from homeassistant import loader
from homeassistant.bootstrap import async_from_config_dict
from homeassistant.setup import async_setup_component


class FakeModel:
    samples = 15600
    def __init__(self, config): pass
    def classify(self, pcm):
        return {"speech": 0.95, "music": 0.01, "singing": 0.0, "rms": 0.1, "top_classes": []}
    def close(self): pass


@contextmanager
def runtime_mode(prefix, real):
    if real:
        yield
    else:
        with patch(prefix + ".NativeYamnet", FakeModel), patch(prefix + ".async_ensure_models", AsyncMock(return_value={})), patch(prefix + ".config_flow.check_local_runtime", return_value=None):
            yield


async def run(source, models=None):
    with tempfile.TemporaryDirectory(prefix="radio-ha-test-") as temporary:
        target = Path(temporary) / "custom_components" / "radio_ad_detector"
        target.parent.mkdir()
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        if models is not None:
            cache = Path(temporary) / ".storage/radio_ad_detector/models"
            cache.mkdir(parents=True)
            for name in ("yamnet.tflite", "yamnet_class_map.csv"):
                shutil.copyfile(models / name, cache / name)
        hass = HomeAssistant(temporary)
        loader.async_setup(hass)
        try:
            assert await async_from_config_dict({
                "homeassistant": {"time_zone": "UTC", "latitude": 0, "longitude": 0, "elevation": 0, "unit_system": "metric"},
                "http": {"server_host": "127.0.0.1", "server_port": 18123},
            }, hass)
            assert await async_setup_component(hass, "radio_ad_detector", {})
            prefix = "custom_components.radio_ad_detector"
            with runtime_mode(prefix, models is not None):
                result = await hass.config_entries.flow.async_init(
                    "radio_ad_detector", context={"source": "user"},
                    data={"advertised_host": "192.0.2.10", "port": 18098},
                )
                assert result["type"] == "create_entry", result
                await hass.async_block_till_done()
                entry = result["result"]
                assert entry.runtime_data is not None, entry.state
                response = await hass.services.async_call(
                    "radio_ad_detector", "get_proxy_url",
                    {"stream_url": "https://radio.example/live?token=a%2Bb&quality=high", "bitrate": 192},
                    blocking=True, return_response=True,
                )
                from yarl import URL
                url = URL(response["url"])
                assert url.host == "192.0.2.10" and url.port == 18098
                assert url.query["url"] == "https://radio.example/live?token=a%2Bb&quality=high"
                assert url.query["format"] == "wav"
                assert response["media_content_type"] == "audio/wav"
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    local_url = url.with_host("127.0.0.1")
                    async with session.head(local_url) as result:
                        assert result.status == 200
                        assert result.headers["Content-Type"] == "audio/wav"
                    async with session.head(local_url.update_query(format="mp3")) as result:
                        assert result.status == 403
                    async with session.head(local_url.with_query({"url": url.query["url"]})) as result:
                        assert result.status == 403
                calls = []
                async def play(call): calls.append(dict(call.data))
                hass.services.async_register("media_player", "play_media", play)
                await hass.services.async_call("radio_ad_detector", "play", {
                    "stream_url": "https://radio.example/live", "entity_id": "media_player.testing",
                }, blocking=True)
                assert len(calls) == 1 and calls[0]["media_content_type"] == "audio/wav"
                assert calls[0]["extra"]["stream_type"] == "LIVE"
                assert URL(calls[0]["media_content_id"]).query["format"] == "wav"
                await hass.services.async_call("radio_ad_detector", "play", {
                    "stream_url": "https://radio.example/live", "entity_id": "media_player.testing",
                    "output_format": "mp3", "slot": 2, "fade_in_seconds": 0,
                }, blocking=True)
                assert len(calls) == 2 and calls[1]["media_content_type"] == "audio/mpeg"
                assert URL(calls[1]["media_content_id"]).query["format"] == "mp3"
                assert URL(calls[1]["media_content_id"]).query["slot"] == "2"
                mp3 = await hass.services.async_call(
                    "radio_ad_detector", "get_proxy_url",
                    {"stream_url": "https://radio.example/live", "output_format": "mp3", "slot": 2},
                    blocking=True, return_response=True,
                )
                assert mp3["url"] == calls[1]["media_content_id"]
                assert mp3["media_content_type"] == "audio/mpeg"
                await hass.services.async_call("radio_ad_detector", "play", {
                    "stream_url": "https://radio.example/live", "entity_id": "media_player.testing",
                    "fade_in_seconds": 10,
                }, blocking=True)
                faded = URL(calls[-1]["media_content_id"])
                assert faded.query["fade_in_seconds"] == "10"
                async with aiohttp.ClientSession() as session:
                    async with session.head(faded.with_host("127.0.0.1")) as result:
                        assert result.status == 200
                    async with session.head(URL(mp3["url"]).with_host("127.0.0.1")) as result:
                        assert result.status == 200
                        assert result.headers["Content-Type"] == "audio/mpeg"

                options = await hass.config_entries.options.async_init(entry.entry_id)
                assert options["type"] == "form", options
                assert options["data_schema"]({}) == {"output_format": "wav", "playback_buffer_seconds": 2}
                radio = entry.runtime_data.runner.app["radio"]
                assert radio.get_playback_buffer() == 2
                saved = await hass.config_entries.options.async_configure(
                    options["flow_id"], {"output_format": "mp3", "playback_buffer_seconds": 0},
                )
                assert saved["type"] == "create_entry", saved
                assert entry.options["output_format"] == "mp3"
                assert entry.options["playback_buffer_seconds"] == radio.get_playback_buffer() == 0

                options = await hass.config_entries.options.async_init(entry.entry_id)
                assert options["data_schema"]({}) == {"output_format": "mp3", "playback_buffer_seconds": 0}
                saved = await hass.config_entries.options.async_configure(
                    options["flow_id"], {"output_format": "mp3", "playback_buffer_seconds": 1.5},
                )
                assert saved["type"] == "create_entry", saved
                assert entry.options["playback_buffer_seconds"] == radio.get_playback_buffer() == 1.5

                station = {"stream_url": "https://radio.example/live"}
                for override, expected_format, expected_type in (
                    ({}, "mp3", "audio/mpeg"),
                    ({"output_format": "wav"}, "wav", "audio/wav"),
                ):
                    data = {**station, **override}
                    proxy = await hass.services.async_call(
                        "radio_ad_detector", "get_proxy_url", data,
                        blocking=True, return_response=True,
                    )
                    proxy_url = URL(proxy["url"])
                    assert proxy_url.query["format"] == expected_format
                    assert proxy["media_content_type"] == expected_type
                    async with aiohttp.ClientSession() as session:
                        async with session.head(proxy_url.with_host("127.0.0.1")) as result:
                            assert result.status == 200
                            assert result.headers["Content-Type"] == expected_type
                    await hass.services.async_call("radio_ad_detector", "play", {
                        **data, "entity_id": "media_player.testing",
                    }, blocking=True)
                    assert calls[-1]["media_content_id"] == proxy["url"]
                    assert calls[-1]["media_content_type"] == expected_type

                coordinator = entry.runtime_data.coordinator
                speech = {"available": True, "ad_detected": True, "reason": "speech", "stream": "test", "session_id": "abc"}
                music = {"available": True, "ad_detected": False, "reason": "music", "stream": "other", "session_id": "def"}
                coordinator.async_set_updated_data({**speech, "slots": {1: music, 2: speech}})
                await hass.async_block_till_done()
                legacy = "binary_sensor.radio_ad_detector_ad_detected"
                assert hass.states.get(legacy).state == "on"
                for slot, ad_state, status in ((1, "off", "music"), (2, "on", "speech"), (3, "unavailable", "unavailable")):
                    assert hass.states.get(f"binary_sensor.radio_ad_detector_stream_{slot}_ad_detected").state == ad_state
                    assert hass.states.get(f"sensor.radio_ad_detector_stream_{slot}_audio_status").state == status
                coordinator.async_set_updated_data({"available": False, "slots": {1: music}})
                await hass.async_block_till_done()
                assert hass.states.get(legacy).state == "unavailable"
                assert hass.states.get("binary_sensor.radio_ad_detector_stream_1_ad_detected").state == "off"
                assert hass.states.get("binary_sensor.radio_ad_detector_stream_2_ad_detected").state == "unavailable"
                duplicate = await hass.config_entries.flow.async_init("radio_ad_detector", context={"source": "user"})
                assert duplicate["type"] == "abort" and duplicate["reason"] == "already_configured"
                assert await hass.config_entries.async_unload(entry.entry_id)
                print(json.dumps({"ha_smoke": "passed", "real_native_runtime": models is not None, "checks": ["UI flow", "setup/unload", "native entities", "push availability", "play action", "signed proxy URL response", "duplicate setup"]}))
        finally:
            await hass.async_stop()


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv)>2 else None))
