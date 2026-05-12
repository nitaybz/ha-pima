"""Hardware adapters for the PIMA Alarm integration.

Each adapter implements PimaAdapter for a specific PIMA panel family:
- legacy: Hunter Pro 32/96/144 via net4pro IP gateway (deiger protocol)
- modern: Newer Hunter Pro / Force panels (TBD, awaiting protocol)
"""

from __future__ import annotations

from ..const import HARDWARE_LEGACY, HARDWARE_MODERN
from .base import PimaAdapter, PimaStatus, AdapterError, AdapterAuthError, AdapterConnectError


def build_adapter(hardware: str, **kwargs) -> PimaAdapter:
    """Construct the right adapter for the chosen hardware."""
    if hardware == HARDWARE_LEGACY:
        from .legacy import LegacyAdapter

        return LegacyAdapter(**kwargs)
    if hardware == HARDWARE_MODERN:
        from .modern import ModernAdapter

        return ModernAdapter(**kwargs)
    raise ValueError(f"Unknown hardware type: {hardware}")


__all__ = [
    "build_adapter",
    "PimaAdapter",
    "PimaStatus",
    "AdapterError",
    "AdapterAuthError",
    "AdapterConnectError",
]
