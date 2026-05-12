"""PIMA Alarm integration for Home Assistant."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .adapters import (
    AdapterAuthError,
    AdapterConnectError,
    AdapterError,
    build_adapter,
)
from .const import (
    ATTR_ZONE,
    CONF_ACCOUNT,
    CONF_CODE,
    CONF_HARDWARE,
    CONF_HOST,
    CONF_LISTEN_PORT,
    CONF_PARTITIONS,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_ZONES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HARDWARE_LEGACY,
    HARDWARE_MODERN,
    SERVICE_BYPASS_ZONE,
)
from .coordinator import PimaCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
]

_BYPASS_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Required(ATTR_ZONE): vol.All(int, vol.Range(min=1, max=144)),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PIMA from a config entry."""
    data = entry.data
    hardware = data[CONF_HARDWARE]
    try:
        if hardware == HARDWARE_LEGACY:
            adapter = build_adapter(
                HARDWARE_LEGACY,
                host=data[CONF_HOST],
                port=data[CONF_PORT],
                code=data[CONF_CODE],
                zones=data[CONF_ZONES],
                partitions=set(data[CONF_PARTITIONS])
                if data.get(CONF_PARTITIONS)
                else None,
            )
        elif hardware == HARDWARE_MODERN:
            adapter = build_adapter(
                HARDWARE_MODERN,
                listen_port=data[CONF_LISTEN_PORT],
                password=data[CONF_CODE],
                account=data.get(CONF_ACCOUNT),
            )
        else:
            raise AdapterError(f"Unknown hardware family: {hardware}")
    except AdapterError as err:
        raise ConfigEntryNotReady(str(err)) from err

    try:
        await adapter.connect()
        await adapter.login()
    except AdapterAuthError as err:
        await adapter.close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except AdapterConnectError as err:
        await adapter.close()
        raise ConfigEntryNotReady(str(err)) from err
    except AdapterError as err:
        await adapter.close()
        raise ConfigEntryNotReady(str(err)) from err

    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = PimaCoordinator(hass, adapter, entry.entry_id, scan_interval)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    _prune_stale_entities(hass, entry, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _async_register_services(hass)
    return True


def _prune_stale_entities(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: "PimaCoordinator"
) -> None:
    """Remove entities in the registry that this entry no longer creates.

    Runs on every setup, so when the user shrinks the partition list (or the
    zone count) via the options flow — or the modern adapter relearns a
    different installed-zone count — the dropped entities are removed
    instead of lingering as "unavailable".
    """
    # Partitions
    partitions = entry.data.get(CONF_PARTITIONS) or []
    if not partitions and coordinator.data and coordinator.data.partitions:
        partitions = list(coordinator.data.partitions.keys())

    # Zone count
    configured_zones = entry.data.get(CONF_ZONES) or 0
    if configured_zones:
        zone_count = int(configured_zones)
    else:
        zone_count = int(
            getattr(coordinator.adapter, "installed_zones", 0) or 0
        ) or 144

    valid: set[str] = {f"{entry.entry_id}-trouble"}
    for partition in partitions:
        valid.add(f"{entry.entry_id}-partition-{partition}")
    for zone in range(1, zone_count + 1):
        valid.add(f"{entry.entry_id}-zone-{zone}-open")
        valid.add(f"{entry.entry_id}-zone-{zone}-alarm")
    # Force outputs (modern adapter only — sirens + 8 controlled).
    if entry.data.get(CONF_HARDWARE) == HARDWARE_MODERN:
        for order in (1, 2, *range(34, 42)):
            valid.add(f"{entry.entry_id}-output-{order}")

    registry = er.async_get(hass)
    for reg_entry in list(registry.entities.values()):
        if reg_entry.config_entry_id != entry.entry_id:
            continue
        if reg_entry.unique_id not in valid:
            _LOGGER.info(
                "Removing stale entity %s (unique_id %s)",
                reg_entry.entity_id,
                reg_entry.unique_id,
            )
            registry.async_remove(reg_entry.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: PimaCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_BYPASS_ZONE)
    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry whenever options change so scan_interval picks up."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_BYPASS_ZONE):
        return

    async def _bypass_zone(call: ServiceCall) -> None:
        entry_id = call.data["entry_id"]
        zone = call.data[ATTR_ZONE]
        coordinator: PimaCoordinator | None = hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is None:
            raise vol.Invalid(f"Unknown PIMA entry: {entry_id}")
        await coordinator.adapter.bypass_zone(zone)
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_BYPASS_ZONE, _bypass_zone, schema=_BYPASS_SERVICE_SCHEMA
    )
