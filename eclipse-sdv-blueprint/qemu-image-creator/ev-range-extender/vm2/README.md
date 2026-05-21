# EV Range Extender — VM2 (Zonal / cabin) components

VM2 owns the **cabin signals**. It hosts the digital.auto **SDV
Runtime clients plus a local Kuksa Databroker endpoint (`127.0.0.1:55555`).
The VM2 ECUs write only to local Kuksa; cross-VM sharing to/from VM1 is
done only by `kuksa-bridge`, and auto-starts via systemd.
**You do not run anything on VM2 by hand during the demo.**

| Component | File | systemd unit | Role |
|---|---|---|---|
| HVAC ECU | `hvac_ecu.py` | `ev-range-hvac.service` | Subscribes to host PyTk Zenoh key `sim/cabin/temp` on `tcp/0.0.0.0:7461` and writes the value into VM2 local Kuksa under `Vehicle.Cabin.HVAC.AmbientAirTemperature` (bridge sync carries it to VM1). |
| Seat Control Module | `seat_ecu.py` | `ev-range-seat.service` | Subscribes to host PyTk Zenoh keys `sim/cabin/seat/heating` + `sim/cabin/seat/hc` on `tcp/0.0.0.0:7462` and writes them into VM2 local Kuksa under `Vehicle.Cabin.Seat.Row1.DriverSide.{Heating,HeatingCooling}` (bridge sync carries them to VM1). |

## VSS signals written to VM2 local Kuksa

| VSS path | Type | Source | Notes |
|---|---|---|---|
| `Vehicle.Cabin.HVAC.AmbientAirTemperature` | float | host PyTk Fan Speed slider → `hvac_ecu.py` | Slider is 0..100; the dashboard labels it "Fan Speed" and `range_ai.py` on VM1 interprets the value as fan-speed percent (see top-level README). |
| `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` | int (% 0..100) | host PyTk Seat Heating toggle → `seat_ecu.py` | 0 = off, 100 = on. |
| `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` | int (% -100..100) | host PyTk Seat Cooling toggle → `seat_ecu.py` | 0 = off, -100 = cooling on. (Positive values are valid VSS but the dashboard never publishes them.) |

> **Why these exact paths?** They're the canonical COVESA VSS 4.x
> leaves shipped with the `sdv-runtime` image. There is no
> `Seat.Ventilation` leaf in COVESA — the standard way to express
> ventilation on a seat is `HeatingCooling` with a negative percent,
> which is what the dashboard publishes when "Seat Cooling" is on.

## VSS catalog sanity check

VM1 `sdv-runtime` ships with the standard COVESA VSS catalog, so all
three cabin paths are present out of the box. If you ever need to
verify, run the Kuksa CLI on VM1:

```bash
docker run -it --rm --network host \
    ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main
```

```text
metadata Vehicle.Cabin.HVAC.AmbientAirTemperature
metadata Vehicle.Cabin.Seat.Row1.DriverSide.Heating
metadata Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
```

All three should return `[metadata] OK`. If any returns `not_found`,
the wrong container image is running on VM1:

```bash
docker inspect --format '{{.Config.Image}}' sdv-runtime
# expected: ghcr.io/eclipse-autowrx/sdv-runtime:latest
```

## Inspect / debug

Both services log to `/tmp/ev-range-*.log` (world-readable, no sudo needed):

```bash
tail -f /tmp/ev-range-hvac.log
tail -f /tmp/ev-range-seat.log
```

Status / restart:

```bash
systemctl status   ev-range-hvac ev-range-seat
sudo systemctl restart ev-range-hvac
```

To run any of them by hand (with the systemd unit stopped first):

```bash
sudo systemctl stop ev-range-hvac
cd /home/ubuntu/ev-range-extender/vm2
python3 hvac_ecu.py            # defaults are correct
```

## Troubleshooting

See the top-level [`README.md`](../../README.md) "Troubleshooting"
section for the common cases (cloud-init hang, service inactive,
dashboard slider with no effect, etc.).
