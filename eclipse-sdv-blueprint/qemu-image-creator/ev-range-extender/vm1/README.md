# EV Range Extender - VM1 (HPC) components

The HPC-VM in the EV Range Extender architecture (`eclipse-sdv-blueprint/README.md`)
hosts the digital.auto runtime + apps. This folder is where those VM1
apps live, one Python file at a time.

| Component | File | Status | Role |
|---|---|---|---|
| **Range Compute AI** | `range_ai.py` | implemented | Subscribes to the 3 battery signals on Kuksa, computes range, publishes `Vehicle.Powertrain.Range` |
| Power-save manager | _planned_ | next milestone | Subscribes to `Range`, publishes Cabin signals (`HVAC.FanSpeed`, `Seat.*.Heating`, `Seat.*.Ventilation`) - sent to VM2 over Zenoh |

> **Note on the BMS:** the diagram shows a separate "Battery Monitoring
> System" box that publishes the three battery signals. For this demo
> we don't run a BMS app at all - we publish the signals **directly from
> the Kuksa CLI** (acting as the simulated battery sensor source).
> Range AI doesn't care where the signals come from, only that they
> appear on the local Databroker.

## End-to-end VM1 data flow

```
   Kuksa CLI                                                                 range_ai.py
   ─────────                                                                 ───────────
        │                                                                          │
        │   publish Vehicle.Powertrain.TractionBattery.CurrentCurrent      25.5    │
        │   publish Vehicle.Powertrain.TractionBattery.CurrentVoltage     400.0    │ subscribe_current_values
        │   publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current 100   │   (3 battery signals)
        │                                                                          │
        ▼                                                                          │
   ┌────────────────────────────────────────────────────────────────────────────────┴─┐
   │                  Kuksa Databroker  (ev-range, 127.0.0.1:55555)                  │
   │   Vehicle.Powertrain.TractionBattery.CurrentCurrent           (A)               │
   │   Vehicle.Powertrain.TractionBattery.CurrentVoltage           (V)               │
   │   Vehicle.Powertrain.TractionBattery.StateOfCharge.Current    (%)               │
   │   Vehicle.Powertrain.Range                                    (km)              │ <-- range_ai
   └─────────────────────────────────────────────────────────────────────────────────┘    publishes here
```

All four signals end up **recorded in the ev-range Kuksa Databroker**, which
is exactly what the inside of the VM1 box in the diagram demands.

> **Why `TractionBattery.*` and not `Battery.*`?** The diagram uses the
> short form for clarity, but the canonical COVESA VSS 4.x catalog
> (which `ghcr.io/eclipse-autowrx/sdv-runtime` ships with) places these
> signals under `Vehicle.Powertrain.TractionBattery.*`. You can confirm
> what's available in your runtime by running `metadata
> Vehicle.Powertrain.TractionBattery.**` inside the Kuksa CLI.

## What `range_ai.py` does

Connects to the local Kuksa Databroker on `127.0.0.1:55555` and:

1. **Subscribes** to the three battery telemetry signals:

| VSS path | Unit | Source for the demo |
|---|---|---|
| `Vehicle.Powertrain.TractionBattery.CurrentCurrent` | A | `publish` from the Kuksa CLI |
| `Vehicle.Powertrain.TractionBattery.CurrentVoltage` | V | `publish` from the Kuksa CLI |
| `Vehicle.Powertrain.TractionBattery.StateOfCharge.Current` | % | `publish` from the Kuksa CLI |

2. On every update, **computes the estimated remaining driving range**:

```
available_kWh = (SoC / 100) * 75 kWh
consumption   = 0.18 kWh/km, scaled up if instantaneous power > 18 kW
range_km      = available_kWh / consumption
```

| SoC | Range at idle/cruise (P <= 18 kW) | Range under hard discharge (P = 36 kW) |
|---|---|---|
| 100 % | ~417 km | ~208 km |
| 50 %  | ~208 km | ~104 km |
| 12 %  | ~50 km  | ~25 km  |

The model parameters (battery capacity, nominal consumption, cruise power)
are constants at the top of `range_ai.py` - tweak to model a different vehicle.

3. **Publishes** the result back to the same Databroker as
   `Vehicle.Powertrain.Range` (km).

Until `StateOfCharge` is set at least once, Range AI logs
`<waiting for StateOfCharge to be set>` and publishes nothing.

## Prerequisites (on VM1)

All already provided by the existing cloud-init on VM1:

| Thing | Where it comes from |
|---|---|
| `kuksa-client` Python package | `input/user-data-vm1` line 207 (`pip3 install ... kuksa-client ...`) |
| Kuksa Databroker on `127.0.0.1:55555` | `xmel-start-runtime` script - runs the `ghcr.io/eclipse-autowrx/sdv-runtime:latest` container with `--network host` and `RUNTIME_NAME=ev-range` |
| Docker (for the Kuksa CLI container) | Cloud-init installs `docker.io` |

Pre-flight sanity checks on VM1:

```bash
# 1. kuksa-client importable
python3 -c "from kuksa_client.grpc.aio import VSSClient; print('kuksa-client OK')"

# 2. Databroker reachable on :55555
ss -ltn | grep 55555

# 3. ev-range runtime container alive
docker ps --filter name=sdv-runtime
```

If any of those fail, see Troubleshooting at the bottom.

## Step-by-step demo

You'll need **2 SSH terminals to VM1** (3 if you want to live-watch
`Vehicle.Powertrain.Range` from the CLI in parallel). All commands
assume password `ubuntu`.

### Step 1 - copy the app to VM1 (from the host)

```bash
cd /home/goutham/Gitrepos/epam-service-connector-fork/eclipse-sdv-blueprint/qemu-image-creator

sshpass -p 'ubuntu' scp -r ev-range-extender ubuntu@192.168.100.10:/home/ubuntu/
```

### Step 2 - Terminal A: start Range Compute AI

```bash
ssh ubuntu@192.168.100.10
cd /home/ubuntu/ev-range-extender/vm1
python3 range_ai.py
```

Expected:

```
[range-ai] Connecting to Kuksa Databroker at 127.0.0.1:55555...
[range-ai] Connected.
[range-ai]   Subscribing to 3 battery signal(s):
[range-ai]     - Vehicle.Powertrain.TractionBattery.CurrentCurrent
[range-ai]     - Vehicle.Powertrain.TractionBattery.CurrentVoltage
[range-ai]     - Vehicle.Powertrain.TractionBattery.StateOfCharge.Current
[range-ai]   Will publish to:
[range-ai]     - Vehicle.Powertrain.Range
[range-ai]   Model: capacity=75.0 kWh, consumption=0.18 kWh/km, cruise=18.0 kW
[range-ai] output : <waiting for StateOfCharge to be set>
```

It is now waiting for the CLI to publish.

### Step 3 - Terminal B: open the Kuksa CLI (the simulated sensor source)

```bash
ssh ubuntu@192.168.100.10
docker run -it --rm --network host \
    ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main
```

You'll get an interactive prompt that talks to the same Databroker on
`127.0.0.1:55555`.

### Step 4 - confirm the catalog has the signals (one-time)

In Terminal B (the CLI), discover what battery signals the runtime
exposes:

```text
metadata Vehicle.Powertrain.TractionBattery.**
metadata Vehicle.Powertrain.Range
```

You should see entries for `CurrentCurrent`, `CurrentVoltage`,
`StateOfCharge.Current`, and `Range`. If your runtime image uses
different paths, update the constants `SIGNAL_CURRENT`, `SIGNAL_VOLTAGE`,
`SIGNAL_SOC` at the top of `range_ai.py` accordingly.

### Step 5 - run the 5-phase EV Range Extender scenario

Paste the blocks below into **Terminal B (the CLI)** in order. After
each block, switch to **Terminal A** and watch `range_ai.py` log a fresh
`Vehicle.Powertrain.Range` (it republishes the new range to the
ev-range Databroker on every input update).

The expected `Range` values come from the formula in `range_ai.py`
(`75 kWh * SoC / 0.18 kWh/km`, scaled up when `I*V > 18 kW`).

#### Phase 1 — cold start, fully charged

```text
publish Vehicle.Powertrain.TractionBattery.CurrentVoltage         420.0
publish Vehicle.Powertrain.TractionBattery.CurrentCurrent          25.5
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current  100.0
```

Terminal A (after the third publish, once SoC is known):

```
[range-ai] input  : Vehicle.Powertrain.TractionBattery.StateOfCharge.Current = 100.000
[range-ai] output : Vehicle.Powertrain.Range = 417 km (computed 416.7 km; SoC=100.000 %, I=25.500 A, U=420.000 V)
```

> Power = 420 V * 25.5 A = 10.7 kW, **below** the 18 kW cruise
> threshold, so the nominal 0.18 kWh/km consumption is used.

#### Phase 2 — normal cruising drains the battery

```text
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   75
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   50
```

Terminal A:

```
[range-ai] output : Vehicle.Powertrain.Range = 312 km (SoC=75.000 %, ...)
[range-ai] output : Vehicle.Powertrain.Range = 208 km (SoC=50.000 %, ...)
```

#### Phase 3 — hard acceleration (current spikes)

```text
publish Vehicle.Powertrain.TractionBattery.CurrentCurrent          90.0
```

Terminal A:

```
[range-ai] output : Vehicle.Powertrain.Range = 99 km (SoC=50.000 %, I=90.000 A, U=420.000 V)
```

> Power = 420 V * 90 A = **37.8 kW** ≈ 2.1x cruise threshold, so
> consumption scales up by the same factor and range collapses from
> 208 km to 99 km even though SoC is unchanged.

#### Phase 4 — driver eases off, voltage sags from sustained load

```text
publish Vehicle.Powertrain.TractionBattery.CurrentCurrent          25.5
publish Vehicle.Powertrain.TractionBattery.CurrentVoltage         380.0
```

Terminal A:

```
[range-ai] output : Vehicle.Powertrain.Range = 208 km (SoC=50.000 %, I=25.500 A, U=420.000 V)
[range-ai] output : Vehicle.Powertrain.Range = 208 km (SoC=50.000 %, I=25.500 A, U=380.000 V)
```

> Current returns to nominal so power drops back below the cruise
> threshold and range recovers to 208 km. The voltage sag from 420 V
> to 380 V (P = 9.7 kW, still cruise) doesn't move the number - by
> design the model only penalises range when **power exceeds the
> cruise threshold**.

#### Phase 5 — critical SoC, low-battery trigger

```text
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   12
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   25
```

Terminal A:

```
[range-ai] output : Vehicle.Powertrain.Range = 50 km  (SoC=12.000 %, ...)   <-- trigger
[range-ai] output : Vehicle.Powertrain.Range = 104 km (SoC=25.000 %, ...)   <-- recovers (fast-DC stop)
```

> **The "wow moment".** `Range = 50 km` is the trigger condition for
> the EV Range Extender feature. In the next milestone the Power-save
> manager will subscribe to `Vehicle.Powertrain.Range`, react to this
> threshold, and publish reduced fan speed + disabled seat
> heating/ventilation over Zenoh to VM2. The second publish in this
> phase (SoC = 25%) simulates a fast-DC charging stop and demonstrates
> that range recovers automatically once SoC climbs back.

### Step 6 - verify all 4 signals are recorded on the ev-range runtime

Still in the CLI:

```text
get Vehicle.Powertrain.TractionBattery.CurrentCurrent
get Vehicle.Powertrain.TractionBattery.CurrentVoltage
get Vehicle.Powertrain.TractionBattery.StateOfCharge.Current
get Vehicle.Powertrain.Range
```

All four return values. To stream the computed Range live (so you can
watch it update as you set new battery values):

```text
subscribe Vehicle.Powertrain.Range
```

Then in another CLI session (or after stopping the subscription) keep
poking SoC and watch fresh `Range` values arrive within milliseconds.
This is exactly the channel that VM2 will eventually consume over Zenoh
in the next milestone.

### Step 7 - script a scenario from the host (optional)

For a non-interactive demo run, fire one-shot CLI commands:

```bash
docker run --rm --network host ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main \
    -- publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current 8.5
```

Range AI will republish a fresh `Vehicle.Powertrain.Range` immediately.

## Useful flags

`range_ai.py`:

| Flag | Default | Purpose |
|---|---|---|
| `--host` | `127.0.0.1` | Kuksa Databroker host |
| `--port` | `55555` | Kuksa Databroker port |

## Troubleshooting

**`ModuleNotFoundError: No module named 'kuksa_client'`**

Cloud-init didn't install it (or the VM was provisioned before that
change). Reinstall on VM1:

```bash
sudo pip3 install --break-system-packages --ignore-installed \
    kuksa-client grpcio grpcio-tools
```

**Range AI hangs at "Connecting to Kuksa Databroker..."**

The `ev-range` SDV Runtime container is not running or not listening.
Check it:

```bash
docker ps --filter name=sdv-runtime
ss -ltn | grep 55555
tail -n 50 /tmp/xmel-runtime.log
```

If the container is not there, restart it via the helper:

```bash
sudo /usr/local/bin/xmel-start-runtime
```

**`publish Vehicle.Powertrain.TractionBattery.* ...` fails with "not_found"**

The path you used is not in the runtime's VSS catalog. List what's
actually available:

```text
metadata Vehicle.Powertrain.**
```

Pick the matching paths for current / voltage / SoC and update
`SIGNAL_CURRENT`, `SIGNAL_VOLTAGE`, `SIGNAL_SOC` at the top of
`range_ai.py` to match.

**`Unknown command. See 'help' for a list of available commands.` in the CLI**

The `databroker-cli` shipped in `kuksa-databroker-cli:main` uses
`publish` (not `set`) to write a value. Run `help` inside the CLI
prompt to see the full list - typical commands are: `publish`, `get`,
`subscribe`, `metadata`, `connect`, `quit`.

**Range AI keeps printing `<waiting for StateOfCharge to be set>`**

Either you haven't run
`publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current ...`
in the CLI yet, or the CLI is talking to a different Databroker than
Range AI. Both must point at `127.0.0.1:55555` on VM1. The
`docker run --network host` flag in Step 3 is what makes
`127.0.0.1` inside the CLI container resolve to VM1's loopback.
