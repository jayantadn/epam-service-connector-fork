# EV Range Extender — VM1 (HPC) components

VM1 hosts the digital.auto **SDV Runtime** (`ev-range` Kuksa
Databroker on `127.0.0.1:55555`) plus three Python services that
auto-start on every boot via systemd. **You do not run anything on
VM1 by hand during the demo.**

| Component | File | systemd unit | Role |
|---|---|---|---|
| Battery Monitoring System | `bms.py` | `ev-range-bms.service` | Subscribes to host PyTk Zenoh keys (`sim/battery/*`) on `tcp/0.0.0.0:7460` and writes the values into VM1's Kuksa under `Vehicle.Powertrain.TractionBattery.*`. |
| Range Compute AI | `range_ai.py` | `ev-range-range-ai.service` | Subscribes to 3 battery + 3 cabin signals on VM1's Kuksa, computes range, publishes `Vehicle.Powertrain.Range`. |

## VSS signals on VM1's Kuksa

VM1's Kuksa Databroker is the single source of truth for the demo —
every signal is collected here and `range_ai.py` reads them all from
this one broker.

| VSS path | Type | Where it comes from |
|---|---|---|
| `Vehicle.Powertrain.TractionBattery.CurrentVoltage` | float (V) | host PyTk dashboard → Zenoh → `bms.py` |
| `Vehicle.Powertrain.TractionBattery.CurrentCurrent` | float (A) | host PyTk dashboard → Zenoh → `bms.py` |
| `Vehicle.Powertrain.TractionBattery.StateOfCharge.Current` | float (%) | host PyTk dashboard → Zenoh → `bms.py` |
| `Vehicle.Cabin.HVAC.AmbientAirTemperature` | float | host (Fan Speed slider) → `hvac_ecu.py` (VM2) → kuksa-bridge → VM1's Kuksa |
| `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` | int (% 0..100) | host (Seat Heating toggle) → `seat_ecu.py` (VM2) → kuksa-bridge → VM1's Kuksa |
| `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` | int (% -100..100) | host (Seat Cooling toggle) → `seat_ecu.py` (VM2) → kuksa-bridge → VM1's Kuksa |
| `Vehicle.Powertrain.Range` | uint32 (km) | written by `range_ai.py` |

> **Signal note for the dashboard's "Fan Speed" slider.** The slider
> publishes 0..100, which lands at
> `Vehicle.Cabin.HVAC.AmbientAirTemperature`. The six input VSS paths
> stay exactly as the original signal catalogue defines them; only the
> *interpretation* of that value is decided by `range_ai.py` (it
> treats it as fan-speed percent for the demo, so a higher value adds
> HVAC cabin load and reduces Range).

## Range model (`range_ai.py`)

```
available_kWh   = (SoC / 100) * 75 kWh
consumption     = 0.18 kWh/km
                  * load_factor              (if instantaneous power > 18 kW)
                  + cabin_load_kW / 60 km/h  (HVAC fan + seat heater + ventilation)
range_km        = available_kWh / consumption
```

`load_factor = power_kW / 18` whenever `|U·I| / 1000` exceeds the
18 kW cruise threshold (i.e. roughly above `~48 A` at `380 V`).

`cabin_load_kW` is additive:

```
cabin_load_kW = 2.0 * (FanSpeed / 100)                  # 0..2 kW HVAC fan
              + 2.0 * (Seat.Heating / 100)              # 0..2 kW seat heater
              + 2.0 * (Seat.HeatingCooling / 100)       # 0..2 kW   if HC > 0
              + 0.5 * (-Seat.HeatingCooling / 100)      # 0..0.5 kW if HC < 0
```

The dashboard's mutex makes Heating and HeatingCooling
mutually-exclusive in practice, so the seat terms can't double-count
when the GUI drives them.

Tweak the constants at the top of `range_ai.py`
(`BATTERY_CAPACITY_KWH`, `NOMINAL_CONSUMPTION_KWH_PER_KM`,
`NOMINAL_CRUISE_POWER_KW`, `HVAC_FAN_FULL_KW`, `SEAT_HEATER_FULL_KW`,
`SEAT_VENT_FULL_KW`, `AVG_SPEED_KMH`) to model a different vehicle.

## Inspect / debug

All three services log to `/tmp/ev-range-*.log` (world-readable, no
sudo needed):

```bash
tail -f /tmp/ev-range-bms.log
tail -f /tmp/ev-range-range-ai.log
tail -f /tmp/ev-range-zenoh-client.log
```

Status / restart:

```bash
systemctl status   ev-range-bms ev-range-range-ai ev-range-zenoh-client
sudo systemctl restart ev-range-range-ai
```

To run any of them by hand (with the systemd unit stopped first):

```bash
sudo systemctl stop ev-range-bms
cd /home/ubuntu/ev-range-extender/vm1
python3 bms.py            # all flags have working defaults
```

## Troubleshooting

See the top-level [`README.md`](../../README.md) "Troubleshooting"
section for the common cases (cloud-init hang, tap busy, service
inactive, dashboard slider with no effect, etc.).
