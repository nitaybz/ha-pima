"""Alarm control panel entities for PIMA."""

from __future__ import annotations

import logging
import time

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .adapters import AdapterError, PimaStatus
from .const import (
    ATTR_ALARMED_ZONES,
    ATTR_BYPASSED_ZONES,
    ATTR_FAILED_ZONES,
    ATTR_FAILURES,
    ATTR_LAST_TRIGGERED_AT,
    ATTR_LAST_TRIGGERED_ZONE,
    ATTR_OPEN_ZONES,
    CONF_CODE,
    CONF_HOST,
    CONF_PARTITIONS,
    CONF_REQUIRE_CODE,
    DEFAULT_REQUIRE_CODE,
    DOMAIN,
    STATE_ARMED_AWAY,
    STATE_ARMED_HOME,
    STATE_ARMED_NIGHT,
    STATE_DISARMED,
)
from .coordinator import PimaCoordinator

_LOGGER = logging.getLogger(__name__)


_HA_TO_PANEL_STATE = {
    STATE_ARMED_AWAY: AlarmControlPanelState.ARMED_AWAY,
    STATE_ARMED_HOME: AlarmControlPanelState.ARMED_HOME,
    STATE_ARMED_NIGHT: AlarmControlPanelState.ARMED_NIGHT,
    STATE_DISARMED: AlarmControlPanelState.DISARMED,
}

# How long we keep showing ARMING / DISARMING after a command before we
# fall back to whatever the panel actually reports. 90 s covers the
# default Hunter Pro 60 s exit delay with margin for the next poll cycle.
_TRANSITION_BUDGET_SECONDS = 90


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PimaCoordinator = hass.data[DOMAIN][entry.entry_id]
    configured = entry.data.get(CONF_PARTITIONS)
    if configured:
        partitions = sorted(set(configured))
    elif coordinator.data and coordinator.data.partitions:
        # Modern Force path: partitions are auto-learned from id 2310,
        # which already excludes "Partition Not Exist" entries.
        partitions = sorted(coordinator.data.partitions.keys())
    else:
        partitions = [1]
    async_add_entities(
        PimaAlarmPanel(coordinator, entry, partition) for partition in partitions
    )


class PimaAlarmPanel(CoordinatorEntity[PimaCoordinator], AlarmControlPanelEntity):
    """One alarm_control_panel entity per configured partition."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )

    def __init__(
        self, coordinator: PimaCoordinator, entry: ConfigEntry, partition: int
    ) -> None:
        super().__init__(coordinator)
        self._partition = partition
        host = entry.data[CONF_HOST]
        require_code = entry.options.get(CONF_REQUIRE_CODE, DEFAULT_REQUIRE_CODE)
        # When the user wants RTI-style "no code" behaviour we hide the
        # keypad entirely and ignore whatever HA passes to arm/disarm
        # service calls. The stored access code is still used to talk to
        # the panel; this flag only controls the HA-side prompt.
        self._attr_code_format = CodeFormat.NUMBER if require_code else None
        self._attr_code_arm_required = bool(require_code)
        self._require_code = bool(require_code)
        self._expected_code = str(entry.data.get(CONF_CODE) or "")
        self._attr_unique_id = f"{entry.entry_id}-partition-{partition}"
        self._attr_translation_key = "partition"
        self._attr_name = f"Partition {partition}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"PIMA {host}",
            manufacturer="PIMA Electronic Systems",
            model="Hunter Pro (legacy)",
            configuration_url=f"http://{host}",
        )
        # Tracks an in-flight transition so the entity can show ARMING /
        # DISARMING in the UI while the panel runs its exit delay. The
        # legacy protocol has no explicit "in transit" byte, so we manage
        # this client-side with a deadline.
        self._pending_target: AlarmControlPanelState | None = None
        self._pending_until: float = 0.0

    def _confirmed_state(self) -> AlarmControlPanelState | None:
        status: PimaStatus | None = self.coordinator.data
        if status is None:
            return None
        ha_state = status.partitions.get(self._partition)
        return _HA_TO_PANEL_STATE.get(ha_state) if ha_state else None

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        confirmed = self._confirmed_state()
        if self._pending_target is not None:
            now = time.monotonic()
            if confirmed == self._pending_target or now >= self._pending_until:
                self._pending_target = None
                return confirmed
            return (
                AlarmControlPanelState.DISARMING
                if self._pending_target == AlarmControlPanelState.DISARMED
                else AlarmControlPanelState.ARMING
            )
        return confirmed

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        status: PimaStatus | None = self.coordinator.data
        if status is None:
            return {}
        attrs: dict[str, object] = {
            ATTR_OPEN_ZONES: sorted(status.open_zones),
            ATTR_ALARMED_ZONES: sorted(status.alarmed_zones),
            ATTR_BYPASSED_ZONES: sorted(status.bypassed_zones),
            ATTR_FAILED_ZONES: sorted(status.failed_zones),
            ATTR_FAILURES: sorted(status.failures),
        }
        if self.coordinator.last_triggered_zone is not None:
            attrs[ATTR_LAST_TRIGGERED_ZONE] = self.coordinator.last_triggered_zone
        if self.coordinator.last_triggered_at is not None:
            attrs[ATTR_LAST_TRIGGERED_AT] = self.coordinator.last_triggered_at.isoformat()
        return attrs

    def _check_code(self, code: str | None) -> None:
        """Validate the HA-side code prompt when the toggle is on."""
        if not self._require_code:
            return
        if not code or code != self._expected_code:
            raise ValueError("Invalid access code")

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        self._check_code(code)
        await self._send(STATE_DISARMED)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        self._check_code(code)
        await self._send(STATE_ARMED_AWAY)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        self._check_code(code)
        await self._send(STATE_ARMED_HOME)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        self._check_code(code)
        await self._send(STATE_ARMED_NIGHT)

    async def _send(self, ha_state: str) -> None:
        # Show ARMING / DISARMING immediately; only flip to the real state
        # once the coordinator confirms or the budget expires.
        self._pending_target = _HA_TO_PANEL_STATE[ha_state]
        self._pending_until = time.monotonic() + _TRANSITION_BUDGET_SECONDS
        self.async_write_ha_state()
        try:
            await self.coordinator.adapter.arm(ha_state, {self._partition})
        except AdapterError as err:
            _LOGGER.error("PIMA arm command failed: %s", err)
            # Roll back the optimistic state so the UI doesn't lie.
            self._pending_target = None
            self.async_write_ha_state()
            raise
        await self.coordinator.async_request_refresh()
