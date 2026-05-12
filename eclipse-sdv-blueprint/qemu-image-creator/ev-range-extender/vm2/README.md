# EV Range Extender — VM2 (Zonal / cabin) components

VM2 is the **Zonal** node. It hosts a `digital.auto sdv-runtime`
container (Kuksa Databroker on `127.0.0.1:55555`, named
`ev-range-cabin`, COVESA VSS catalog preloaded) plus three Python
applications that are auto-deployed by cloud-init and auto-started as
`systemd` services on first boot. **No file is started by hand on VM2.**

| Component | File | systemd unit | Role |
|---|---|---|---|
| **HVAC ECU** | `hvac_ecu.py` | `ev-range-hvac.service` | Listens on `tcp/0.0.0.0:7461` for the host PyTk dashboard's `sim/cabin/fan-speed` Zenoh key and writes the value into `Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed` in the local Kuksa. |
| **Seat ECU** | `seat_ecu.py` | `ev-range-seat.service` | Listens on `tcp/0.0.0.0:7462` for the host PyTk dashboard's `sim/cabin/seat/heating` and `sim/cabin/seat/hc` Zenoh keys and writes them into `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` and `…HeatingCooling` respectively. |
| **Zenoh publisher (VM2 → VM1 bridge — sending end)** | `zenoh_publisher.py` | `ev-range-zenoh-publisher.service` | Subscribes to the local Kuksa for the three cabin paths above and forwards every update over Zenoh to VM1's `zenoh_client.py` on `tcp/192.168.100.10:7447`. |

All three services depend only on the local Kuksa Databroker on
`127.0.0.1:55555` and on the Python packages cloud-init installs
(`kuksa-client`, `eclipse-zenoh`). The transport between host↔ECU and
between VM2↔VM1 is **only Eclipse Zenoh** — no SOME/IP, no gRPC, no
manual `scp`.

## Cabin signals owned on VM2

`hvac_ecu.py` and `seat_ecu.py` are the **only writers** of the cabin
branch on VM2. They only mirror what the host dashboard publishes; they
never originate a value of their own.

| VSS path | EPAM type | Datatype | Unit | Driven by (dashboard control) |
|---|---|---|---|---|
| `Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed` | actuator | uint8 | % (0..100) | **Fan Speed** slider (`sim/cabin/fan-speed`) |
| `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` | actuator | int8 | % (0..100) | **Seat Heating** toggle (`sim/cabin/seat/heating`; ON = 100, OFF = 0) |
| `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` | actuator | int8 | % (-100..100) | **Seat Cooling** toggle (`sim/cabin/seat/hc`; ON = -100, OFF = 0; positive values = heating, negative = cooling/ventilation) |

> **Why those exact paths?** The user-friendly forms `Seat.Heating` and
> `Seat.Ventilation` are not real VSS leaves. EPAM's VSS catalog (and
> COVESA upstream) splits the seat actuators into one branch per
> physical seat (`Row{1|2}.{DriverSide|Middle|PassengerSide}`) and uses
> **`HeatingCooling`** as the canonical name for the cooling/ventilation
> direction (negative = vent, positive = heat). There is no
> `Seat.Ventilation` leaf anywhere in the EPAM JSON or the COVESA VSS
> 4.x catalog the SDV Runtime ships with.
>
> **Why `Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed` and not just
> `HVAC.AmbientAirTemperature`?** The dashboard is meant to map 1:1 to
> controls a real driver would touch. A driver does not "set ambient
> temperature" directly — they raise / lower the blower fan. `FanSpeed`
> is the actuator, and the Range AI on VM1 uses it as an additive HVAC
> compressor + blower power load.

## End-to-end data flow seen from VM2

```
HOST (192.168.100.1)                                         VM2 (192.168.100.11)
hardware-sim/pytk_dashboard.py
  sim/cabin/fan-speed     ──zenoh tcp/7461──▶ hvac_ecu.py  ──▶ Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed
  sim/cabin/seat/heating  ──zenoh tcp/7462──▶ seat_ecu.py  ──▶ Vehicle.Cabin.Seat.Row1.DriverSide.Heating
  sim/cabin/seat/hc       ──zenoh tcp/7462──▶ seat_ecu.py  ──▶ Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
                                                                      │
                                                  ┌───────────────────┴──────────────────────────┐
                                                  │  ev-range-cabin Kuksa Databroker             │
                                                  │  (127.0.0.1:55555, sdv-runtime image,        │
                                                  │   standard COVESA VSS catalog preloaded)     │
                                                  └───────────────────┬──────────────────────────┘
                                                                      │ subscribe_current_values
                                                                      ▼
                                                            zenoh_publisher.py
                                                                      │ zenoh.put(JSON)
                                                                      │ keys: ev-range/vm2/cabin/* and ev-range/vm2/seat/*
                                                                      ▼   tcp/192.168.100.10:7447
                                                            VM1 zenoh_client.py
                                                                      │
                                                                      ▼
                                                            VM1 ev-range Kuksa  →  range_ai.py
```

The bridge does **not** create a new VSS path on VM1: each forwarded
sample carries its original `Vehicle.Cabin.*` path inside the JSON
payload, so the value lands at the same address on both sides.

### Wire format

Each Zenoh sample is a tiny JSON document, identical on every hop:

```json
{
  "value": 100,
  "path":  "Vehicle.Cabin.Seat.Row1.DriverSide.Heating",
  "unit":  "percent",
  "timestamp": "2026-…Z",
  "source":    "<hostname>"
}
```

(`hvac_ecu.py` and `seat_ecu.py` use the simpler `{value, source, ts}`
shape — the receiving Zenoh key already encodes the VSS path.)

## Operating the services on VM2

Everything below assumes you have already run `./setup.sh` and
`./vm2_launch.sh` on the host; the three services start themselves as
soon as cloud-init finishes installing pip packages and the SDV
Runtime container. There is **no `python3 …` step** during the demo —
only the host PyTk dashboard.

### Status

```bash
ssh ubuntu@192.168.100.11 'systemctl is-active ev-range-hvac.service \
                                                ev-range-seat.service \
                                                ev-range-zenoh-publisher.service'
# expected: 3 lines saying "active"
```

```bash
ssh ubuntu@192.168.100.11 'ss -ltn | grep -E ":55555|:7461|:7462"'
# expected lines for: 55555 (Kuksa), 7461 (hvac_ecu), 7462 (seat_ecu)
```

`zenoh_publisher.py` is a Zenoh **client** (it dials VM1, no listening
port on VM2).

### Logs

Each service writes its stdout/stderr to a world-readable file under
`/tmp/`. Plain `tail -f` works without `sudo`:

```bash
ssh ubuntu@192.168.100.11 'tail -f /tmp/ev-range-hvac.log'
ssh ubuntu@192.168.100.11 'tail -f /tmp/ev-range-seat.log'
ssh ubuntu@192.168.100.11 'tail -f /tmp/ev-range-zenoh-publisher.log'
```

For the full systemd journal of any unit:

```bash
ssh ubuntu@192.168.100.11 'sudo journalctl -u ev-range-zenoh-publisher.service -n 80 --no-pager'
```

### Verify the catalog and inspect values from a Kuksa CLI on VM2

```bash
ssh ubuntu@192.168.100.11
docker run -it --rm --network host \
    ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main
```

Inside the CLI:

```text
metadata Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed
metadata Vehicle.Cabin.Seat.Row1.DriverSide.Heating
metadata Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling

get Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed
get Vehicle.Cabin.Seat.Row1.DriverSide.Heating
get Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
```

All three `metadata` calls must return `[metadata] OK`. Move a slider /
toggle on the host dashboard and the next `get` will show the new
value.

### Manual restart / one-shot debug run

If you need to run a service in the foreground (e.g. with `--debug` or
to attach a debugger), stop the unit first so it doesn't compete:

```bash
sudo systemctl stop ev-range-zenoh-publisher.service
cd /home/ubuntu/ev-range-extender/vm2
python3 zenoh_publisher.py
# ... then:
sudo systemctl start ev-range-zenoh-publisher.service
```

The same pattern works for `hvac_ecu.py` and `seat_ecu.py`.

## CLI flags

`hvac_ecu.py`:

| Flag | Default | Purpose |
|---|---|---|
| `--listen` | `tcp/0.0.0.0:7461` | Zenoh listen endpoint (host dashboard dials it) |
| `--kuksa-host` | `127.0.0.1` | Local Kuksa Databroker host |
| `--kuksa-port` | `55555` | Local Kuksa Databroker port |

`seat_ecu.py`:

| Flag | Default | Purpose |
|---|---|---|
| `--listen` | `tcp/0.0.0.0:7462` | Zenoh listen endpoint |
| `--kuksa-host` | `127.0.0.1` | Local Kuksa Databroker host |
| `--kuksa-port` | `55555` | Local Kuksa Databroker port |

`zenoh_publisher.py`:

| Flag | Default | Purpose |
|---|---|---|
| `--zenoh-peer` | `tcp/192.168.100.10:7447` | VM1's `zenoh_client.py` listener |
| `--kuksa-host` | `127.0.0.1` | Local Kuksa Databroker host |
| `--kuksa-port` | `55555` | Local Kuksa Databroker port |

## Troubleshooting

**`publish … not_found` / `[get metadata] OK Error [Error { code: 404 }]` on VM2 CLI**

The container is the bare `kuksa-databroker:main` image (no VSS
preloaded) instead of `sdv-runtime:latest`. Cloud-init now uses the
correct image; one-time recovery:

```bash
ssh ubuntu@192.168.100.11
sudo /usr/local/bin/evrange-start-databroker
sudo systemctl restart ev-range-hvac.service \
                        ev-range-seat.service \
                        ev-range-zenoh-publisher.service
```

Verify:
```bash
docker inspect --format '{{.Config.Image}}' kuksa-databroker
# expected: ghcr.io/eclipse-autowrx/sdv-runtime:latest
```

**`zenoh_publisher.service` is `active` but VM1 sees no cabin updates**

In order, check:
1. The host's bridge-forwarding rule:
   `sudo iptables -L FORWARD -n -v | grep -E 'br0.*br0'` — if missing,
   `sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT`.
2. VM1's `zenoh_client.py` is up:
   `ssh ubuntu@192.168.100.10 'systemctl is-active ev-range-zenoh-client.service'`.
3. From VM2: `nc -zv 192.168.100.10 7447` must succeed.
4. `tail -n 50 /tmp/ev-range-zenoh-publisher.log` should print
   `FWD  Vehicle.Cabin.* = …  ->  zenoh (… B)` lines whenever the
   dashboard moves.

**`ModuleNotFoundError: No module named 'kuksa_client'` / `'zenoh'`**

Cloud-init's pip install was incomplete (usually IPv6/SLIRP DNS).
Reinstall manually on VM2:

```bash
sudo pip3 install --break-system-packages --ignore-installed \
    kuksa-client eclipse-zenoh
sudo systemctl restart ev-range-hvac.service \
                        ev-range-seat.service \
                        ev-range-zenoh-publisher.service
```

**A unit shows `failed` after `systemctl is-active`**

```bash
ssh ubuntu@192.168.100.11 'sudo journalctl -u ev-range-hvac.service -n 80 --no-pager'
ssh ubuntu@192.168.100.11 'tail -n 80 /tmp/ev-range-hvac.log'
```

The most common cause is `ExecStartPre` timing out before the SDV
Runtime container reaches `:55555`. Each unit waits up to 10 minutes
for `:55555` and 5 minutes for `kuksa-client`/`zenoh` imports, with
`StartLimitIntervalSec=0`, so it normally reaches `active` even on
slow links. If it doesn't, the runtime container itself is unhealthy:

```bash
ssh ubuntu@192.168.100.11 'docker ps --filter name=kuksa-databroker'
ssh ubuntu@192.168.100.11 'tail -n 50 /tmp/evrange-databroker.log'
ssh ubuntu@192.168.100.11 'sudo /usr/local/bin/evrange-start-databroker'
```

For host-side / cross-VM problems (dashboard, bridge, networking) see
the **Known issues and workarounds** section of the top-level
[`README.md`](../../README.md).
