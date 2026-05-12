"""Adapter for modern PIMA Force / Vision panels (JSON-over-TCP protocol).

Spec: "Force Interface — JSON Format Specification" Ver. 2.1 (PIMA Electronic
Systems Ltd). Key differences from the legacy Hunter Pro net4pro protocol:

* **Direction reversed.** The alarm system (AS) connects to Home Assistant.
  HA must listen on a static-IP TCP port; the panel dials in. We use
  ``asyncio.start_server`` and treat the active connection as our single
  control channel.
* **Stateless REST.** Each :data:`OPERATION` frame carries the password
  in-line; there is no login session.
* **JSON framing.** Frames are raw JSON objects sent back-to-back with no
  length prefix or newline delimiter. We use :class:`json.JSONDecoder.raw_decode`
  on a sliding buffer to split them.
* **Pagination.** A DATA response that exceeds 250 bytes is split into
  multiple frames marked ``"more":"yes"``; we re-request from
  ``last_order + 1`` until the marker is absent.
* **kc=1 keepalive.** Every ACK we send carries ``"kc":1`` so the AS keeps
  the TCP connection open instead of disconnecting after draining its
  event buffer.

This module owns the lifecycle of the listener + the active connection,
exposes the same :class:`PimaAdapter` surface as the legacy adapter, and
maps Force events / status to the canonical :class:`PimaStatus` so the
coordinator and entity layer above don't need to care which family the
panel is from.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Iterable

from ..const import (
    LEGACY_TO_HA_STATE,
    STATE_ARMED_AWAY,
    STATE_ARMED_HOME,
    STATE_ARMED_NIGHT,
    STATE_DISARMED,
)
from .base import (
    AdapterAuthError,
    AdapterConnectError,
    AdapterError,
    PimaAdapter,
    PimaStatus,
)

_LOGGER = logging.getLogger(__name__)


# Operation types — Appendix B of the Force JSON spec.
_OPTYPE = {
    "armed_away": 12,   # Full arming
    "armed_home": 13,   # Home1 arming
    "armed_night": 14,  # Home2 arming
    "disarmed": 17,
}
_OUTPUT_ACTIVATE = 35
_OUTPUT_DEACTIVATE = 36

# Parameter IDs — Appendix C.
_ID_SYSTEM_KEY_STATUS = 2310
_ID_ZONE_STATUS = 2149
_ID_INSTALLED_ZONES = 2148
_ID_FAULTS = 2250
_ID_ZONE_NAMES = 260
_ID_USER_NAMES = 411

# Per-partition arming codes from id 2310 (Appendix C).
_PARTITION_STATE = {
    1: None,            # "Partition Not Exist"
    2: STATE_DISARMED,
    3: STATE_ARMED_AWAY,    # Full Armed
    4: STATE_ARMED_HOME,    # Home1
    5: STATE_ARMED_NIGHT,   # Home2 → night
    6: STATE_ARMED_HOME,    # Home3 → home (no native HA equivalent)
    7: STATE_ARMED_HOME,    # Home4 → home
    8: STATE_ARMED_AWAY,    # Shabbat ON  → treat as armed
    9: STATE_DISARMED,      # Shabbat OFF → disarmed
}

# Zone status bit positions inside the upper bytes of id 2149 (Appendix C).
_ZONE_BIT_LOW_BATTERY = 1
_ZONE_BIT_MANUAL_BYPASS = 7
_ZONE_BIT_AUTO_BYPASS = 8
_ZONE_BIT_ALARMED = 9
_ZONE_BIT_OPEN = 11
_ZONE_BIT_TAMPER = 3
_ZONE_BIT_FIRE = 13

_FRAME_SIZE_LIMIT = 250         # spec §4.6.5 (HA→AS direction)
_CONNECT_TIMEOUT_SECONDS = 60   # how long to wait for the panel to dial in
_REQUEST_TIMEOUT_SECONDS = 10   # ACK/DATA response budget per outbound request
_COUNTER_ROLLOVER = 10_000      # spec §4.4 — rolls over after 9999


class ModernAdapter(PimaAdapter):
    """Force / Vision adapter — HA acts as TCP server for the panel."""

    def __init__(
        self,
        listen_port: int,
        password: str,
        account: int | None = None,
        listen_host: str = "0.0.0.0",
    ) -> None:
        self._listen_port = listen_port
        self._listen_host = listen_host
        self._password = password
        self._account = account  # may be None — auto-filled from first frame
        self._server: asyncio.AbstractServer | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connection_event = asyncio.Event()
        self._counter = 1
        self._cached_status = PimaStatus(logged_in=False)
        self._zone_count = 0
        self._zone_names: dict[int, str] = {}
        self._user_names: dict[int, str] = {}
        # Output activation state cached from EVENT 770 (HA can't reliably
        # query it; the panel reports activate/deactivate events).
        self._outputs_on: set[int] = set()
        # Pending ACK/NAK/DATA waiters keyed by the counter we sent.
        self._waiters: dict[int, asyncio.Future[dict]] = {}
        self._reader_task: asyncio.Task | None = None

    # ─── PimaAdapter surface ──────────────────────────────────────────────

    async def connect(self) -> None:
        """Open the listener and wait for the panel to dial in."""
        try:
            self._server = await asyncio.start_server(
                self._handle_client, host=self._listen_host, port=self._listen_port
            )
        except OSError as err:
            raise AdapterConnectError(
                f"Couldn't bind TCP port {self._listen_port}: {err}"
            ) from err
        _LOGGER.info(
            "Force adapter listening on %s:%d — waiting for panel to connect",
            self._listen_host, self._listen_port,
        )
        try:
            await asyncio.wait_for(
                self._connection_event.wait(), timeout=_CONNECT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as err:
            await self._teardown_server()
            raise AdapterConnectError(
                "Force panel didn't connect within "
                f"{_CONNECT_TIMEOUT_SECONDS}s — verify its network config "
                "points at this Home Assistant host."
            ) from err

    async def login(self) -> PimaStatus:
        """Force is stateless — there's no login frame. We validate by
        requesting a status read and seeing if the panel ACKs. While we're
        at it, pull the configuration data (zone names, user names) so the
        entity layer can use real labels."""
        status = await self.get_status()
        # One-shot fetches; failures here are non-fatal — the panel is
        # working, we just don't get pretty names.
        try:
            await self.fetch_zone_names()
        except AdapterError as err:
            _LOGGER.debug("Couldn't fetch zone names: %s", err)
        try:
            await self.fetch_user_names()
        except AdapterError as err:
            _LOGGER.debug("Couldn't fetch user names: %s", err)
        return status

    async def get_status(self) -> PimaStatus:
        """Pull live status: partitions, zones, faults."""
        if self._writer is None:
            raise AdapterError("Panel is not connected")
        try:
            partitions_data = await self._send_data_req(
                _ID_SYSTEM_KEY_STATUS, start_order=1, stop_order=16
            )
            zones_data = await self._send_data_req(
                _ID_ZONE_STATUS, start_order=1, collect_more=True
            )
            faults_data = await self._send_data_req(
                _ID_FAULTS, start_order=1, collect_more=True
            )
            if self._zone_count == 0:
                installed = await self._send_data_req(
                    _ID_INSTALLED_ZONES, start_order=1, stop_order=1
                )
                if installed and installed[0]:
                    try:
                        self._zone_count = int(installed[0])
                    except (TypeError, ValueError):
                        pass
        except AdapterAuthError:
            raise
        except AdapterError:
            raise
        status = self._parse_status(partitions_data, zones_data, faults_data)
        self._cached_status = status
        return status

    async def arm(self, ha_state: str, partitions: set[int]) -> PimaStatus:
        if ha_state not in _OPTYPE:
            raise AdapterError(f"Unsupported arming state: {ha_state}")
        if not partitions:
            partitions = {0}  # 0 = all partitions per spec §4.6.1
        optype = _OPTYPE[ha_state]
        for partition in sorted(partitions):
            await self._send_operation(
                optype=optype, opclass=1, partition=partition, order=1
            )
        return await self.get_status()

    async def bypass_zone(self, zone: int) -> None:
        """Toggle the manual-bypass bit (bit 7) for ``zone`` in id 2149.

        Force has no one-shot bypass operation — per spec §4.6.5 ("Setting
        bit by HA") we read the current zone status bytes, flip bit 7, and
        write them back as a DATA frame (HA→AS). The panel ACK/NAK's the
        write like any other operation.
        """
        if not 1 <= zone <= 144:
            raise AdapterError(f"Zone {zone} out of range (1..144)")
        # Read current status for just this zone.
        current = await self._send_data_req(
            _ID_ZONE_STATUS, start_order=zone, stop_order=zone
        )
        new_bits = 1 << _ZONE_BIT_MANUAL_BYPASS  # default: just set bypass
        for entry in current:
            zone_no, bits = self._parse_zone_entry(entry)
            if zone_no == zone:
                # Toggle the bypass bit rather than always set, so callers
                # can use the same service to clear it.
                bits ^= 1 << _ZONE_BIT_MANUAL_BYPASS
                new_bits = bits
                break
        # Re-encode: 4 hex bytes, lower byte = zone number, upper bytes = bits.
        encoded = f"{(new_bits << 8 | zone) & 0xFFFFFFFF:X}"
        await self._send_data_set(_ID_ZONE_STATUS, start_order=zone, parameters=[encoded])

    async def close(self) -> None:
        await self._teardown_server()

    @property
    def installed_zones(self) -> int:
        return self._zone_count

    @property
    def zone_names(self) -> dict[int, str]:
        return dict(self._zone_names)

    @property
    def user_names(self) -> dict[int, str]:
        return dict(self._user_names)

    @property
    def outputs_on(self) -> set[int]:
        return set(self._outputs_on)

    async def set_output(self, order: int, on: bool) -> None:
        """Activate (35) or deactivate (36) a Force output.

        ``order`` values (Appendix B): 1 = External siren, 2 = Internal siren,
        34–41 = Controlled outputs 1–8.
        """
        if order not in (1, 2) and not 34 <= order <= 41:
            raise AdapterError(
                f"Output order {order} out of range (1/2 sirens, 34-41 controlled)"
            )
        await self._send_operation(
            optype=_OUTPUT_ACTIVATE if on else _OUTPUT_DEACTIVATE,
            opclass=1,
            partition=0,
            order=order,
        )
        if on:
            self._outputs_on.add(order)
        else:
            self._outputs_on.discard(order)

    async def fetch_zone_names(self) -> dict[int, str]:
        """Pull all zone names from id 260 (paginated).

        Called once on connect — the panel's labels become the entity names
        in HA, which is far nicer than ``zone_5_open``.
        """
        if not self._zone_count:
            return {}
        names = await self._send_data_req(
            _ID_ZONE_NAMES,
            start_order=1,
            stop_order=self._zone_count,
            collect_more=True,
        )
        for idx, name in enumerate(names, start=1):
            cleaned = (name or "").strip()
            if cleaned:
                self._zone_names[idx] = cleaned
        return dict(self._zone_names)

    async def fetch_user_names(self) -> dict[int, str]:
        names = await self._send_data_req(
            _ID_USER_NAMES,
            start_order=1,
            stop_order=32,
            collect_more=True,
        )
        for idx, name in enumerate(names, start=1):
            cleaned = (name or "").strip()
            if cleaned:
                self._user_names[idx] = cleaned
        return dict(self._user_names)

    # ─── Connection management ────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Single active client only. New connections replace the old."""
        peer = writer.get_extra_info("peername")
        _LOGGER.info("Force panel connected from %s", peer)
        if self._writer is not None:
            _LOGGER.warning(
                "Replacing existing Force connection from %s",
                self._writer.get_extra_info("peername"),
            )
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (asyncio.TimeoutError, OSError):
                pass
        self._reader = reader
        self._writer = writer
        self._connection_event.set()
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        """Parse incoming JSON frames using JSONDecoder + a sliding buffer."""
        assert self._reader is not None
        decoder = json.JSONDecoder()
        buf = ""
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    _LOGGER.info("Force panel closed the connection")
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while buf:
                    buf = buf.lstrip()  # drop leading whitespace between objects
                    if not buf:
                        break
                    try:
                        obj, idx = decoder.raw_decode(buf)
                    except json.JSONDecodeError:
                        # Need more bytes to finish the object.
                        break
                    buf = buf[idx:]
                    await self._on_frame(obj)
        except (asyncio.CancelledError, ConnectionError):
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Force reader loop crashed")
        finally:
            await self._fail_pending(AdapterError("Connection lost"))
            self._reader = None
            self._writer = None

    async def _fail_pending(self, err: Exception) -> None:
        for fut in self._waiters.values():
            if not fut.done():
                fut.set_exception(err)
        self._waiters.clear()

    async def _teardown_server(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (asyncio.TimeoutError, OSError):
                pass
            self._writer = None
            self._reader = None
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except (asyncio.TimeoutError, OSError):
                pass
            self._server = None
        self._connection_event.clear()

    # ─── Frame handling ───────────────────────────────────────────────────

    async def _on_frame(self, frame: dict) -> None:
        _LOGGER.debug("AS→HA frame: %s", frame)
        # Auto-learn the panel's account id from the first frame that has it.
        if self._account is None and "account" in frame:
            self._account = int(frame["account"])
            _LOGGER.info("Learned Force account id: %d", self._account)

        ftype = (frame.get("frame_type") or "").upper()
        counter = frame.get("counter")

        if ftype in ("EVENT", "NULL"):
            # Per spec §4.5.2 every event must be ACKed with kc=1 to keep
            # the connection open.
            if counter is not None:
                await self._send_ack(counter)
            if ftype == "EVENT":
                self._apply_event(frame)
            return

        if ftype in ("ACK", "NAK", "DATA"):
            if counter is not None and counter in self._waiters:
                fut = self._waiters.pop(counter)
                if not fut.done():
                    fut.set_result(frame)
            elif ftype == "DATA":
                # DATA frames can arrive unsolicited as configuration pushes
                # from HA→AS direction (spec §4.6.5). For now just log.
                _LOGGER.debug("Unsolicited DATA frame: %s", frame)
            return

        _LOGGER.debug("Unrecognised frame_type=%r", ftype)

    def _apply_event(self, frame: dict) -> None:
        """Patch our cached status from a CID-format event."""
        cid_type = frame.get("type")
        qualifier = frame.get("qualifier")
        zone = frame.get("zone")
        # Zone open (760) / closed.
        if cid_type == 760 and isinstance(zone, int):
            if qualifier == 1:
                self._cached_status.open_zones.add(zone)
            elif qualifier == 3:
                self._cached_status.open_zones.discard(zone)
        # Burglary alarm (130).
        if cid_type == 130 and isinstance(zone, int):
            if qualifier == 1:
                self._cached_status.alarmed_zones.add(zone)
            elif qualifier == 3:
                self._cached_status.alarmed_zones.discard(zone)
        # Output 770 — order = output number (1–5 onboard per spec).
        if cid_type == 770 and isinstance(zone, int):
            if qualifier == 1:
                self._outputs_on.add(zone)
            elif qualifier == 3:
                self._outputs_on.discard(zone)

    # ─── Sending ──────────────────────────────────────────────────────────

    def _next_counter(self) -> int:
        c = self._counter
        self._counter = (self._counter % _COUNTER_ROLLOVER) + 1
        return c

    async def _send_frame(self, frame: dict) -> None:
        if self._writer is None:
            raise AdapterError("No active connection to the Force panel")
        encoded = json.dumps(frame, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _FRAME_SIZE_LIMIT:
            raise AdapterError(
                f"Outgoing frame is {len(encoded)} bytes; spec caps HA→AS at "
                f"{_FRAME_SIZE_LIMIT}"
            )
        _LOGGER.debug("HA→AS frame: %s", frame)
        self._writer.write(encoded)
        await self._writer.drain()

    async def _send_ack(self, counter: int) -> None:
        if self._account is None:
            return  # can't ACK without an account id
        await self._send_frame(
            {
                "frame_type": "ACK",
                "counter": counter,
                "account": self._account,
                "kc": 1,
            }
        )

    async def _send_operation(
        self,
        *,
        optype: int,
        opclass: int,
        partition: int,
        order: int | None = None,
        parameters: list | None = None,
    ) -> dict:
        if self._account is None:
            raise AdapterError("Panel hasn't reported its account id yet")
        counter = self._next_counter()
        frame: dict[str, Any] = {
            "frame_type": "OPERATION",
            "counter": counter,
            "account": self._account,
            "password": self._password,
            "optype": optype,
            "opclass": opclass,
            "partition": partition,
        }
        if order is not None:
            frame["order"] = order
        if parameters is not None:
            frame["parameters"] = parameters
        return await self._send_and_wait(counter, frame)

    async def _send_data_set(
        self,
        ident: int,
        *,
        start_order: int,
        parameters: list[str],
    ) -> dict:
        """Write configuration / status bits back to the panel.

        Spec §4.6.5 calls this a ``DATA`` frame (Table 1) but the revision
        history note for v1.3 mentions ``data_set``. Implementations in the
        wild use ``DATA`` so we go with that and include the password.
        """
        if self._account is None:
            raise AdapterError("Panel hasn't reported its account id yet")
        counter = self._next_counter()
        frame: dict[str, Any] = {
            "frame_type": "DATA",
            "counter": counter,
            "account": self._account,
            "password": self._password,
            "id": ident,
            "start_order": start_order,
            "parameters": parameters,
        }
        return await self._send_and_wait(counter, frame)

    async def _send_data_req(
        self,
        ident: int,
        *,
        start_order: int = 1,
        stop_order: int | None = None,
        collect_more: bool = False,
    ) -> list[str]:
        """Send DATA-REQ and reassemble paginated DATA responses."""
        if self._account is None:
            raise AdapterError("Panel hasn't reported its account id yet")
        collected: list[str] = []
        current_start = start_order
        while True:
            counter = self._next_counter()
            frame: dict[str, Any] = {
                "frame_type": "DATA-REQ",
                "counter": counter,
                "account": self._account,
                "password": self._password,
                "id": ident,
                "start_order": current_start,
            }
            if stop_order is not None:
                frame["stop_order"] = stop_order
            response = await self._send_and_wait(counter, frame)
            params = response.get("parameters") or []
            if isinstance(params, list):
                collected.extend(params)
            if not collect_more or response.get("more") != "yes":
                break
            current_start = current_start + len(params)
        return collected

    async def _send_and_wait(self, counter: int, frame: dict) -> dict:
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._waiters[counter] = future
        try:
            await self._send_frame(frame)
            response = await asyncio.wait_for(future, timeout=_REQUEST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as err:
            self._waiters.pop(counter, None)
            raise AdapterError(
                f"Force panel didn't respond to counter={counter} within "
                f"{_REQUEST_TIMEOUT_SECONDS}s"
            ) from err
        ftype = (response.get("frame_type") or "").upper()
        if ftype == "NAK":
            reason = response.get("DATA") or response.get("data") or "unknown"
            if "password" in str(reason).lower() or "code" in str(reason).lower():
                raise AdapterAuthError(f"Force panel NAK: {reason}")
            raise AdapterError(f"Force panel NAK: {reason}")
        return response

    # ─── Parsing ──────────────────────────────────────────────────────────

    def _parse_status(
        self,
        partitions_raw: list[str],
        zones_raw: list[str],
        faults_raw: list[str],
    ) -> PimaStatus:
        """Build the canonical :class:`PimaStatus` from three DATA replies."""
        partitions: dict[int, str] = {}
        for idx, value in enumerate(partitions_raw, start=1):
            try:
                key = int(value)
            except (TypeError, ValueError):
                continue
            mapped = _PARTITION_STATE.get(key)
            if mapped is not None:
                partitions[idx] = mapped

        open_zones: set[int] = set()
        alarmed: set[int] = set()
        bypassed: set[int] = set()
        failed: set[int] = set()
        for entry in zones_raw:
            zone_no, bits = self._parse_zone_entry(entry)
            if zone_no is None:
                continue
            if bits & (1 << _ZONE_BIT_OPEN):
                open_zones.add(zone_no)
            if bits & (1 << _ZONE_BIT_ALARMED):
                alarmed.add(zone_no)
            if bits & ((1 << _ZONE_BIT_MANUAL_BYPASS) | (1 << _ZONE_BIT_AUTO_BYPASS)):
                bypassed.add(zone_no)
            if bits & ((1 << _ZONE_BIT_TAMPER) | (1 << _ZONE_BIT_LOW_BATTERY)):
                failed.add(zone_no)

        faults: set[str] = set()
        for entry in faults_raw:
            label = self._parse_fault_entry(entry)
            if label:
                faults.add(label)

        return PimaStatus(
            partitions=partitions,
            open_zones=open_zones,
            alarmed_zones=alarmed,
            bypassed_zones=bypassed,
            failed_zones=failed,
            failures=faults,
            logged_in=True,
        )

    @staticmethod
    def _parse_zone_entry(entry: str) -> tuple[int | None, int]:
        """Decode the hex string format from id 2149.

        Spec §4.6.5 Zone Status: up to 4 hex bytes. The lower byte is the
        zone number; the upper bytes carry status bits (open, bypass etc.).
        Example: ``"08003"`` → zone 0x03, status bits 0x080 → bit 7 (manual
        bypass).
        """
        if not entry:
            return None, 0
        try:
            value = int(entry, 16)
        except ValueError:
            return None, 0
        zone_no = value & 0xFF
        bits = value >> 8
        return (zone_no or None), bits

    @staticmethod
    def _parse_fault_entry(entry: str) -> str | None:
        """Decode id 2250 — first byte = fault id, second = order."""
        from .force_faults import FAULT_NAMES  # local table, see below

        if not entry:
            return None
        try:
            value = int(entry, 16)
        except ValueError:
            return None
        fault_id = value & 0xFF
        order = (value >> 8) & 0xFF
        name = FAULT_NAMES.get(fault_id, f"Fault {fault_id}")
        return f"{name} (order {order})" if order else name


__all__ = ["ModernAdapter"]
