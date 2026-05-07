# EV Range Extender - VM2 (Zonal / cabin / sensor) components

VM2 owns the **cabin signals** in the EV Range Extender architecture.
For this demo we use **only the Kuksa CLI on VM2** as the signal source
(no Python publisher / simulator scripts) and a Python helper to ferry
the values to VM1 over **SOME/IP** (the upstream blueprint's
"Eclipse SCore / SOME-IP" link). A Zenoh publisher is kept on disk
as a reference / legacy alternative.

| Component | File | Status | Role |
|---|---|---|---|
| **Kuksa Databroker** | _Docker container `kuksa-databroker` (sdv-runtime image, same as VM1)_ | running via cloud-init | Local broker on VM2 (127.0.0.1:55555). Boots with the **standard COVESA VSS catalog already loaded** (no JSON file needed). The Kuksa CLI talks to this and the publisher subscribes to it. |
| **Kuksa CLI on VM2** | `ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main` | docker run, ad-hoc | The **only** signal source on VM2. Issues `publish ...` commands. |
| **SOME/IP publisher** (active) | `someip_publisher.py` | implemented | Subscribes to VM2's local Kuksa for the whitelisted cabin signals and re-emits each update as a SOME/IP notification event (service `0xCAB0`, eventgroup `0x0001`, events `0x8001..0x8003`) towards VM1's `someip_client.py`. Wire encoding lives in `../common/someip_service.py`. |
| Zenoh publisher (legacy) | `zenoh_publisher.py` | implemented | Same role as `someip_publisher.py` but over Eclipse Zenoh on `tcp/192.168.100.10:7447`. Kept on disk as a reference transport - run **either** the SOME/IP path **or** the Zenoh path, not both. |

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

## End-to-end VM2 -> VM1 data flow (active SOME/IP transport)

```
VM2 [Zonal]                                                         VM1 [HPC]
───────────                                                         ─────────
Kuksa CLI on VM2 (docker)                                           Kuksa CLI on VM1 (docker)
    │                                                               │
    │ publish Vehicle.Cabin.HVAC.AmbientAirTemperature 22.0         │ publish Vehicle.Powertrain.TractionBattery.* ...
    │ publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating 100        │
    │ publish Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling -50 │
    ▼                                                               ▼
VM2 Kuksa Databroker  ──── subscribe_current_values ────►   someip_publisher.py
(127.0.0.1:55555,                                           (subscribes to local Kuksa,
 COVESA VSS loaded                                           encodes payload, sends
 by sdv-runtime)                                             SOME/IP notification event)
                                                                    │
                                                                    │  service 0xCAB0
                                                                    │  eventgroup 0x0001
                                                                    │  event 0x8001  (AmbientAirTemperature, float, 4 B)
                                                                    │  event 0x8002  (Seat.Heating, int8, 1 B)
                                                                    │  event 0x8003  (Seat.HeatingCooling, int8, 1 B)
                                                                    ▼
                                                       udp/30490 (SOME/IP-SD multicast 224.224.224.245)
                                                       udp/30509 (event traffic from VM2)
                                                       udp/30510 (event traffic to VM1)
                                                                    │
                                                                    ▼
                                                       VM1 someip_client.py
                                                                    │
                                                                    │  decode_event(event_id, payload)
                                                                    │  set_current_values
                                                                    ▼
                                                       VM1 ev-range Kuksa Databroker
                                                                    │
                                                                    ▼
                                                       VM1 range_ai.py
                                                       (recomputes Range)
```

> **Legacy Zenoh path.** Replace `someip_publisher.py` (this side) and
> `someip_client.py` (VM1) with `zenoh_publisher.py` /
> `zenoh_client.py`. The wire becomes JSON over `tcp/7447` on Zenoh
> keys `ev-range/vm2/cabin/...` and `ev-range/vm2/seat/...`; the
> Kuksa side is unchanged.

## Prerequisites (on VM2)

Provided by VM2 cloud-init:
- `docker.io`, `someipy` (>=1.0,<2.0), `eclipse-zenoh`, `kuksa-client`
  Python packages.
- `ghcr.io/eclipse-autowrx/sdv-runtime:latest` running as the
  `kuksa-databroker` container on `127.0.0.1:55555`. **The standard
  COVESA VSS catalog is preloaded** by that image - there is **no JSON
  file to copy in, no `--metadata` flag, no manual setup**.

Verify after boot (or after re-running cloud-init):
```bash
docker ps --filter name=kuksa-databroker
ss -ltn | grep 55555
python3 -c "from kuksa_client.grpc.aio import VSSClient; import someipy, zenoh; print('OK')"
```

If the broker is missing or unhealthy, restart it:
```bash
sudo /usr/local/bin/xmel-start-databroker
tail -n 50 /tmp/xmel-databroker.log
```

If any of `kuksa-client` / `someipy` / `eclipse-zenoh` is missing
(older VM2 image):
```bash
sudo pip3 install --break-system-packages --ignore-installed \
    kuksa-client 'someipy>=1.0,<2.0' eclipse-zenoh
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

## Stage the publishers on VM2

The `ev-range-extender/` folder contains both publishers - the
SOME/IP one (active) and the Zenoh one (legacy) - plus the shared
`common/someip_service.py` they import. Copy the whole tree:

```bash
cd /home/goutham/Gitrepos/epam-service-connector-fork/eclipse-sdv-blueprint/qemu-image-creator
sshpass -p 'ubuntu' scp -r ev-range-extender ubuntu@192.168.100.11:/home/ubuntu/
ssh ubuntu@192.168.100.11 \
    "cd /home/ubuntu/ev-range-extender && sed -i 's/\r$//' vm2/*.py common/*.py"
```

## Run the demo (three terminals on VM2)

You'll need three `ssh ubuntu@192.168.100.11` shells open.

### Terminal D2 - SOME/IP publisher (`someip_publisher.py`, ACTIVE)

```bash
cd /home/ubuntu/ev-range-extender/vm2
python3 someip_publisher.py
```

Expected:
```
[someip-pub] Connecting to local Kuksa Databroker at 127.0.0.1:55555...
[someip-pub] Connected to local Kuksa.
[someip-pub] Subscribed Kuksa paths -> SOME/IP events:
[someip-pub]     Vehicle.Cabin.HVAC.AmbientAirTemperature
[someip-pub]       -> event 0x8001  (unit=celsius)
[someip-pub]     Vehicle.Cabin.Seat.Row1.DriverSide.Heating
[someip-pub]       -> event 0x8002  (unit=percent)
[someip-pub]     Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
[someip-pub]       -> event 0x8003  (unit=percent)
[someip-pub] Starting SOME/IP-SD on 224.224.224.245:30490 via interface 192.168.100.11
[someip-pub] Constructing ServerServiceInstance (service=0xcab0, instance=0x0001, endpoint=192.168.100.11:30509, ttl=5s)
[someip-pub] Starting cyclic SD offers (every 2000 ms)...
[someip-pub] Publisher running. Drive values from the Kuksa CLI on VM2 ...
```

> **Legacy alternative**: `python3 zenoh_publisher.py` instead. The
> rest of the demo (Terminal D1, all `publish ...` commands) is
> identical.

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

Every `publish` should (active SOME/IP path):
1. Update VM2's local Databroker.
2. Trigger a `[someip-pub] FWD ... -> someip event 0x800N (...B)` line in Terminal D2.
3. Trigger a `[someip-cli] OK   ... = ... (from someip event 0x800N)` line in VM1's `someip_client.py` terminal.
4. Trigger a `[range-ai] input  : ... = ...` and `[range-ai] output : Vehicle.Powertrain.Range = N km ...` line in VM1's `range_ai.py` terminal.

(Legacy Zenoh path: `[zenoh-pub] FWD ...` in D2 and `[zenoh-cli] OK ...` in C.)

### Optional - Terminal D3 - watch Range update live (cross-VM verification)

You can also subscribe to `Vehicle.Powertrain.Range` from VM1 by SSHing
to VM1 and opening a CLI there - that confirms the round-trip.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `publish ... [get metadata] OK Error [Error { code: 404 ... }]` on VM2 CLI | The container is the bare `kuksa-databroker:main` image (no VSS preloaded) instead of `sdv-runtime:latest` | `sudo /usr/local/bin/xmel-start-databroker` (uses the correct image now) |
| VM2 publishes but VM1 sees nothing (SOME/IP) | `someip_client.py` not running on VM1, or `iptables FORWARD -i br0 -o br0 -j ACCEPT` missing on host (also blocks SD multicast) | start the client; add the rule |
| VM2 publishes but VM1 sees nothing (Zenoh legacy) | `zenoh_client.py` not running on VM1, or same `iptables FORWARD` issue | start the client; add the rule |
| `range_ai` says `<waiting for StateOfCharge ...>` | You haven't published any `StateOfCharge.Current` from the Kuksa CLI on **VM1** yet | run Phase 1 first |
| `ModuleNotFoundError: kuksa_client` / `someipy` / `zenoh` | One of the pip packages is missing on VM2 | `sudo pip3 install --break-system-packages --ignore-installed kuksa-client 'someipy>=1.0,<2.0' eclipse-zenoh` |
| `[someip-pub] ERROR sending event 0x800N ...` | someipy lost its event-port socket - usually because another process is on `udp/30509` | `pkill -f someip_publisher.py`; check `ss -lun | grep 30509`; restart |
| `[someip-pub] FATAL: ... 5 ... not found` / `[zenoh-pub] FATAL: ...` | VSS path not in catalog (catalog out of date, or wrong image) | check `docker inspect kuksa-databroker` shows `sdv-runtime:latest` |
| Want to inspect SOME/IP on the wire | - | `sudo tcpdump -ni any -X 'udp port 30490 or udp port 30509 or udp port 30510'` (or load a `.pcap` in Wireshark - the built-in `someip` dissector decodes everything) |

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
and `vm1/zenoh_client.py` (legacy path), one entry in `VSS_TO_EVENT`
plus a codec in `common/someip_service.py` (active SOME/IP path), and
one constant in `vm1/range_ai.py`.
