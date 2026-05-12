# EV Range Extender — VM1 (HPC) components

VM1 is the **High-Performance Compute** node. It hosts the
`digital.auto sdv-runtime` container (Kuksa Databroker on
`127.0.0.1:55555`, named `ev-range`) plus three Python applications that
are auto-deployed by cloud-init and auto-started as `systemd` services
on first boot. **No file is started by hand on VM1.**

| Component | File | systemd unit | Role |
|---|---|---|---|
| **BMS** | `bms.py` | `ev-range-bms.service` | Listens on `tcp/0.0.0.0:7460` for the host PyTk dashboard's `sim/battery/*` Zenoh keys and writes the values into the **local** `Vehicle.Powertrain.TractionBattery.*` paths in Kuksa. |
| **Zenoh client (VM2 → VM1 bridge — receiving end)** | `zenoh_client.py` | `ev-range-zenoh-client.service` | Listens on `tcp/0.0.0.0:7447` for cabin samples forwarded by VM2's `zenoh_publisher.py` and writes them into the local Kuksa under their original `Vehicle.Cabin.*` paths. |
| **Range Compute AI** | `range_ai.py` | `ev-range-range-ai.service` | Subscribes to the 6 input signals on the local Kuksa, recomputes `Vehicle.Powertrain.Range`, publishes it back. |

All three services depend only on the local Kuksa Databroker on
`127.0.0.1:55555` and on the Python packages cloud-init installs
(`kuksa-client`, `eclipse-zenoh`). They use **only Eclipse Zenoh** for
out-of-broker traffic — there is no SOME/IP, no gRPC, no manual `scp`.

## Signals owned and consumed on VM1

`bms.py` is the **only writer** of the battery branch. `zenoh_client.py`
is the **only writer** of the cabin branch on VM1 (those writes
originated on VM2 and were forwarded by VM2's `zenoh_publisher.py`).
`range_ai.py` is a pure consumer + producer of `Vehicle.Powertrain.Range`.

| VSS path | Type | Unit | Written by | Driven by |
|---|---|---|---|---|
| `Vehicle.Powertrain.TractionBattery.CurrentVoltage` | float | V | `bms.py` | Battery Voltage slider on the host dashboard (`sim/battery/voltage`) |
| `Vehicle.Powertrain.TractionBattery.CurrentCurrent` | float | A | `bms.py` | Battery Current slider (`sim/battery/current`) |
| `Vehicle.Powertrain.TractionBattery.StateOfCharge.Current` | float | % | `bms.py` | Battery % slider (`sim/battery/soc`) |
| `Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed` | int | % (0..100) | `zenoh_client.py` | Fan Speed slider (host → VM2 `hvac_ecu.py` → VM2 Kuksa → bridge → VM1 Kuksa) |
| `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` | int | % (0..100) | `zenoh_client.py` | Seat Heating toggle (host → VM2 `seat_ecu.py` → VM2 Kuksa → bridge → VM1 Kuksa) |
| `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` | int | % (-100..100; negative = vent/cool, positive = heat) | `zenoh_client.py` | Seat Cooling toggle (same path as above) |
| `Vehicle.Powertrain.Range` | int | km | `range_ai.py` | Computed |

The cabin signals are deliberately **bridged into VM1's Kuksa** rather
than read directly from VM2 — that keeps `range_ai.py` agnostic of the
other VM and lets a single Kuksa CLI on VM1 inspect every input it uses.

## End-to-end data flow seen from VM1

```
HOST (192.168.100.1)                                         VM1 (192.168.100.10)
hardware-sim/pytk_dashboard.py
  sim/battery/voltage  ─────zenoh tcp/7460─────▶ bms.py  ──▶ Vehicle.Powertrain.TractionBattery.CurrentVoltage
  sim/battery/current  ─────zenoh tcp/7460─────▶ bms.py  ──▶ Vehicle.Powertrain.TractionBattery.CurrentCurrent
  sim/battery/soc      ─────zenoh tcp/7460─────▶ bms.py  ──▶ Vehicle.Powertrain.TractionBattery.StateOfCharge.Current
                                                                                │
VM2 (192.168.100.11)                                                            │
  hvac_ecu.py / seat_ecu.py ──▶ VM2 Kuksa                                       │
                          │                                                     │
                          ▼                                                     │
                 zenoh_publisher.py  ───zenoh tcp/7447───▶ zenoh_client.py  ──▶ Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed
                                                                            ──▶ Vehicle.Cabin.Seat.Row1.DriverSide.Heating
                                                                            ──▶ Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
                                                                                │
                                                                                ▼
                                                                ┌──────────────────────────────────────────────────────┐
                                                                │  ev-range Kuksa Databroker (127.0.0.1:55555)         │
                                                                │   3 battery paths   (from bms.py)                    │
                                                                │   3 cabin   paths   (from zenoh_client.py)           │
                                                                │   Vehicle.Powertrain.Range  (written by range_ai.py) │
                                                                └────────────────────────┬─────────────────────────────┘
                                                                                         │ subscribe_current_values (6 inputs)
                                                                                         ▼
                                                                                 range_ai.py
```

## Range model (`range_ai.py`)

The model parameters are constants at the top of `range_ai.py` — change
them there to model a different vehicle. Defaults match a typical
~75 kWh passenger EV:

| Constant | Value | Meaning |
|---|---|---|
| `BATTERY_CAPACITY_KWH` | 75.0 | Usable pack energy |
| `NOMINAL_CONSUMPTION_KWH_PER_KM` | 0.18 | Cruise consumption baseline |
| `NOMINAL_CRUISE_POWER_KW` | 18.0 | Above this the load_factor penalty kicks in |
| `HVAC_FAN_FULL_KW` | 2.0 | HVAC blower + AC compressor at 100 % fan |
| `SEAT_HEATER_FULL_KW` | 2.0 | Driver-zone heating budget (seat + footwell + steering) |
| `SEAT_VENT_FULL_KW` | 0.5 | Driver-zone ventilation/cooling budget |
| `AVG_SPEED_KMH` | 60.0 | Converts kW cabin load to kWh/km |

Per update (any of the 6 inputs changes):

```
available_kWh   = (SoC / 100) * BATTERY_CAPACITY_KWH
power_kW        = |I * U| / 1000
consumption     = NOMINAL_CONSUMPTION_KWH_PER_KM
                * (power_kW / NOMINAL_CRUISE_POWER_KW)   if power_kW > NOMINAL_CRUISE_POWER_KW
                + cabin_load_kW / AVG_SPEED_KMH
range_km        = available_kWh / consumption
```

Cabin load is purely additive:

```
cabin_load_kW =   HVAC_FAN_FULL_KW    * (FanSpeed              / 100)        # FanSpeed in 0..100
              +   SEAT_HEATER_FULL_KW * (Seat.Heating          / 100)        # Heating in 0..100
              + ( SEAT_HEATER_FULL_KW * (Seat.HeatingCooling   / 100)  if HC > 0 )
              + ( SEAT_VENT_FULL_KW   * (-Seat.HeatingCooling  / 100)  if HC < 0 )
```

Until `StateOfCharge.Current` has been written at least once, `range_ai`
logs `<waiting for StateOfCharge to be set>` and publishes nothing. The
3 cabin signals are optional — if VM2 hasn't sent anything yet
`cabin_load_kW = 0` and the model falls back to battery-only.

`Vehicle.Powertrain.Range` is declared `Uint32` in the COVESA VSS
catalog the SDV Runtime ships with, so the value is published as a
non-negative `int` (km).

## Operating the services on VM1

Everything below assumes you have already run `./setup.sh` on the host;
the three services start themselves as soon as cloud-init finishes
installing pip packages and the SDV Runtime container. There is **no
`python3 …` step** during the demo — only the host PyTk dashboard.

### Status

```bash
ssh ubuntu@192.168.100.10 'systemctl is-active ev-range-bms.service \
                                                ev-range-zenoh-client.service \
                                                ev-range-range-ai.service'
# expected: 3 lines saying "active"
```

```bash
ssh ubuntu@192.168.100.10 'ss -ltn | grep -E ":55555|:7447|:7460"'
# expected lines for: 55555 (Kuksa), 7447 (zenoh_client), 7460 (bms)
```

### Logs

Each service writes its stdout/stderr to a world-readable file under
`/tmp/`. Plain `tail -f` works without `sudo`:

```bash
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-bms.log'
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-zenoh-client.log'
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-range-ai.log'
```

For the full systemd journal of any unit:

```bash
ssh ubuntu@192.168.100.10 'sudo journalctl -u ev-range-range-ai.service -n 80 --no-pager'
```

### Verify the round-trip from a Kuksa CLI on VM1

If you want to inspect or override values directly on the Databroker
(no dashboard), you can run the Kuksa CLI ad-hoc:

```bash
ssh ubuntu@192.168.100.10
docker run -it --rm --network host \
    ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main
```

Inside the CLI:

```text
metadata Vehicle.Powertrain.TractionBattery.**
metadata Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed
metadata Vehicle.Cabin.Seat.Row1.DriverSide.Heating
metadata Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
metadata Vehicle.Powertrain.Range

subscribe Vehicle.Powertrain.Range
```

Now move a slider on the host dashboard and watch the `Range` line
update in the CLI.

### Manual restart / one-shot debug run

If you ever need to take a service down (e.g. to run with `--debug` or
attach a debugger), stop the unit first so it doesn't compete:

```bash
sudo systemctl stop ev-range-range-ai.service
cd /home/ubuntu/ev-range-extender/vm1
python3 range_ai.py
# ... then:
sudo systemctl start ev-range-range-ai.service
```

The same pattern works for `bms.py` and `zenoh_client.py`.

## CLI flags

`range_ai.py`:

| Flag | Default | Purpose |
|---|---|---|
| `--host` | `127.0.0.1` | Kuksa Databroker host |
| `--port` | `55555` | Kuksa Databroker port |

`bms.py`:

| Flag | Default | Purpose |
|---|---|---|
| `--listen` | `tcp/0.0.0.0:7460` | Zenoh listen endpoint (host dashboard dials it) |
| `--kuksa-host` | `127.0.0.1` | Local Kuksa Databroker host |
| `--kuksa-port` | `55555` | Local Kuksa Databroker port |

`zenoh_client.py`:

| Flag | Default | Purpose |
|---|---|---|
| `--listen` | `tcp/0.0.0.0:7447` | Zenoh listen endpoint (VM2's publisher dials it) |
| `--key` | `ev-range/vm2/**` | Zenoh key expression to subscribe to |
| `--kuksa-host` | `127.0.0.1` | Local Kuksa Databroker host |
| `--kuksa-port` | `55555` | Local Kuksa Databroker port |

## Troubleshooting

**A service shows `failed` after `systemctl is-active`**

```bash
ssh ubuntu@192.168.100.10 'sudo journalctl -u ev-range-bms.service -n 80 --no-pager'
ssh ubuntu@192.168.100.10 'tail -n 80 /tmp/ev-range-bms.log'
```

The most common cause is `ExecStartPre` timing out before the SDV
Runtime container reaches `:55555`. Each unit waits up to 10 minutes
for `:55555` and 5 minutes for `kuksa-client`/`zenoh` imports, with
`StartLimitIntervalSec=0`, so it normally reaches `active` even on
slow links. If it doesn't, the runtime container itself is unhealthy:

```bash
ssh ubuntu@192.168.100.10 'docker ps --filter name=sdv-runtime'
ssh ubuntu@192.168.100.10 'tail -n 50 /tmp/evrange-runtime.log'
ssh ubuntu@192.168.100.10 'sudo /usr/local/bin/evrange-start-runtime'
```

**`range_ai` keeps printing `<waiting for StateOfCharge to be set>`**

Battery signals haven't reached the Databroker yet. Move the
**Battery %** slider on the host dashboard. Then check:

```bash
ssh ubuntu@192.168.100.10 'tail -n 20 /tmp/ev-range-bms.log'
# expect: [bms] OK Vehicle.Powertrain.TractionBattery.StateOfCharge.Current = ...
```

If `bms.log` is empty, the Zenoh sample never arrived — check the
host's `iptables FORWARD` rule and the `--vm1` / `--bms-port` flags
passed to `pytk_dashboard.py`.

**`Vehicle.Cabin.*` paths show no value**

The cross-VM bridge is broken. Both ends must be `active`:

```bash
ssh ubuntu@192.168.100.11 'systemctl is-active ev-range-zenoh-publisher.service'
ssh ubuntu@192.168.100.10 'systemctl is-active ev-range-zenoh-client.service'
```

From VM2, `nc -zv 192.168.100.10 7447` must succeed (the host needs
`iptables -A FORWARD -i br0 -o br0 -j ACCEPT`).

**`ModuleNotFoundError: No module named 'kuksa_client'` / `'zenoh'`**

Cloud-init's pip install was incomplete (usually IPv6/SLIRP DNS).
Reinstall manually on VM1:

```bash
sudo pip3 install --break-system-packages --ignore-installed \
    kuksa-client eclipse-zenoh
sudo systemctl restart ev-range-bms.service \
                        ev-range-zenoh-client.service \
                        ev-range-range-ai.service
```

**`publish ... [get metadata] ... not_found` from a VM1 Kuksa CLI**

The wrong container image is running. Cloud-init now uses
`ghcr.io/eclipse-autowrx/sdv-runtime:latest` (preloaded with the
standard COVESA VSS catalog). Verify and recover:

```bash
ssh ubuntu@192.168.100.10 'docker inspect --format "{{.Config.Image}}" sdv-runtime'
# expected: ghcr.io/eclipse-autowrx/sdv-runtime:latest
ssh ubuntu@192.168.100.10 'sudo /usr/local/bin/evrange-start-runtime'
```

For host-side / cross-VM problems (dashboard, bridge, networking) see
the **Known issues and workarounds** section of the top-level
[`README.md`](../../README.md).
