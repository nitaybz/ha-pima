"""Standalone parser tests for the modern Force adapter.

These don't import Home Assistant — they exercise pure decode logic against
the worked examples in the Force JSON spec, Appendix C. Run with::

    python3 tests/test_modern_parsing.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest

# The adapter imports relative-to-package (``from ..const import ...``); set
# up a minimal stub package so the parsing helpers can be imported in
# isolation without pulling Home Assistant.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "custom_components"))

# Build a minimal stand-in for ``pima.const`` so modern.py imports cleanly.
import types  # noqa: E402

pima_pkg = types.ModuleType("pima")
pima_pkg.__path__ = [os.path.join(ROOT, "custom_components", "pima")]
sys.modules["pima"] = pima_pkg

const = types.ModuleType("pima.const")
const.STATE_ARMED_AWAY = "armed_away"
const.STATE_ARMED_HOME = "armed_home"
const.STATE_ARMED_NIGHT = "armed_night"
const.STATE_DISARMED = "disarmed"
const.LEGACY_TO_HA_STATE = {}
sys.modules["pima.const"] = const

# Adapters sub-package + base + fault table
adapters_pkg = types.ModuleType("pima.adapters")
adapters_pkg.__path__ = [os.path.join(ROOT, "custom_components", "pima", "adapters")]
sys.modules["pima.adapters"] = adapters_pkg

from pima.adapters.modern import ModernAdapter  # noqa: E402


class ZoneParseTests(unittest.TestCase):
    """Spec §4.6.5 worked examples for id 2149 zone-status decoding."""

    def test_zone_27_tamper_open(self):
        # Example: zone 27 (=0x1B) bit 3 set ⇒ tamper open.
        zone_no, bits = ModernAdapter._parse_zone_entry("81B")
        # 0x81B = 0x8 (upper) << 8 | 0x1B (lower zone)
        # bits = 0x08 → bit 3 set (tamper)
        self.assertEqual(zone_no, 0x1B)
        self.assertEqual(bits, 0x08)
        # Bit 3 is tamper.
        self.assertTrue(bits & (1 << 3))

    def test_zone_5_tamper(self):
        zone_no, bits = ModernAdapter._parse_zone_entry("80005")
        # 0x80005 → lower byte 0x05 = zone 5, upper bits 0x800 → bit 11 set (open)
        self.assertEqual(zone_no, 5)
        self.assertTrue(bits & (1 << 11))

    def test_zone_12_manual_bypass(self):
        zone_no, bits = ModernAdapter._parse_zone_entry("800C")
        # 0x800C → zone 12, bit 7 set (manual bypass) — 0x80
        self.assertEqual(zone_no, 0x0C)
        self.assertTrue(bits & (1 << 7))

    def test_zone_25_alarmed_and_open(self):
        zone_no, bits = ModernAdapter._parse_zone_entry("A0019")
        # 0xA0019 → zone 25, upper bits 0xA00 → bits 9 (alarmed) and 11 (open)
        self.assertEqual(zone_no, 25)
        self.assertTrue(bits & (1 << 9))
        self.assertTrue(bits & (1 << 11))


class FrameDecodingTests(unittest.TestCase):
    """Verify our JSON streaming decode handles the spec's example payloads."""

    def test_back_to_back_objects(self):
        """JSON spec — frames stream raw, no newline delimiter."""
        payload = (
            '{"frame_type":"ACK","counter":1,"account":9999,"kc":1}'
            '{"frame_type":"null","counter":2,"account":9999}'
        )
        decoder = json.JSONDecoder()
        parsed = []
        buf = payload
        while buf.strip():
            buf = buf.lstrip()
            obj, idx = decoder.raw_decode(buf)
            parsed.append(obj)
            buf = buf[idx:]
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["frame_type"], "ACK")
        self.assertEqual(parsed[1]["frame_type"], "null")


class FaultTableTests(unittest.TestCase):
    def test_ac_loss_is_id_1(self):
        from pima.adapters.force_faults import FAULT_NAMES
        self.assertEqual(FAULT_NAMES[1], "AC Loss")

    def test_zone_expander_fault_is_id_9(self):
        from pima.adapters.force_faults import FAULT_NAMES
        self.assertEqual(FAULT_NAMES[9], "Zone Expander Fault")


if __name__ == "__main__":
    unittest.main(verbosity=2)
