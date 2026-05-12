"""Binary sensors for PIMA zones and system troubles."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .adapters import PimaStatus
from .const import CONF_HOST, CONF_ZONES, DOMAIN
from .coordinator import PimaCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PimaCoordinator = hass.data[DOMAIN][entry.entry_id]
    configured = entry.data.get(CONF_ZONES)
    if configured:
        zone_count = int(configured)
    else:
        # Modern Force path — the adapter learns the installed count from
        # DATA-REQ id 2148 on first connect.
        zone_count = int(getattr(coordinator.adapter, "installed_zones", 0) or 0)
        if zone_count <= 0:
            zone_count = 144  # safe upper bound; user can hide unused via UI

    # Modern adapter exposes zone_names; legacy doesn't (protocol limitation).
    zone_names: dict[int, str] = getattr(coordinator.adapter, "zone_names", {}) or {}

    entities: list[BinarySensorEntity] = []
    for zone in range(1, zone_count + 1):
        label = zone_names.get(zone)
        entities.append(PimaZoneOpenSensor(coordinator, entry, zone, label))
        entities.append(PimaZoneAlarmSensor(coordinator, entry, zone, label))
    entities.append(PimaTroubleSensor(coordinator, entry))
    async_add_entities(entities)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    host = entry.data[CONF_HOST]
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"PIMA {host}",
        manufacturer="PIMA Electronic Systems",
        model="Hunter Pro (legacy)",
        configuration_url=f"http://{host}",
    )


class _PimaBinarySensor(CoordinatorEntity[PimaCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PimaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_device_info = _device_info(entry)


class PimaZoneOpenSensor(_PimaBinarySensor):
    _attr_device_class = BinarySensorDeviceClass.OPENING

    def __init__(
        self,
        coordinator: PimaCoordinator,
        entry: ConfigEntry,
        zone: int,
        label: str | None = None,
    ) -> None:
        super().__init__(coordinator, entry)
        self._zone = zone
        self._attr_unique_id = f"{entry.entry_id}-zone-{zone}-open"
        self._attr_name = label or f"Zone {zone}"

    @property
    def is_on(self) -> bool | None:
        status: PimaStatus | None = self.coordinator.data
        if status is None:
            return None
        return self._zone in status.open_zones


class PimaZoneAlarmSensor(_PimaBinarySensor):
    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: PimaCoordinator,
        entry: ConfigEntry,
        zone: int,
        label: str | None = None,
    ) -> None:
        super().__init__(coordinator, entry)
        self._zone = zone
        self._attr_unique_id = f"{entry.entry_id}-zone-{zone}-alarm"
        self._attr_name = f"{label} alarm" if label else f"Zone {zone} alarm"

    @property
    def is_on(self) -> bool | None:
        status: PimaStatus | None = self.coordinator.data
        if status is None:
            return None
        return self._zone in status.alarmed_zones


class PimaTroubleSensor(_PimaBinarySensor):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: PimaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-trouble"
        self._attr_name = "System trouble"

    @property
    def is_on(self) -> bool | None:
        status: PimaStatus | None = self.coordinator.data
        if status is None:
            return None
        return bool(status.failures)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        status: PimaStatus | None = self.coordinator.data
        if status is None:
            return {}
        return {"failures": sorted(status.failures)}
