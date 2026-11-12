"""Common push entity. Keep rapidly changing scores out of recorder history."""
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class RadioEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, entry, key, name, slot=None):
        super().__init__(entry.runtime_data.coordinator)
        self.slot = slot
        if slot is None:
            self._attr_unique_id = f"{entry.entry_id}_{key}"
            self._attr_name = name
        else:
            self._attr_unique_id = f"{entry.entry_id}_stream_{slot}_{key}"
            self._attr_name = f"Stream {slot} {name.lower()}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name="Radio Ad Detector", manufacturer="Local audio analysis")

    @property
    def playback_state(self):
        data = self.coordinator.data or {}
        if self.slot is None:
            return data
        return data.get("slots", {}).get(self.slot, {})

    @property
    def available(self):
        return super().available and bool(self.playback_state.get("available"))

    @property
    def extra_state_attributes(self):
        data = self.playback_state
        return {"stream": data.get("stream"), "session_id": data.get("session_id")}
