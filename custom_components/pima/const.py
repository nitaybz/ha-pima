"""Constants for the PIMA Alarm integration."""

from __future__ import annotations

DOMAIN = "pima"

CONF_HARDWARE = "hardware"
CONF_HOST = "host"
CONF_PORT = "port"
CONF_CODE = "code"
CONF_ZONES = "zones"
CONF_PARTITIONS = "partitions"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_REQUIRE_CODE = "require_code"
CONF_LISTEN_PORT = "listen_port"
CONF_ACCOUNT = "account"

DEFAULT_REQUIRE_CODE = False
DEFAULT_LISTEN_PORT = 9999

HARDWARE_LEGACY = "legacy"
HARDWARE_MODERN = "modern"

DEFAULT_PORT = 4001
DEFAULT_SCAN_INTERVAL = 5
MIN_SCAN_INTERVAL = 2
MAX_SCAN_INTERVAL = 60

# "auto" sentinel — config flow probes the panel to figure out the real value.
ZONES_AUTO = 0
ZONE_OPTIONS_WITH_AUTO = (ZONES_AUTO, 32, 96, 144)
ZONE_OPTIONS = (32, 96, 144)

ATTR_FAILURES = "failures"
ATTR_OPEN_ZONES = "open_zones"
ATTR_ALARMED_ZONES = "alarmed_zones"
ATTR_BYPASSED_ZONES = "bypassed_zones"
ATTR_FAILED_ZONES = "failed_zones"
ATTR_LAST_TRIGGERED_ZONE = "last_triggered_zone"
ATTR_LAST_TRIGGERED_AT = "last_triggered_at"

SERVICE_BYPASS_ZONE = "bypass_zone"
ATTR_ZONE = "zone"

EVENT_ALARM_TRIGGERED = f"{DOMAIN}_alarm_triggered"
EVENT_ZONE_TRIGGERED = f"{DOMAIN}_zone_triggered"

# Legacy adapter mode strings (as deiger's library returns them in the
# partitions dict).
ARM_MODE_FULL = "full_arm"
ARM_MODE_HOME1 = "home1"
ARM_MODE_HOME2 = "home2"
ARM_MODE_DISARM = "disarm"

STATE_ARMED_AWAY = "armed_away"
STATE_ARMED_HOME = "armed_home"
STATE_ARMED_NIGHT = "armed_night"
STATE_DISARMED = "disarmed"

LEGACY_TO_HA_STATE = {
    ARM_MODE_FULL: STATE_ARMED_AWAY,
    ARM_MODE_HOME1: STATE_ARMED_HOME,
    ARM_MODE_HOME2: STATE_ARMED_NIGHT,
    ARM_MODE_DISARM: STATE_DISARMED,
}

HA_STATE_TO_LEGACY = {v: k for k, v in LEGACY_TO_HA_STATE.items()}
