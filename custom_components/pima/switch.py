"""Switch platform — exposes Force panel outputs.

Two siren outputs (external/internal, orders 1 + 2) and up to eight
controlled outputs (orders 34–41) per Appendix B of the Force JSON spec.
Only created for modern adapter entries; the legacy net4pro protocol
exposes outputs differently and doesn't have writeable controlled outputs
on every panel.
"""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .adapters import AdapterError
from .adapters.modern import ModernAdapter
from .const import CONF_HARDWARE, DOMAIN, HARDWARE_MODERN
from .coordinator import PimaCoordinator

_LOGGER = logging.getLogger(__name__)

# (order, label)
_SIRENS: tuple[tuple[int, str], ...] = (
    (1, "External siren"),
    (2, "Internal siren"),
)
_CONTROLLED: tuple[tuple[int, str], ...] = tuple(
    (34 + i, f"Output {i + 1}") for i in range(8)
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_HARDWARE) != HARDWARE_MODERN:
        return
    coordinator: PimaCoordinator = hass.data[DOMAIN][entry.entry_id]
    if not isinstance(coordinator.adapter, ModernAdapter):
        return
    entities = [PimaOutputSwitch(coordinator, entry, order, label)
                for order, label in (*_SIRENS, *_CONTROLLED)]
    # Sirens default-disabled — they're rarely something a user wants to
    # toggle from the dashboard and an accidental tap could be alarming.
    async_add_entities(entities)


class PimaOutputSwitch(CoordinatorEntity[PimaCoordinator], SwitchEntity):
    """One output per entity. State follows EVENT 770 (Output activated/deactivated)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PimaCoordinator,
        entry: ConfigEntry,
        order: int,
        label: str,
    ) -> None:
        super().__init__(coordinator)
        self._order = order
        self._attr_unique_id = f"{entry.entry_id}-output-{order}"
        self._attr_name = label
        self._attr_entity_registry_enabled_default = order > 2  # disable sirens by default
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"PIMA Force (listen :{entry.data.get('listen_port')})",
            manufacturer="PIMA Electronic Systems",
            model="Force / Vision",
        )

    @property
    def is_on(self) -> bool:
        adapter = self.coordinator.adapter
        if isinstance(adapter, ModernAdapter):
            return self._order in adapter.outputs_on
        return False

    async def async_turn_on(self, **kwargs) -> None:
        adapter = self.coordinator.adapter
        if not isinstance(adapter, ModernAdapter):
            return
        try:
            await adapter.set_output(self._order, True)
        except AdapterError as err:
            _LOGGER.error("PIMA output %d activate failed: %s", self._order, err)
            raise
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        adapter = self.coordinator.adapter
        if not isinstance(adapter, ModernAdapter):
            return
        try:
            await adapter.set_output(self._order, False)
        except AdapterError as err:
            _LOGGER.error("PIMA output %d deactivate failed: %s", self._order, err)
            raise
        self.async_write_ha_state()
