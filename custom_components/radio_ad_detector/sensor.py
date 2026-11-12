"""Current audio classification."""
from homeassistant.components.sensor import SensorEntity

from .entity import RadioEntity
from .radio_audio.engine import STREAM_SLOTS


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(RadioStatusSensor(entry, slot) for slot in (None, *STREAM_SLOTS))


class RadioStatusSensor(RadioEntity, SensorEntity):
    _attr_icon = "mdi:radio"

    def __init__(self, entry, slot):
        super().__init__(entry, "status", "Audio status", slot)

    @property
    def native_value(self):
        return self.playback_state.get("reason", "idle")
