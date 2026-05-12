"""Config flow for the PIMA Alarm integration.

Designed to be as self-configuring as the protocol allows:

* Step 1 — hardware family pick (legacy net4pro vs modern Force).
* Step 2 — legacy details. The user may leave *host*, *zones* and *partitions*
  empty; we then:
    - scan the LAN for any PIMA panel (if host is empty),
    - probe the panel's module id to decide 96 vs 144 (if zones = Auto),
    - read the status frame and infer the configured partition list (if
      partitions is empty).
* Step 2a — pick step, only shown when the LAN scan finds more than one panel.

An options flow lets users tune scan interval, partitions, and code without
removing the entry.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .adapters import (
    AdapterAuthError,
    AdapterConnectError,
    AdapterError,
    build_adapter,
)
from .const import (
    CONF_ACCOUNT,
    CONF_CODE,
    CONF_HARDWARE,
    CONF_HOST,
    CONF_LISTEN_PORT,
    CONF_PARTITIONS,
    CONF_PORT,
    CONF_REQUIRE_CODE,
    CONF_SCAN_INTERVAL,
    CONF_ZONES,
    DEFAULT_LISTEN_PORT,
    DEFAULT_PORT,
    DEFAULT_REQUIRE_CODE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HARDWARE_LEGACY,
    HARDWARE_MODERN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    ZONES_AUTO,
    ZONE_OPTIONS,
)
from .discovery import (
    _get_local_ip_blocking,
    async_probe_module_id,
    async_scan_lan,
)

_LOGGER = logging.getLogger(__name__)


_HARDWARE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HARDWARE, default=HARDWARE_LEGACY): SelectSelector(
            SelectSelectorConfig(
                options=[
                    {
                        "value": HARDWARE_LEGACY,
                        "label": "Hunter Pro 32 / 96 / 144 (legacy net4pro)",
                    },
                    {
                        "value": HARDWARE_MODERN,
                        "label": "Hunter Pro 8144 / Force (new — coming soon)",
                    },
                ],
                mode=SelectSelectorMode.LIST,
            )
        )
    }
)


def _legacy_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    zone_options = [{"value": "auto", "label": "Auto-detect"}]
    zone_options += [{"value": str(z), "label": str(z)} for z in ZONE_OPTIONS]
    return vol.Schema(
        {
            vol.Optional(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
            ): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_CODE, default=defaults.get(CONF_CODE, "")): str,
            vol.Required(CONF_ZONES, default=defaults.get(CONF_ZONES, "auto")): SelectSelector(
                SelectSelectorConfig(
                    options=zone_options, mode=SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Optional(
                CONF_PARTITIONS, default=defaults.get(CONF_PARTITIONS, "")
            ): str,
        }
    )


def _modern_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_LISTEN_PORT,
                default=defaults.get(CONF_LISTEN_PORT, DEFAULT_LISTEN_PORT),
            ): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_CODE, default=defaults.get(CONF_CODE, "")): str,
            vol.Optional(CONF_ACCOUNT, default=defaults.get(CONF_ACCOUNT, "")): str,
        }
    )


def _options_schema(entry: ConfigEntry) -> vol.Schema:
    data = {**entry.data, **entry.options}
    return vol.Schema(
        {
            vol.Required(CONF_CODE, default=data.get(CONF_CODE, "")): str,
            vol.Optional(
                CONF_PARTITIONS,
                default=",".join(str(p) for p in data.get(CONF_PARTITIONS, [])),
            ): str,
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_SCAN_INTERVAL,
                    max=MAX_SCAN_INTERVAL,
                    step=1,
                    unit_of_measurement="seconds",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_REQUIRE_CODE,
                default=data.get(CONF_REQUIRE_CODE, DEFAULT_REQUIRE_CODE),
            ): bool,
        }
    )


def _parse_partitions(raw: str) -> set[int]:
    parts: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError as err:
            raise ValueError(f"Invalid partition: {chunk!r}") from err
        if not 1 <= value <= 16:
            raise ValueError(f"Partition out of range (1-16): {value}")
        parts.add(value)
    return parts


class PimaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two-step config flow: pick hardware, then enter connection details."""

    VERSION = 1

    def __init__(self) -> None:
        self._hardware: str | None = None
        self._legacy_pending: dict[str, Any] = {}
        self._scan_results: list[str] = []

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PimaOptionsFlow(entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_HARDWARE_SCHEMA)
        self._hardware = user_input[CONF_HARDWARE]
        if self._hardware == HARDWARE_MODERN:
            return await self.async_step_modern()
        # Auto-scan the LAN *before* showing the legacy form so the host/port
        # fields can be pre-filled. Costs ~3–5 s; users expect a short pause
        # after picking a hardware family.
        _LOGGER.info("Scanning LAN for PIMA panels before showing the form")
        self._scan_results = await async_scan_lan(self.hass)
        if len(self._scan_results) > 1:
            return await self.async_step_pick_panel()
        return await self.async_step_legacy()

    async def async_step_modern(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a Force / Vision panel.

        Force panels are TCP clients — they dial *into* HA, so all we collect
        here is the listen port + the panel's user/installer code. Account
        id is optional; we auto-learn it from the first frame the panel
        sends. Validation (waiting for the panel to actually connect) is
        deferred to ``async_setup_entry`` so the UI stays responsive.
        """
        errors: dict[str, str] = {}
        if user_input is None:
            ha_ip = await self.hass.async_add_executor_job(_get_local_ip_blocking)
            placeholders = {"ha_ip": ha_ip or "<this HA host IP>"}
            return self.async_show_form(
                step_id="modern",
                data_schema=_modern_schema(),
                description_placeholders=placeholders,
                errors=errors,
            )

        try:
            listen_port = int(user_input[CONF_LISTEN_PORT])
        except (KeyError, TypeError, ValueError):
            errors[CONF_LISTEN_PORT] = "invalid_input"
        else:
            if not 1 <= listen_port <= 65535:
                errors[CONF_LISTEN_PORT] = "invalid_input"

        password = (user_input.get(CONF_CODE) or "").strip()
        if not password:
            errors[CONF_CODE] = "invalid_input"

        account_raw = (user_input.get(CONF_ACCOUNT) or "").strip()
        account: int | None = None
        if account_raw:
            try:
                account = int(account_raw)
            except ValueError:
                errors[CONF_ACCOUNT] = "invalid_input"

        if errors:
            ha_ip = await self.hass.async_add_executor_job(_get_local_ip_blocking)
            return self.async_show_form(
                step_id="modern",
                data_schema=_modern_schema(user_input),
                description_placeholders={"ha_ip": ha_ip or "<this HA host IP>"},
                errors=errors,
            )

        unique_id = f"{HARDWARE_MODERN}:{listen_port}"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        data: dict[str, Any] = {
            CONF_HARDWARE: HARDWARE_MODERN,
            CONF_LISTEN_PORT: listen_port,
            CONF_CODE: password,
        }
        if account is not None:
            data[CONF_ACCOUNT] = account
        return self.async_create_entry(
            title=f"PIMA Force (listen :{listen_port})",
            data=data,
            options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
        )

    async def async_step_legacy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is None:
            # Pre-fill host + port if the LAN scan turned up exactly one panel.
            defaults: dict[str, Any] = {}
            if len(self._scan_results) == 1:
                host, port = self._scan_results[0]
                defaults = {CONF_HOST: host, CONF_PORT: port}
            return self.async_show_form(
                step_id="legacy", data_schema=_legacy_schema(defaults), errors=errors
            )

        host = (user_input.get(CONF_HOST) or "").strip()
        port = int(user_input[CONF_PORT])
        code = user_input[CONF_CODE].strip()
        zones_raw = user_input[CONF_ZONES]

        try:
            partitions = _parse_partitions(user_input.get(CONF_PARTITIONS, ""))
        except ValueError as err:
            _LOGGER.warning("Bad partitions input: %s", err)
            return self.async_show_form(
                step_id="legacy",
                data_schema=_legacy_schema(user_input),
                errors={"base": "invalid_partitions"},
            )

        # ─── LAN scan if host is blank ────────────────────────────────────
        if not host:
            _LOGGER.info("Host left blank — scanning LAN for PIMA panels")
            # User-entered port is the preferred one; everything else gets
            # tried as a fallback.
            self._scan_results = await async_scan_lan(self.hass, extra_ports=(port,))
            if not self._scan_results:
                return self.async_show_form(
                    step_id="legacy",
                    data_schema=_legacy_schema(user_input),
                    errors={CONF_HOST: "scan_no_panel"},
                )
            if len(self._scan_results) > 1:
                self._legacy_pending = {
                    CONF_CODE: code,
                    CONF_ZONES: zones_raw,
                    CONF_PARTITIONS: ",".join(str(p) for p in sorted(partitions)),
                }
                return await self.async_step_pick_panel()
            host, port = self._scan_results[0]
            _LOGGER.info("Single panel found at %s:%d, using it", host, port)

        # ─── Auto-detect zones if requested ───────────────────────────────
        if zones_raw == "auto":
            probed = await async_probe_module_id(host, port)
            if probed is None:
                _LOGGER.warning("Module-id probe failed for %s:%s", host, port)
                return self.async_show_form(
                    step_id="legacy",
                    data_schema=_legacy_schema(
                        {**user_input, CONF_HOST: host}
                    ),
                    errors={"base": "cannot_connect"},
                )
            zones = probed
            _LOGGER.info("Auto-detected %d zones at %s", zones, host)
        else:
            zones = int(zones_raw)

        unique_id = f"{HARDWARE_LEGACY}:{host}:{port}"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        # ─── Validate by connecting + logging in + reading status ─────────
        adapter = None
        try:
            _LOGGER.debug(
                "Validating panel: host=%s port=%s zones=%s partitions=%s",
                host, port, zones, partitions or "(auto)",
            )
            adapter = build_adapter(
                HARDWARE_LEGACY,
                host=host,
                port=port,
                code=code,
                zones=zones,
                partitions=partitions or None,
            )
            _LOGGER.debug("Adapter built; connecting...")
            await adapter.connect()
            _LOGGER.debug("Connected; logging in...")
            await adapter.login()
            _LOGGER.debug("Logged in; reading status...")
            status = await adapter.get_status()
            _LOGGER.debug("Status: partitions=%s open=%s", status.partitions, status.open_zones)
            if not partitions:
                # PIMA legacy protocol limitation: an unconfigured partition
                # and a real-but-currently-disarmed partition both report
                # 0x00, so we can't reliably distinguish from a single read.
                # If anything is non-disarmed we trust that as the live list;
                # otherwise default to {1} (single-partition home, the
                # overwhelmingly common case) and let the user extend the
                # list via the options flow if they have more.
                non_disarmed = {
                    p for p, s in status.partitions.items() if s != "disarmed"
                }
                partitions = non_disarmed or {1}
                _LOGGER.info(
                    "Auto-detected partitions: %s%s",
                    sorted(partitions),
                    " (single-partition default; edit in options if multi-partition)"
                    if not non_disarmed
                    else "",
                )
        except AdapterConnectError as err:
            _LOGGER.warning("Connect failed: %s", err)
            errors["base"] = "cannot_connect"
        except AdapterAuthError as err:
            _LOGGER.warning("Auth failed: %s", err)
            errors[CONF_CODE] = "invalid_auth"
        except AdapterError as err:
            _LOGGER.warning("Adapter error: %s", err)
            errors["base"] = "unknown"
        except Exception:  # noqa: BLE001 — catch-all so we see what's escaping
            _LOGGER.exception("Unexpected exception during config validation")
            errors["base"] = "unknown"
        finally:
            if adapter is not None:
                await adapter.close()

        if errors:
            return self.async_show_form(
                step_id="legacy",
                data_schema=_legacy_schema({**user_input, CONF_HOST: host}),
                errors=errors,
            )

        return self.async_create_entry(
            title=f"PIMA {host}",
            data={
                CONF_HARDWARE: HARDWARE_LEGACY,
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_CODE: code,
                CONF_ZONES: zones,
                CONF_PARTITIONS: sorted(partitions),
            },
            options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
        )

    async def async_step_pick_panel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="pick_panel",
                data_schema=vol.Schema(
                    {
                        vol.Required("target"): SelectSelector(
                            SelectSelectorConfig(
                                options=[
                                    {
                                        "value": f"{ip}:{port}",
                                        "label": f"{ip}:{port}",
                                    }
                                    for ip, port in self._scan_results
                                ],
                                mode=SelectSelectorMode.LIST,
                            )
                        )
                    }
                ),
            )
        host_str, port_str = user_input["target"].rsplit(":", 1)
        merged = {
            **self._legacy_pending,
            CONF_HOST: host_str,
            CONF_PORT: int(port_str),
        }
        return await self.async_step_legacy(merged)


class PimaOptionsFlow(OptionsFlow):
    """Edit code / partitions / scan interval without removing the entry."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="init", data_schema=_options_schema(self._entry)
            )
        try:
            partitions = _parse_partitions(user_input.get(CONF_PARTITIONS, ""))
        except ValueError:
            return self.async_show_form(
                step_id="init",
                data_schema=_options_schema(self._entry),
                errors={CONF_PARTITIONS: "invalid_partitions"},
            )
        new_options = {
            CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
            CONF_REQUIRE_CODE: bool(user_input.get(CONF_REQUIRE_CODE, DEFAULT_REQUIRE_CODE)),
        }
        new_data = {
            **self._entry.data,
            CONF_CODE: user_input[CONF_CODE],
        }
        if partitions:
            new_data[CONF_PARTITIONS] = sorted(partitions)
        self.hass.config_entries.async_update_entry(
            self._entry, data=new_data, options=new_options
        )
        return self.async_create_entry(title="", data=new_options)
