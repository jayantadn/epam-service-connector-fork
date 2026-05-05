# EV Range Extender - VM2 (cabin / sensor) components

VM2 owns the **cabin signals** in the EV Range Extender architecture.
For this demo we use **only the Kuksa CLI on VM2** as the signal source
(no Python publisher / simulator scripts) and a single Python helper to
ferry the values to VM1 over Zenoh.

| Component | File | Status | Role |
|---|---|---|---|
| **Kuksa Databroker** | _Docker container `kuksa-databroker` (sdv-runtime image, same as VM1)_ | running via cloud-init | Local broker on VM2 (127.0.0.1:55555). Boots with the **standard COVESA VSS catalog already loaded** (no JSON file needed). The Kuksa CLI talks to this and `zenoh_publisher.py` subscribes to it. |
| **Kuksa CLI on VM2** | `ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main` | docker run, ad-hoc | The **only** signal source on VM2. Issues `publish ...` commands. |
| **Zenoh publisher** | `zenoh_publisher.py` | implemented | Subscribes to VM2's local Kuksa for the whitelisted cabin signals and republishes each update on Zenoh -> VM1. |

## Bridged signals (the three the Kuksa CLI on VM2 controls)

| VSS path | EPAM type | Datatype | Unit | Meaning |
|---|---|---|---|---|
| `Vehicle.Cabin.HVAC.AmbientAirTemperature` | sensor | float | °C | Ambient/cabin air temperature. Cold = battery efficiency loss + cabin heater load on VM1. |
| `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` | actuator | int8 | percent (0..100) | Driver-zone **heating** request. Higher = more cabin heating power, less range. |
| `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` | actuator | int8 | percent (-100..100) | Driver-zone combined actuator. **Negative = ventilation/cooling**, positive = heating. |

> **Why those exact paths?** The user-friendly forms `Seat.Heating` and
> `Seat.Ventilation` are not real VSS leaves. EPAM's VSS catalog (and
> COVESA upstream) splits the seat actuators into one branch per
> physical seat (`Row{1|2}.{DriverSide|Middle|PassengerSide}`) and uses
> **`HeatingCooling`** as the canonical name for the cooling/ventilation
> direction (negative percent = vent, positive percent = heat). There is
> no separate `.Ventilation` leaf anywhere in the EPAM JSON.

## End-to-end VM2 -> VM1 data flow

```
VM2                                                                 VM1
────                                                                ────
Kuksa CLI on VM2 (docker)                                           Kuksa CLI on VM1 (docker)
    │                                                               │
    │ publish Vehicle.Cabin.HVAC.AmbientAirTemperature 22.0         │ publish Vehicle.Powertrain.TractionBattery.* ...
    │ publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating 100        │
    │ publish Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling -50 │
    ▼                                                               ▼
VM2 Kuksa Databroker  ──── subscribe_current_values ────►    zenoh_publisher.py
(127.0.0.1:55555,                                            (subscribes to local Kuksa,
 EPAM VSS loaded                                              publishes JSON over Zenoh)
 via --metadata)                                                    │
                                                                    │  zenoh.put(...)
                                                                    │  ev-range/vm2/cabin/...
                                                                    │  ev-range/vm2/seat/...
                                                                    ▼
                                                       tcp/192.168.100.10:7447
                                                                    │
                                                                    ▼
                                                       VM1 zenoh_client.py
                                                                    │
                                                                    │  set_current_values
                                                                    ▼
                                                       VM1 ev-range Kuksa Databroker
                                                                    │
                                                                    ▼
                                                       VM1 range_ai.py
                                                       (recomputes Range)
```

## Prerequisites (on VM2)

Provided by VM2 cloud-init:
- `docker.io`, `eclipse-zenoh`, `kuksa-client` Python packages.
- `ghcr.io/eclipse-autowrx/sdv-runtime:latest` running as the
  `kuksa-databroker` container on `127.0.0.1:55555`. **The standard
  COVESA VSS catalog is preloaded** by that image - there is **no JSON
  file to copy in, no `--metadata` flag, no manual setup**.

Verify after boot (or after re-running cloud-init):
```bash
docker ps --filter name=kuksa-databroker
ss -ltn | grep 55555
python3 -c "from kuksa_client.grpc.aio import VSSClient; import zenoh; print('OK')"
```

If the broker is missing or unhealthy, restart it:
```bash
sudo /usr/local/bin/xmel-start-databroker
tail -n 50 /tmp/xmel-databroker.log
```

If `kuksa-client` is missing (older VM2 image):
```bash
sudo pip3 install --break-system-packages --ignore-installed kuksa-client
```

## VSS catalog sanity check

Open the Kuksa CLI on VM2 and confirm the three signals are already in
the catalog (they ship with `sdv-runtime`'s standard COVESA VSS):

```bash
docker run -it --rm --network host \
    ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main
```
```text
metadata Vehicle.Cabin.HVAC.AmbientAirTemperature
metadata Vehicle.Cabin.Seat.Row1.DriverSide.Heating
metadata Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
```
All three must return `[metadata] OK`. If any returns `not_found`, the
container is the wrong image - confirm:
```bash
docker inspect --format '{{.Config.Image}}' kuksa-databroker
# expected: ghcr.io/eclipse-autowrx/sdv-runtime:latest
```
If you see `ghcr.io/eclipse-kuksa/kuksa-databroker:main` (the bare
image, no preloaded VSS), recreate it via `sudo /usr/local/bin/xmel-start-databroker`
which now uses the right image.

## Stage `zenoh_publisher.py` on VM2

From the host:
```bash
cd /home/goutham/Gitrepos/epam-service-connector-fork/eclipse-sdv-blueprint/qemu-image-creator
sshpass -p 'ubuntu' scp -r ev-range-extender ubuntu@192.168.100.11:/home/ubuntu/
ssh ubuntu@192.168.100.11 "cd /home/ubuntu/ev-range-extender/vm2 && sed -i 's/\r$//' *.py"
```

## Run the demo (three terminals on VM2)

You'll need three `ssh ubuntu@192.168.100.11` shells open.

### Terminal D2 - Zenoh publisher (`zenoh_publisher.py`)

```bash
cd /home/ubuntu/ev-range-extender/vm2
python3 zenoh_publisher.py
```

Expected:
```
[zenoh-pub] Connected to local Kuksa.
[zenoh-pub] Subscribed Kuksa paths -> Zenoh keys:
[zenoh-pub]     Vehicle.Cabin.HVAC.AmbientAirTemperature
[zenoh-pub]       -> ev-range/vm2/cabin/Vehicle.Cabin.HVAC.AmbientAirTemperature  (unit=celsius)
[zenoh-pub]     Vehicle.Cabin.Seat.Row1.DriverSide.Heating
[zenoh-pub]       -> ev-range/vm2/seat/Vehicle.Cabin.Seat.Row1.DriverSide.Heating  (unit=percent)
[zenoh-pub]     Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
[zenoh-pub]       -> ev-range/vm2/seat/Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling  (unit=percent)
[zenoh-pub] Opening Zenoh session, connecting to tcp/192.168.100.10:7447
[zenoh-pub] Publisher running. Drive values from the Kuksa CLI on VM2 ...
```

### Terminal D1 - Kuksa CLI on VM2 (publishes signals)

```bash
docker run -it --rm --network host ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main
```

Inside the CLI you can now drive any of the three signals:

```text
# Phase 6 - cabin temperature scenarios
publish Vehicle.Cabin.HVAC.AmbientAirTemperature 22.0
publish Vehicle.Cabin.HVAC.AmbientAirTemperature 10.0
publish Vehicle.Cabin.HVAC.AmbientAirTemperature 0.0
publish Vehicle.Cabin.HVAC.AmbientAirTemperature -10.0

# Phase 7 - seat heater + ventilation scenarios
publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating 100
publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating 50
publish Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling -100
publish Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling 50
publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating 0
publish Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling 0
```

Every `publish` should:
1. Update VM2's local Databroker.
2. Trigger a `[zenoh-pub] FWD ...` line in Terminal D2.
3. Trigger a `[zenoh-cli] OK   ...` line in VM1's `zenoh_client.py` terminal.
4. Trigger a `[range-ai] input  : ... = ...` and `[range-ai] output : Vehicle.Powertrain.Range = N km ...` line in VM1's `range_ai.py` terminal.

### Optional - Terminal D3 - watch Range update live (cross-VM verification)

You can also subscribe to `Vehicle.Powertrain.Range` from VM1 by SSHing
to VM1 and opening a CLI there - that confirms the round-trip.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `publish ... [get metadata] OK Error [Error { code: 404 ... }]` on VM2 CLI | The container is the bare `kuksa-databroker:main` image (no VSS preloaded) instead of `sdv-runtime:latest` | `sudo /usr/local/bin/xmel-start-databroker` (uses the correct image now) |
| VM2 publishes but VM1 sees nothing | `zenoh_client.py` not running on VM1, or `iptables FORWARD -i br0 -o br0 -j ACCEPT` missing on host | start the client; add the rule |
| `range_ai` says `<waiting for StateOfCharge ...>` | You haven't published any `StateOfCharge.Current` from the Kuksa CLI on **VM1** yet | run Phase 1 first |
| `ModuleNotFoundError: kuksa_client` | `kuksa-client` pip package missing on VM2 | `sudo pip3 install --break-system-packages --ignore-installed kuksa-client` |
| `[zenoh-pub] FATAL: ... 5 ... not found` | VSS path not in catalog (catalog out of date, or wrong image) | check `docker inspect kuksa-databroker` shows `sdv-runtime:latest` |

## What about `Vehicle.Cabin.Seat.Row[*].Seat[*].Ventilation`?

The path you originally asked about doesn't exist anywhere in the EPAM
VSS catalog (or upstream COVESA). The closest semantic match is the
**negative side of `HeatingCooling`** — that's what we use for the
"ventilation" scenarios in Phase 7 below. If you need a dedicated
ventilation actuator later, the cabin-fan path is:

```
Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed     (actuator, uint8, percent)
```

Adding it would be one entry in `BRIDGED_PATHS` on both `vm2/zenoh_publisher.py`
and `vm1/zenoh_client.py`, plus one constant in `vm1/range_ai.py`.
