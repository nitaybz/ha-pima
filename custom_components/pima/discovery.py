"""LAN discovery for legacy PIMA panels.

PIMA panels (and any serial-to-Ethernet bridge in front of them) auto-emit
length-prefixed frames every ~1 s once a TCP connection is open. We can't
assume we land on a frame boundary, so we read a short window of bytes and
scan it for the unique 3-byte signature ``[length][module_id][message]``
where every byte is from the known PIMA vocabulary:

* length  ∈ {0x08 idle, 0x62 96-zone status, 0x7A 144-zone status}
* module  ∈ {0x0D for 32/96-zone, 0x13 for 144-zone}
* message ∈ {0x01 OPEN, 0x05 STATUS, 0x0E READ, 0x0F WRITE, 0x19 CLOSE}

Discovery is **ARP-first**: we only probe hosts the kernel already knows
about, on the two highest-likelihood ports (4001 = net4pro default,
10001 = Lantronix XPort default) plus whatever the user typed. That keeps
the worst-case scan to a couple of seconds rather than minutes against an
entire /24.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Iterable

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


MODULE_ID_TO_ZONES = {0x0D: 96, 0x13: 144}
_VALID_LENGTHS = {0x08, 0x62, 0x7A}
_VALID_MESSAGES = {0x01, 0x05, 0x0E, 0x0F, 0x19}

# Ports tried first when no specific port was given. 4001 is net4pro's default
# and 10001 is Lantronix XPort's default — those two cover the overwhelming
# majority of legacy PIMA installs.
PRIORITY_PORTS: tuple[int, ...] = (4001, 10001)
# Fallback ports for less-common bridges (Moxa NPort, Elfin EW11, etc.).
FALLBACK_PORTS: tuple[int, ...] = (4000, 4002, 2000, 4660, 8000)

_CONCURRENCY = 32
_PROBE_CONNECT_TIMEOUT = 1.2
_PROBE_READ_TIMEOUT = 2.5
_TOTAL_SCAN_TIMEOUT = 10.0
_ARP_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+\S+\s+\S+\s+\S+\s+(\S+)", re.MULTILINE)


def _get_local_ip_blocking() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _read_arp_table_blocking() -> list[str]:
    """Return IPs the kernel marks REACHABLE or STALE."""
    try:
        with open("/proc/net/arp") as f:
            lines = f.readlines()
    except OSError:
        return []
    hosts: list[str] = []
    for line in lines[1:]:  # skip header
        parts = line.split()
        if len(parts) < 6:
            continue
        ip, _hw_type, flags, mac, _mask, _dev = parts[:6]
        # Flag 0x2 = ATF_COM (complete entry, MAC is known). Skip incomplete.
        try:
            if int(flags, 16) & 0x2 and mac != "00:00:00:00:00:00":
                hosts.append(ip)
        except ValueError:
            continue
    return hosts


def _signature_search(buf: bytes) -> int | None:
    for i in range(len(buf) - 2):
        if (
            buf[i] in _VALID_LENGTHS
            and buf[i + 1] in MODULE_ID_TO_ZONES
            and buf[i + 2] in _VALID_MESSAGES
        ):
            return MODULE_ID_TO_ZONES[buf[i + 1]]
    return None


async def _collect_bytes(
    reader: asyncio.StreamReader, deadline: float, want: int = 256
) -> bytearray:
    buf = bytearray()
    while len(buf) < want:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            chunk = await asyncio.wait_for(
                reader.read(min(64, want - len(buf))), timeout=remaining
            )
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf.extend(chunk)
    return buf


async def _probe(host: str, port: int) -> int | None:
    """Probe one host:port; return zone count on success, None otherwise."""
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=_PROBE_CONNECT_TIMEOUT
        )
        deadline = asyncio.get_running_loop().time() + _PROBE_READ_TIMEOUT
        buf = await _collect_bytes(reader, deadline)
    except (asyncio.TimeoutError, OSError):
        return None
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (asyncio.TimeoutError, OSError):
                pass
    return _signature_search(buf)


async def async_probe_module_id(host: str, port: int) -> int | None:
    """Backwards-compatible wrapper used by the config flow."""
    return await _probe(host, port)


async def _scan_pass(targets: Iterable[tuple[str, int]]) -> list[tuple[str, int, int]]:
    """Run a single concurrent pass; return ``(host, port, zones)`` hits."""
    sem = asyncio.Semaphore(_CONCURRENCY)
    hits: list[tuple[str, int, int]] = []

    async def bounded(host: str, port: int) -> None:
        async with sem:
            zones = await _probe(host, port)
            if zones is not None:
                hits.append((host, port, zones))

    await asyncio.gather(*(bounded(h, p) for h, p in targets))
    return hits


async def async_scan_lan(
    hass: HomeAssistant,
    port: int | None = None,
    subnet_prefix: int = 24,
    extra_ports: Iterable[int] = (),
) -> list[tuple[str, int]]:
    """Discover PIMA panels on the LAN.

    Strategy:

    1. Read the host's ARP table for live neighbours (cheap, near-zero false
       positives in normal LAN conditions).
    2. Pass 1 — probe each neighbour on the **priority ports** (4001, 10001,
       plus any port the user typed).
    3. Pass 2 — if nothing turned up, retry on a small fallback port set.

    The whole operation is wrapped in :data:`_TOTAL_SCAN_TIMEOUT` so the
    config flow never blocks longer than ~10 s.
    """

    async def _do_scan() -> list[tuple[str, int]]:
        arp_hosts = await hass.async_add_executor_job(_read_arp_table_blocking)
        source_ip = await hass.async_add_executor_job(_get_local_ip_blocking)
        if source_ip:
            arp_hosts = [h for h in arp_hosts if h != source_ip]
        _LOGGER.debug("ARP turned up %d reachable hosts", len(arp_hosts))
        if not arp_hosts:
            # Fall back to a /24 sweep (slow path, only when ARP is empty).
            if source_ip:
                try:
                    net = ipaddress.IPv4Network(
                        f"{source_ip}/{subnet_prefix}", strict=False
                    )
                    arp_hosts = [str(ip) for ip in net.hosts() if str(ip) != source_ip]
                except ValueError:
                    arp_hosts = []
            _LOGGER.debug("Falling back to /24 sweep (%d hosts)", len(arp_hosts))

        priority_ports = tuple(
            dict.fromkeys((*(p for p in [port] if p), *PRIORITY_PORTS, *extra_ports))
        )
        _LOGGER.debug("Pass 1: %d hosts × %s", len(arp_hosts), priority_ports)
        targets = [(h, p) for h in arp_hosts for p in priority_ports]
        hits = await _scan_pass(targets)

        if not hits:
            _LOGGER.debug("Pass 2: %d hosts × %s", len(arp_hosts), FALLBACK_PORTS)
            targets = [(h, p) for h in arp_hosts for p in FALLBACK_PORTS]
            hits = await _scan_pass(targets)

        return [(h, p) for h, p, _ in hits]

    try:
        results = await asyncio.wait_for(_do_scan(), timeout=_TOTAL_SCAN_TIMEOUT)
    except asyncio.TimeoutError:
        _LOGGER.warning("LAN scan exceeded %.1fs budget — aborted", _TOTAL_SCAN_TIMEOUT)
        return []

    _LOGGER.info("LAN scan finished: %d panel(s) found %s", len(results), results)
    return results
