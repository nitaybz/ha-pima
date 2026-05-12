# PIMA Alarm for Home Assistant

<img src="custom_components/pima/brand/logo.png" align="right" alt="PIMA logo" width="200">

A complete Home Assistant integration for **PIMA** alarm panels, with full UI setup, automatic discovery, and support for both panel generations.

* **Legacy panels:** Hunter Pro 32 / 96 / 144, via `net4pro` or any serial-to-Ethernet bridge (Lantronix XPort, Moxa NPort, Elfin EW11, etc.).
* **Modern panels:** Force / Vision, native JSON-over-TCP protocol (the same one PIMALINK 2.0 uses).

Once configured you get a real `alarm_control_panel` entity per partition, a `binary_sensor` for every zone (named with the panel's own labels on Force), system trouble sensors, output switches, automation-friendly events, a bypass service, an options flow, and diagnostics. Everything you would expect from a first-class Home Assistant integration.

## What you get

| Capability | Hunter Pro (legacy) | Force / Vision |
| --- | :---: | :---: |
| Add via UI (no YAML) | yes | yes |
| Auto-find panel on the LAN | yes | n/a (panel dials in) |
| Auto-detect zone count | yes (96 or 144) | yes (exact installed count) |
| Auto-detect partitions in use | partial | yes (panel reports "not exist") |
| Real zone names from panel | no (protocol limit) | yes |
| Arm away / home / night / disarm | yes | yes |
| ARMING / DISARMING progress in UI | yes | yes |
| Zone bypass service | yes | yes |
| Output switches (sirens + controlled outputs) | n/a | yes |
| HA events on alarm trigger | yes | yes |
| Fires `pima_alarm_triggered` event | yes | yes |
| Diagnostics download (redacted) | yes | yes |
| Options flow (edit code, partitions, polling) | yes | yes |
| Optional "require code in UI" toggle | yes | yes |

## Install

### Via HACS (recommended)

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/nitaybz/ha-pima` with category **Integration**.
3. Install **PIMA Alarm**.
4. Restart Home Assistant.

### Manual

Copy the `custom_components/pima/` folder into your Home Assistant config directory, then restart.

## Configure

Open **Settings ▸ Devices & Services ▸ Add Integration**, search **PIMA Alarm**, and pick your panel family.

### Hunter Pro 32 / 96 / 144 (legacy `net4pro`)

The form opens with the panel pre-filled. If it does not, the integration scans your LAN, validates each candidate by reading a real PIMA frame, and prefills the host and port automatically. You fill in the access code; zone count and partition list are auto-detected if you leave them blank.

| Field | What to enter |
| --- | --- |
| Host / IP | Leave blank to auto-scan, or type the panel's address |
| TCP port | Auto-detected (4001 for `net4pro`, 10001 for Lantronix bridges, etc.) |
| Access code | User code, master code, or installer code. Many installs require the installer code for remote IP access. |
| Zone count | Leave on **Auto** (the panel tells us 96 vs 144) |
| Partitions | Leave blank to auto-detect, or list them (e.g. `1` or `1,2`) |

### Force / Vision (modern JSON protocol)

Force panels are TCP clients, so Home Assistant listens and the panel dials in. The integration auto-learns the account ID, installed zone count, partition list, zone names, user names, and fault state on the first connection.

1. Pick a free TCP port (default `9999`).
2. Enter the access code your panel accepts for remote operations.
3. Save. Home Assistant starts listening.
4. Open **PIMALINK 2.0** on the installer side, go to **Network**, and point the panel at your Home Assistant IP and the port you chose.
5. When the panel dials in, entities appear automatically.

## Services

| Service | What it does |
| --- | --- |
| `pima.bypass_zone` | Toggles the manual-bypass bit for a single zone. Works on both legacy and modern panels. |

## Events

The integration fires standard Home Assistant events you can use as automation triggers:

| Event | Fired when |
| --- | --- |
| `pima_alarm_triggered` | Any zone enters the alarmed state |
| `pima_zone_triggered` | A specific zone fires (includes `zone` and `at`) |

## Options

After setup, click **Configure** on the integration to adjust:

* Access code
* Partition list (extend if you have more than one)
* Polling interval (default 5 seconds, range 2 to 60)
* Whether Home Assistant prompts for the code in the UI before arming or disarming (off by default for RTI-style one-tap operation)

## Limitations and known gaps

The legacy `net4pro` protocol cannot tell an unconfigured partition apart from a real-but-currently-disarmed one. Both report `0x00`. We default to a single partition and let you extend the list via the options flow. The modern Force protocol does not have this limitation.

The Force protocol uses very small TCP frames at 2400-baud bridges, so the legacy frame parser includes a read-loop to assemble complete frames; this is automatic and transparent.

## Credits

* The legacy protocol implementation is based on the excellent reverse-engineered work by [Dror Eiger (deiger/Alarm)](https://github.com/deiger/Alarm), redistributed here under GPL-3.0-or-later.
* The Force JSON protocol is implemented from the official PIMA "Force Interface JSON Format Specification, Ver. 2.1".
* PIMA is a trademark of PIMA Electronic Systems Ltd. This integration has no affiliation with PIMA.

## Support the project

**ha-pima** is a free integration under GPL-3.0. It was built and is maintained by one person on personal time. Creating and maintaining Home Assistant integrations takes a lot of hours; if it helps you, a "Star" on GitHub or a small donation is hugely appreciated.

<a target="blank" href="https://www.paypal.me/nitaybz"><img src="https://img.shields.io/badge/PayPal-Donate-blue.svg?logo=paypal"/></a><br>
<a target="blank" href="https://www.patreon.com/nitaybz"><img src="https://img.shields.io/badge/PATREON-Become a patron-red.svg?logo=patreon"/></a><br>
<a target="blank" href="https://ko-fi.com/nitaybz"><img src="https://img.shields.io/badge/Ko--Fi-Buy%20me%20a%20coffee-29abe0.svg?logo=ko-fi"/></a>

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
