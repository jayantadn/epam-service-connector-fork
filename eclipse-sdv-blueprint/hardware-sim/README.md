# Hardware Simulator (PyTk dashboard)

Tkinter GUI that runs **on the host** and replaces the manual Kuksa
CLI workflow during the EV Range Extender demo. Sliders and toggles
publish over **Eclipse Zenoh** to the three ECUs running inside the
VMs.

## Controls

| Section in the GUI | Control | Zenoh key | Receiver | Lands in (Kuksa, on VM) |
|---|---|---|---|---|
| Battery (VM1 - bms.py) | Battery Voltage (320-420 V) | `sim/battery/voltage` | `bms.py` (VM1) | `Vehicle.Powertrain.TractionBattery.CurrentVoltage` |
| Battery (VM1 - bms.py) | Battery Current (0-200 A) | `sim/battery/current` | `bms.py` (VM1) | `Vehicle.Powertrain.TractionBattery.CurrentCurrent` |
| Battery (VM1 - bms.py) | Battery % (0-100) | `sim/battery/soc` | `bms.py` (VM1) | `Vehicle.Powertrain.TractionBattery.StateOfCharge.Current` |
| Cabin HVAC (VM2 - hvac_ecu.py) | Fan Speed (0-100, slider) | `sim/cabin/temp` | `hvac_ecu.py` (VM2) | `Vehicle.Cabin.HVAC.AmbientAirTemperature` |
| Cabin Seat (VM2 - seat_ecu.py) | Seat Heating (toggle) | `sim/cabin/seat/heating` | `seat_ecu.py` (VM2) | `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` (0 / 100) |
| Cabin Seat (VM2 - seat_ecu.py) | Seat Cooling (toggle) | `sim/cabin/seat/hc` | `seat_ecu.py` (VM2) | `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` (0 / -100) |

`range_ai.py` on VM1 sees the Kuksa updates and recomputes
`Vehicle.Powertrain.Range`. `Seat Heating` and `Seat Cooling` are
mutually exclusive — turning one on automatically forces the other
off.

> **Signal note:** the "Fan Speed" slider rides on the existing VSS
> path `Vehicle.Cabin.HVAC.AmbientAirTemperature`. The six VSS paths
> stay exactly as the original signal catalogue defines them; only
> the *interpretation* of the value (fan-speed percent vs. degrees
> Celsius) is decided by `range_ai.py` on VM1. Higher Fan Speed →
> more cabin draw → less Range.

Each Zenoh sample is a tiny JSON document:

```json
{ "value": 50, "source": "host-name", "ts": "2026-...Z" }
```

## Topology

```
Host (192.168.100.1)                    VMs (auto-deployed by cloud-init)
+----------------------------+
| pytk_dashboard.py          |   tcp/192.168.100.10:7460
|   3 battery sliders        |  ----------------> bms.py        (VM1)
|   1 fan-speed slider       |   tcp/192.168.100.11:7461
|   2 seat toggles           |  ----------------> hvac_ecu.py   (VM2)
|                            |   tcp/192.168.100.11:7462
|   Zenoh peer publisher     |  ----------------> seat_ecu.py   (VM2)
+----------------------------+
```

## Prerequisites (host)

- **Python 3.9+** with `tkinter`. On Debian/Ubuntu:
  ```bash
  sudo apt install -y python3-tk
  ```
- **Zenoh Python binding**. The recommended path is to reuse the
  virtualenv created in `qemu-image-creator/` (a sibling of this
  folder at the repo root):
  ```bash
  cd ../qemu-image-creator
  source .venv/bin/activate
  python3 -m pip install -r requirements.txt
  ```
  Or, without a virtualenv:
  ```bash
  pip install --user --break-system-packages eclipse-zenoh
  ```
- **Network**: the WSL host must already have the `br0` bridge from
  `setup.py` / `setup.sh` (so `192.168.100.1` is on the same L2
  segment as the VMs). The matching `iptables FORWARD -i br0 -o
  br0 -j ACCEPT` rule is added by `setup.py`.

## Run

Defaults match `setup.py` / `setup.sh` addressing (VM1
`192.168.100.10`, VM2 `192.168.100.11`):

```bash
python3 pytk_dashboard.py
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

## Verify the round trip

With the GUI open and the VMs booted (the ECUs auto-start on boot),
in a separate terminal SSH into a VM and tail one of the ECU logs:

```bash
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-bms.log'
ssh ubuntu@192.168.100.11 'tail -f /tmp/ev-range-hvac.log'
ssh ubuntu@192.168.100.11 'tail -f /tmp/ev-range-seat.log'
```

Move a slider in the GUI → the matching ECU prints `OK <vss-path> = <value>`.

For the recomputed range:

```bash
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-range-ai.log'
```

## Troubleshooting

**GUI starts but nothing happens on the VM side**

- An ECU systemd service is stopped:
  ```bash
  ssh ubuntu@192.168.100.10 'systemctl is-active ev-range-bms'
  ssh ubuntu@192.168.100.11 'systemctl is-active ev-range-hvac ev-range-seat'
  ```
- The host `iptables FORWARD -i br0 -o br0 -j ACCEPT` rule is missing
  (re-run `python3 setup.py` from the parent folder, or add the rule
  manually).
- Wrong VM IP / port — pass `--vm1 / --vm2 / --bms-port / ...` to
  override.

**`ImportError: No module named '_tkinter'`**

Tk is not installed on the host: `sudo apt install -y python3-tk`.

**`ImportError: No module named 'zenoh'`**

The `qemu-image-creator/` virtualenv isn't active or its deps weren't
installed:

```bash
cd ../qemu-image-creator
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

**Slider feels laggy**

Each drag publishes on every tick. If that's too noisy, edit
`_on_scale` in `pytk_dashboard.py` to debounce / throttle.

**Window is invisible / cursor disappears (WSLg)**

The dashboard already forces `cursor=left_ptr` and lifts/focuses the
window on launch. If the cursor still disappears after a long idle,
click anywhere in the window once.
