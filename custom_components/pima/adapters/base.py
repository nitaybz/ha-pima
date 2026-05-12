"""Abstract adapter interface for PIMA panels."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class AdapterError(Exception):
    """Generic adapter error."""


class AdapterConnectError(AdapterError):
    """Raised when the panel cannot be reached."""


class AdapterAuthError(AdapterError):
    """Raised when the access code is rejected."""


@dataclass
class PimaStatus:
    """Canonical status snapshot returned by every adapter.

    partitions: mapping partition number -> one of armed_away/armed_home/armed_night/disarmed.
    open_zones / alarmed_zones / bypassed_zones / failed_zones: 1-based zone numbers.
    failures: set of human-readable system trouble strings.
    logged_in: True if the adapter currently holds an authenticated session.
    """

    partitions: dict[int, str] = field(default_factory=dict)
    open_zones: set[int] = field(default_factory=set)
    alarmed_zones: set[int] = field(default_factory=set)
    bypassed_zones: set[int] = field(default_factory=set)
    failed_zones: set[int] = field(default_factory=set)
    failures: set[str] = field(default_factory=set)
    logged_in: bool = False


class PimaAdapter(ABC):
    """Async-friendly interface every PIMA hardware adapter must implement.

    Implementations should be safe to call from a HA DataUpdateCoordinator;
    blocking I/O must be wrapped in hass.async_add_executor_job by callers.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Open the transport. Raises AdapterConnectError on failure."""

    @abstractmethod
    async def login(self) -> PimaStatus:
        """Authenticate. Raises AdapterAuthError if code is rejected."""

    @abstractmethod
    async def get_status(self) -> PimaStatus:
        """Return the current canonical status."""

    @abstractmethod
    async def arm(self, ha_state: str, partitions: set[int]) -> PimaStatus:
        """Set partitions to the given HA state (armed_away/armed_home/armed_night/disarmed)."""

    async def bypass_zone(self, zone: int) -> None:
        """Toggle the bypass flag for a zone. Default: not supported."""
        raise AdapterError("Bypass not supported by this adapter")

    @abstractmethod
    async def close(self) -> None:
        """Release the transport."""
