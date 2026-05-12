"""Data update coordinator for the PIMA Alarm integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .adapters import AdapterError, PimaAdapter, PimaStatus
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_ALARM_TRIGGERED,
    EVENT_ZONE_TRIGGERED,
)

_LOGGER = logging.getLogger(__name__)


class PimaCoordinator(DataUpdateCoordinator[PimaStatus]):
    """Coordinator that polls the PIMA panel and exposes the latest status."""

    def __init__(
        self,
        hass: HomeAssistant,
        adapter: PimaAdapter,
        entry_id: str,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{entry_id}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.adapter = adapter
        self.last_triggered_zone: int | None = None
        self.last_triggered_at: datetime | None = None
        self._previous_alarmed: set[int] = set()
        self._previous_partitions: dict[int, str] = {}

    async def _async_update_data(self) -> PimaStatus:
        try:
            status = await self.adapter.get_status()
        except AdapterError as err:
            raise UpdateFailed(str(err)) from err

        # Track newly-alarmed zones so we can fire HA events and surface
        # last_triggered_zone in the alarm panel attributes.
        new_alarmed = status.alarmed_zones - self._previous_alarmed
        if new_alarmed:
            self.last_triggered_zone = min(new_alarmed)
            self.last_triggered_at = datetime.now(tz=timezone.utc)
            for zone in new_alarmed:
                self.hass.bus.async_fire(
                    EVENT_ZONE_TRIGGERED,
                    {"zone": zone, "at": self.last_triggered_at.isoformat()},
                )
        if status.alarmed_zones and not self._previous_alarmed:
            self.hass.bus.async_fire(
                EVENT_ALARM_TRIGGERED,
                {
                    "zones": sorted(status.alarmed_zones),
                    "at": (self.last_triggered_at or datetime.now(tz=timezone.utc)).isoformat(),
                },
            )
        self._previous_alarmed = set(status.alarmed_zones)
        self._previous_partitions = dict(status.partitions)
        return status

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.adapter.close()
