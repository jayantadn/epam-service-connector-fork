# Hardware Simulator (PyTk dashboard)

A small Tkinter GUI that runs **on the host** and replaces the manual
Kuksa CLI workflow during the EV Range Extender demo. It publishes
slider/spinbox values over **Zenoh** to three ECUs running inside the
VMs:

| Section in the GUI | Zenoh key | Receiver | Lands in (Kuksa, on VM) |
|---|---|---|---|
| Battery Voltage | `sim/battery/voltage` | `bms.py` (VM1) | `Vehicle.Powertrain.TractionBattery.CurrentVoltage` |
| Battery Current | `sim/battery/current` | `bms.py` (VM1) | `Vehicle.Powertrain.TractionBattery.CurrentCurrent` |
| Battery SoC | `sim/battery/soc` | `bms.py` (VM1) | `Vehicle.Powertrain.TractionBattery.StateOfCharge.Current` |
| Cabin Ambient Temp | `sim/cabin/temp` | `hvac_ecu.py` (VM2) | `Vehicle.Cabin.HVAC.AmbientAirTemperature` |
| Seat Heating | `sim/cabin/seat/heating` | `seat_ecu.py` (VM2) | `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` |
| Seat Heating-Cooling | `sim/cabin/seat/hc` | `seat_ecu.py` (VM2) | `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` |

`range_ai.py` on VM1 sees the Kuksa updates and recomputes
`Vehicle.Powertrain.Range` exactly as before — only the input layer
changed.

## Topology

```
Host (WSL, 192.168.100.1)               VMs (auto-deployed by cloud-init)
+------------------------------+
| pytk_dashboard.py            |
|  Tk window: 6 sliders        |
|                              |   tcp/192.168.100.10:7460
|  Zenoh peer session,         |  ----------------> bms.py        (VM1)
|  dials all 3 ECUs            |   tcp/192.168.100.11:7461
|                              |  ----------------> hvac_ecu.py   (VM2)
|                              |   tcp/192.168.100.11:7462
|                              |  ----------------> seat_ecu.py   (VM2)
+------------------------------+
```

Each Zenoh sample is a tiny JSON document:

```json
{ "value": 22.5, "source": "host-name", "ts": "2026-...Z" }
```

## Prerequisites (host)

- **Python 3.9+** with the `tkinter` stdlib module. On Debian/Ubuntu:

  ```bash
  sudo apt install -y python3-tk
  ```
- **Zenoh Python binding**:

  ```bash
  pip install --user -r requirements.txt
  ```
- **Network**: the WSL host must already have the `br0` bridge from
  `setup.sh` (so `192.168.100.1` is on the same L2 segment as the VMs).
  No extra firewall rules — the existing `iptables FORWARD -i br0 -o
  br0 -j ACCEPT` rule is enough.

## Run

Defaults match `setup.sh`'s addressing (VM1 `192.168.100.10`, VM2
`192.168.100.11`) and the ECU listen ports baked into `bms.py`,
`hvac_ecu.py`, `seat_ecu.py`:

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

## Verifying the round trip

With the GUI open and the VMs booted (the ECUs auto-start on boot), in
a separate terminal SSH into VM1 and watch any of:

```bash
ssh ubuntu@192.168.100.10 'sudo journalctl -fu ev-range-bms'   # BMS log
ssh ubuntu@192.168.100.11 'sudo journalctl -fu ev-range-hvac'  # HVAC log
ssh ubuntu@192.168.100.11 'sudo journalctl -fu ev-range-seat'  # Seat log
```

Move a slider in the GUI → the matching ECU prints `OK <vss-path> = <value>`.

## Troubleshooting

**GUI starts but ECUs see nothing**

- `iptables -A FORWARD -i br0 -o br0 -j ACCEPT` is missing on the host.
- An ECU systemd service is stopped: `sudo systemctl status ev-range-bms`
  on VM1, `sudo systemctl status ev-range-hvac ev-range-seat` on VM2.
- Wrong VM IP / port — pass `--vm1 / --vm2 / --bms-port / ...` to override.

**`ImportError: No module named '_tkinter'`**

Tk isn't installed: `sudo apt install -y python3-tk`.

**`ImportError: No module named 'zenoh'`**

`pip install --user -r requirements.txt`.

**Slider feels laggy**

Each slider drag publishes on every tick. If that's too noisy you can
edit `_on_scale` in `pytk_dashboard.py` to debounce / throttle.
