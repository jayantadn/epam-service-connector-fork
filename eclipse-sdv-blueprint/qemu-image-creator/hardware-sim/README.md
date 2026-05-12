# Hardware Simulator (PyTk dashboard)

A small Tkinter GUI that runs **on the host** and is the only piece of
the EV Range Extender demo a developer launches by hand. It publishes
slider / toggle values over **Zenoh** to the three ECUs running inside
the VMs (which were auto-deployed and auto-started by cloud-init):

| GUI control | Type | Range | Zenoh key | Receiver | VSS path written by the ECU |
|---|---|---|---|---|---|
| Battery Voltage | Slider | 320 – 420 V (default 400) | `sim/battery/voltage` | `bms.py` (VM1) | `Vehicle.Powertrain.TractionBattery.CurrentVoltage` |
| Battery Current | Slider | 0 – 200 A (default 25) | `sim/battery/current` | `bms.py` (VM1) | `Vehicle.Powertrain.TractionBattery.CurrentCurrent` |
| Battery % | Slider | 0 – 100 % (default 80) | `sim/battery/soc` | `bms.py` (VM1) | `Vehicle.Powertrain.TractionBattery.StateOfCharge.Current` |
| Fan Speed | Slider | 0 – 100 % (default 0) | `sim/cabin/fan-speed` | `hvac_ecu.py` (VM2) | `Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed` |
| Seat Heating | Toggle | OFF (0) / ON (100) | `sim/cabin/seat/heating` | `seat_ecu.py` (VM2) | `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` |
| Seat Cooling | Toggle | OFF (0) / ON (-100) | `sim/cabin/seat/hc` | `seat_ecu.py` (VM2) | `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` |

All sliders are **non-negative only** — there is no separate regen /
charging input, and there is no negative ambient temperature. **Seat
Heating** and **Seat Cooling** are **mutually exclusive**: ticking one
automatically clears the other and publishes the cleared toggle's OFF
value, so the receiving ECU on VM2 also stops the action.

`zenoh_publisher.py` on VM2 forwards the cabin paths to VM1, where
`range_ai.py` recomputes `Vehicle.Powertrain.Range`. None of that
plumbing is visible from the dashboard — the developer just moves
sliders and watches the result on VM1.

## Topology

```
Host (WSL, 192.168.100.1)               VMs (auto-deployed + auto-started by cloud-init)
+------------------------------+
| pytk_dashboard.py            |
|   Tk window:                 |
|     3 battery sliders        |   tcp/192.168.100.10:7460
|     1 fan-speed slider       |  ----------------> bms.py        (VM1)
|     2 seat toggles           |   tcp/192.168.100.11:7461
|   single Zenoh peer session, |  ----------------> hvac_ecu.py   (VM2)
|   one publisher per key      |   tcp/192.168.100.11:7462
|                              |  ----------------> seat_ecu.py   (VM2)
+------------------------------+
```

Each Zenoh sample is a tiny JSON document:

```json
{ "value": 75, "source": "<host-name>", "ts": "2026-…Z" }
```

For toggles `value` is the configured ON or OFF integer (e.g. `100` or
`0` for `Seat Heating`).

## Prerequisites (host)

- **Python 3.9+** with the `tkinter` stdlib module. On Debian / Ubuntu:
  ```bash
  sudo apt install -y python3-tk
  ```
- **Zenoh Python binding**:
  ```bash
  pip install --user --break-system-packages -r requirements.txt
  ```
- **Network**: the WSL host must already have the `br0` bridge from
  `setup.sh` (so `192.168.100.1` is on the same L2 segment as the VMs).
  WSL Netfilter blocks bridged traffic by default — the existing
  `iptables -A FORWARD -i br0 -o br0 -j ACCEPT` rule unblocks both the
  host → ECU links and the VM2 → VM1 cross-VM bridge.
- **VMs running** with all 6 services `active` — see the top-level
  [`README.md`](../README.md) Step 3 for the exact `systemctl
  is-active` checks.

## Run

Defaults match `setup.sh`'s addressing (VM1 `192.168.100.10`, VM2
`192.168.100.11`) and the Zenoh listen ports baked into `bms.py`,
`hvac_ecu.py`, and `seat_ecu.py`:

```bash
python3 pytk_dashboard.py
# expected on stdout:
#   [pytk] dialing Zenoh endpoints: ['tcp/192.168.100.10:7460',
#                                    'tcp/192.168.100.11:7461',
#                                    'tcp/192.168.100.11:7462']
```

To override addresses / ports:

```bash
python3 pytk_dashboard.py \
    --vm1 192.168.100.10 \
    --vm2 192.168.100.11 \
    --bms-port  7460 \
    --hvac-port 7461 \
    --seat-port 7462
```

## Verifying the round trip

With the GUI open and the VMs booted (the ECUs auto-start on boot),
open a second host terminal and tail any of the per-service logs (each
service writes to a world-readable `/tmp/ev-range-*.log`):

```bash
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-bms.log'         # battery sliders
ssh ubuntu@192.168.100.11 'tail -f /tmp/ev-range-hvac.log'        # fan-speed slider
ssh ubuntu@192.168.100.11 'tail -f /tmp/ev-range-seat.log'        # seat toggles
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-range-ai.log'    # recomputed Range
```

Move a slider or click a toggle. The matching ECU prints
`OK <vss-path> = <value> (from <host>)` and `range_ai` reprints its
input/output a moment later.

## Troubleshooting

**GUI starts but the ECUs see nothing**

In order, check:
1. The host's bridge-forwarding rule:
   `sudo iptables -L FORWARD -n -v | grep -E 'br0.*br0'`. If missing,
   `sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT`.
2. The matching ECU service is `active`:
   ```bash
   ssh ubuntu@192.168.100.10 'systemctl is-active ev-range-bms.service'
   ssh ubuntu@192.168.100.11 'systemctl is-active ev-range-hvac.service ev-range-seat.service'
   ```
   If one is `failed`: `journalctl -u <unit> -n 60 --no-pager` plus
   `tail -n 60 /tmp/ev-range-<name>.log`.
3. The first line `pytk_dashboard.py` prints. The endpoints in
   `[pytk] dialing Zenoh endpoints: [...]` must match the VM IPs +
   listen ports above. If they don't, override with `--vm1 / --vm2 /
   --bms-port / --hvac-port / --seat-port`.

**`ImportError: No module named '_tkinter'`**

Tk isn't installed on the host: `sudo apt install -y python3-tk`.

**`ImportError: No module named 'zenoh'`**

```bash
pip install --user --break-system-packages -r requirements.txt
```

**Cursor invisible / window opens behind the terminal (WSLg)**

Both are mitigated in code (`pytk_dashboard.py` pins
`cursor="left_ptr"` and lifts itself with
`attributes("-topmost", True)`), but if you still see them on a stale
WSLg session, `wsl --shutdown` from Windows PowerShell and reconnect.
The top-level [`README.md`](../README.md) has the full WSLg recovery
recipe (xcursor-themes, `XCURSOR_THEME` env vars).

**Slider feels laggy**

Each slider drag publishes on every Tk tick (sub-millisecond on the
host, then one Zenoh sample over `br0`). If that's too noisy for your
taste, edit `_publish` in `pytk_dashboard.py` to debounce / throttle.
