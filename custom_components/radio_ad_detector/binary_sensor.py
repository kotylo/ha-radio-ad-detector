"""Suspected advertisements, reported directly by the local detector."""
from homeassistant.components.binary_sensor import BinarySensorEntity

from .entity import RadioEntity
from .radio_audio.engine import STREAM_SLOTS


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(RadioAdSensor(entry, slot) for slot in (None, *STREAM_SLOTS))


class RadioAdSensor(RadioEntity, BinarySensorEntity):
    _attr_icon = "mdi:advertisements"

    def __init__(self, entry, slot):
        super().__init__(entry, "ad", "Ad detected", slot)

    @property
    def is_on(self):
        return self.playback_state.get("ad_detected")
