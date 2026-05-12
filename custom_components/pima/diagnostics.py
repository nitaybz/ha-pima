"""Diagnostics support for the PIMA Alarm integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .adapters import PimaStatus
from .const import CONF_CODE, CONF_HOST, DOMAIN
from .coordinator import PimaCoordinator

REDACT_DATA = {CONF_CODE, CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: PimaCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    status: PimaStatus | None = coordinator.data if coordinator else None
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), REDACT_DATA),
            "options": dict(entry.options),
            "title": entry.title,
            "version": entry.version,
        },
        "runtime": {
            "loaded": coordinator is not None,
            "last_update_success": bool(coordinator and coordinator.last_update_success),
            "last_triggered_zone": (
                coordinator.last_triggered_zone if coordinator else None
            ),
            "last_triggered_at": (
                coordinator.last_triggered_at.isoformat()
                if coordinator and coordinator.last_triggered_at
                else None
            ),
            "status": (
                {
                    "partitions": status.partitions,
                    "open_zones": sorted(status.open_zones),
                    "alarmed_zones": sorted(status.alarmed_zones),
                    "bypassed_zones": sorted(status.bypassed_zones),
                    "failed_zones": sorted(status.failed_zones),
                    "failures": sorted(status.failures),
                    "logged_in": status.logged_in,
                }
                if status is not None
                else None
            ),
        },
    }
