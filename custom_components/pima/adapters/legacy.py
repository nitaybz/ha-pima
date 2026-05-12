"""Adapter for legacy PIMA Hunter Pro 32 / 96 / 144 panels via net4pro IP gateway.

Wraps the vendored deiger/Alarm protocol implementation. All blocking socket I/O
is offloaded to the HA executor so it doesn't stall the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from ..const import LEGACY_TO_HA_STATE, STATE_DISARMED
from . import _pima_legacy as pima_lib
from .base import (
    AdapterAuthError,
    AdapterConnectError,
    AdapterError,
    PimaAdapter,
    PimaStatus,
)

_LOGGER = logging.getLogger(__name__)


_HA_TO_ARM = {
    "armed_away": pima_lib.Arm.FULL_ARM,
    "armed_home": pima_lib.Arm.HOME1,
    "armed_night": pima_lib.Arm.HOME2,
    "disarmed": pima_lib.Arm.DISARM,
}


class LegacyAdapter(PimaAdapter):
    """net4pro / Hunter Pro 32/96/144 adapter."""

    def __init__(
        self,
        host: str,
        port: int,
        code: str,
        zones: int,
        partitions: Iterable[int] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._code = code
        self._zones = zones
        self._partitions: set[int] | None = set(partitions) if partitions else None
        self._alarm: pima_lib.Alarm | None = None
        self._lock = asyncio.Lock()

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def zones(self) -> int:
        return self._zones

    @property
    def partitions(self) -> set[int]:
        return set(self._partitions or {1})

    async def connect(self) -> None:
        async with self._lock:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        try:
            self._alarm = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: pima_lib.Alarm(
                    zones=self._zones, ipaddr=self._host, ipport=self._port
                ),
            )
        except pima_lib.Error as err:
            raise AdapterConnectError(
                f"Cannot connect to PIMA at {self._host}:{self._port}: {err}"
            ) from err

    async def login(self) -> PimaStatus:
        async with self._lock:
            if self._alarm is None:
                await self._connect_locked()
            try:
                raw = await asyncio.get_running_loop().run_in_executor(
                    None, self._alarm.login, self._code
                )
            except pima_lib.Error as err:
                raise AdapterError(f"Login failed: {err}") from err
        status = _to_status(raw)
        if not status.logged_in:
            raise AdapterAuthError("PIMA panel rejected the access code")
        # Auto-derive partition list on first successful login if caller
        # didn't pin it explicitly.
        if self._partitions is None and status.partitions:
            self._partitions = set(status.partitions.keys())
        return status

    async def get_status(self) -> PimaStatus:
        async with self._lock:
            if self._alarm is None:
                await self._connect_locked()
            try:
                raw = await asyncio.get_running_loop().run_in_executor(
                    None, self._alarm.get_status
                )
            except pima_lib.Error as err:
                # Force reconnect on next call so a dropped socket recovers.
                await self._close_locked()
                raise AdapterError(f"Status read failed: {err}") from err
        status = _to_status(raw)
        if not status.logged_in:
            # Auto re-login if the session lapsed.
            return await self.login()
        return status

    async def arm(self, ha_state: str, partitions: set[int]) -> PimaStatus:
        if ha_state not in _HA_TO_ARM:
            raise AdapterError(f"Unsupported arming state: {ha_state}")
        mode = _HA_TO_ARM[ha_state]
        target_partitions = partitions or self.partitions
        async with self._lock:
            if self._alarm is None:
                await self._connect_locked()
            try:
                raw = await asyncio.get_running_loop().run_in_executor(
                    None, self._alarm.arm, mode, target_partitions
                )
            except pima_lib.Error as err:
                await self._close_locked()
                raise AdapterError(f"Arm command failed: {err}") from err
        return _to_status(raw)

    async def bypass_zone(self, zone: int) -> None:
        """Toggle the bypass flag for a single zone.

        Uses the packet shape reverse-engineered from the RTI legacy driver:
            [length, module_id, 0x01, 0x02, 0x02, zone, 0x00, 0x01]
        deiger's library doesn't expose this, so we call into the underlying
        send helper directly. Same lock as every other call.
        """
        if not 1 <= zone <= self._zones:
            raise AdapterError(f"Zone {zone} out of range (1..{self._zones})")
        async with self._lock:
            if self._alarm is None:
                await self._connect_locked()
            assert self._alarm is not None
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: self._alarm._send_message(  # noqa: SLF001
                        self._alarm._Message.OPEN,  # noqa: SLF001
                        self._alarm._Channel.ZONES,  # noqa: SLF001
                        address=bytes([0x02]),
                        data=bytes([zone, 0x00, 0x01]),
                    ),
                )
            except pima_lib.Error as err:
                await self._close_locked()
                raise AdapterError(f"Bypass command failed: {err}") from err

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        alarm = self._alarm
        self._alarm = None
        if alarm is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(None, alarm._close)  # noqa: SLF001
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Error while closing PIMA socket", exc_info=True)


def _to_status(raw: dict) -> PimaStatus:
    partitions_raw: dict[int, str] = raw.get("partitions") or {}
    partitions = {
        p: LEGACY_TO_HA_STATE.get(name, STATE_DISARMED)
        for p, name in partitions_raw.items()
    }
    return PimaStatus(
        partitions=partitions,
        open_zones=set(raw.get("open zones", set())),
        alarmed_zones=set(raw.get("alarmed zones", set())),
        bypassed_zones=set(raw.get("bypassed zones", set())),
        failed_zones=set(raw.get("failed zones", set())),
        failures=set(raw.get("failures", set())),
        logged_in=bool(raw.get("logged in", False)),
    )


__all__ = ["LegacyAdapter"]
