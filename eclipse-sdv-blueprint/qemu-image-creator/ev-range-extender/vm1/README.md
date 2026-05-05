# EV Range Extender - VM1 (HPC) components

VM1 hosts the digital.auto SDV Runtime (`ev-range` Kuksa Databroker)
plus the Range Compute AI app and a small Zenoh client that mirrors
VM2's cabin signals into the local Databroker.

| Component | File | Status | Role |
|---|---|---|---|
| **Range Compute AI** | `range_ai.py` | implemented | Subscribes to 3 battery signals + 3 cabin signals on Kuksa, computes range, publishes `Vehicle.Powertrain.Range`. |
| **Zenoh client** | `zenoh_client.py` | implemented | Listens on `tcp/0.0.0.0:7447` for VM2's Zenoh publisher. Decodes JSON, writes the values into the ev-range Kuksa Databroker so `range_ai.py` can consume them. |
| Power-save manager | _planned_ | next milestone | Subscribes to `Range`. When `Range` falls below a threshold, publishes reduced `Cabin.HVAC.Station.Row1.Driver.FanSpeed` and `Cabin.Seat.*.Heating` over Zenoh to VM2. |

> **No BMS app.** The diagram shows a separate "Battery Monitoring
> System" but for this demo we publish the three battery signals
> **directly from the Kuksa CLI on VM1** (acting as the simulated
> battery sensor source). Range AI doesn't care where the signals
> come from, only that they appear on the local Databroker.
>
> **All cabin signals come from the Kuksa CLI on VM2.** No Python
> publisher / sensor simulator scripts. Every input is `publish ...`
> typed (or scripted) into a Kuksa CLI somewhere.

## End-to-end VM1 data flow

```
   VM2 (192.168.100.11)                                 VM1 (192.168.100.10)
   ────────────────────                                 ────────────────────
   Kuksa CLI on VM2 (docker)                            Kuksa CLI on VM1 (docker)
        │ publish Cabin.HVAC.AmbientAirTemperature ...       │ publish TractionBattery.CurrentCurrent  25.5
        │ publish Seat.Row1.DriverSide.Heating         ...   │ publish TractionBattery.CurrentVoltage 420.0
        │ publish Seat.Row1.DriverSide.HeatingCooling  ...   │ publish TractionBattery.StateOfCharge.Current 100
        ▼                                                    ▼
   VM2 Kuksa Databroker                                ┌────────────────────────────────────────────────────────────────┐
   (127.0.0.1:55555,                                   │  Kuksa Databroker (ev-range, 127.0.0.1:55555)                  │
    EPAM VSS loaded)                                   │   Vehicle.Powertrain.TractionBattery.CurrentCurrent      (A)   │
        │                                              │   Vehicle.Powertrain.TractionBattery.CurrentVoltage      (V)   │
        │ subscribe_current_values                     │   Vehicle.Powertrain.TractionBattery.StateOfCharge.Current (%) │
        ▼                                              │   Vehicle.Cabin.HVAC.AmbientAirTemperature              (degC) │  <-- from VM2
   zenoh_publisher.py                                  │   Vehicle.Cabin.Seat.Row1.DriverSide.Heating            (%)    │  <-- from VM2
   ──────────────────                                  │   Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling     (%)    │  <-- from VM2
        │ zenoh.put(JSON) ev-range/vm2/**              │   Vehicle.Powertrain.Range                              (km)   │  <-- range_ai
        ▼  tcp/192.168.100.10:7447                     └────────────────────────────────────────────────────────────────┘
   zenoh_client.py (on VM1)                                                     ▲
   ─────────────────                                                            │ subscribe_current_values
        │ set_current_values                                                    │   (6 inputs: 3 battery + 3 cabin)
        └───────────────────────────────────────────────────────►        range_ai.py
                                                                          ───────────
```

All seven signals end up **recorded in the ev-range Kuksa Databroker
on VM1**, which is what the inside of the VM1 box in the diagram
demands. The 3 battery signals come from the Kuksa CLI on VM1; the 3
cabin signals come from the Kuksa CLI on VM2 and are bridged by
`zenoh_publisher.py` (VM2) -> `zenoh_client.py` (VM1); and
`Vehicle.Powertrain.Range` is the AI's published output.

> **Why `TractionBattery.*` and not `Battery.*`?** The diagram uses the
> short form for clarity, but the COVESA VSS 4.x catalog (which
> `ghcr.io/eclipse-autowrx/sdv-runtime` ships with) places these
> signals under `Vehicle.Powertrain.TractionBattery.*`. Confirm what's
> available in your runtime by running `metadata
> Vehicle.Powertrain.TractionBattery.**` inside the Kuksa CLI on VM1.
>
> **Why `Seat.Row1.DriverSide.HeatingCooling` and not `Seat.Ventilation`?**
> EPAM/COVESA VSS does not have a dedicated `Seat.Ventilation` leaf.
> The canonical way to express "ventilation" on a seat is
> `HeatingCooling` with a **negative** percent value. See
> `ev-range-extender/vm2/README.md` for details.

## What `range_ai.py` does

Connects to the local Kuksa Databroker on `127.0.0.1:55555` and:

1. **Subscribes** to six input signals:

| VSS path | Unit | Source for the demo |
|---|---|---|
| `Vehicle.Powertrain.TractionBattery.CurrentCurrent` | A | `publish` from the Kuksa CLI on **VM1** |
| `Vehicle.Powertrain.TractionBattery.CurrentVoltage` | V | `publish` from the Kuksa CLI on **VM1** |
| `Vehicle.Powertrain.TractionBattery.StateOfCharge.Current` | % | `publish` from the Kuksa CLI on **VM1** |
| `Vehicle.Cabin.HVAC.AmbientAirTemperature` | °C | Kuksa CLI on **VM2** -> `zenoh_publisher.py` -> `zenoh_client.py` -> Kuksa |
| `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` | % (0..100) | Kuksa CLI on **VM2** -> Zenoh -> Kuksa |
| `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` | % (-100..100) | Kuksa CLI on **VM2** -> Zenoh -> Kuksa |

2. On every update **computes the estimated remaining driving range**:

```
available_kWh    = (SoC / 100) * 75 kWh
consumption      = 0.18 kWh/km
                   * load_factor              (if instantaneous power > 18 kW)
                   * temp_factor              (if ambient temperature < 15 degC)
                   + cabin_load_kW / 60 km/h  (seat heater + ventilation)
range_km         = available_kWh / consumption
```

The cold-weather term:

```
temp_factor = 1.0                           if T >= 15 degC
            = 1 + (15 - T) * 0.025          otherwise   (capped at 2.0)
```

The cabin-actuator term (treats `Seat.Heating` / `Seat.HeatingCooling`
as the **driver-zone aggregate** load, i.e. the entire driver-side
heating/cooling power budget controlled by that switch):

```
cabin_load_kW = 2.0 * (Seat.Heating / 100)              if Seat.Heating  > 0
              + 2.0 * (Seat.HeatingCooling / 100)       if Seat.HeatingCooling > 0
              + 0.5 * (-Seat.HeatingCooling / 100)      if Seat.HeatingCooling < 0
```

| SoC | T = 22 °C, no cabin | T = 0 °C, no cabin | T = -10 °C, no cabin | T = 22 °C, Seat.Heating=100 |
|---|---|---|---|---|
| 100 % | 417 km | 303 km | 256 km | 352 km |
| 50 %  | 208 km | 152 km | 128 km | 176 km |
| 25 %  | 104 km | 76 km  | 64 km  | 88 km  |
| 12 %  | 50 km  | 36 km  | 31 km  | 42 km  |

The model parameters (battery capacity, nominal consumption, cruise
power, cold threshold, cold penalty, seat heater/vent budgets) are all
constants at the top of `range_ai.py` - tweak to model a different
vehicle.

3. **Publishes** the result back to the same Databroker as
   `Vehicle.Powertrain.Range` (km, written as `int` to match the
   Uint32 catalog declaration).

Until `StateOfCharge` is set at least once, Range AI logs
`<waiting for StateOfCharge to be set>` and publishes nothing. The 3
cabin signals are **optional** - if VM2 isn't publishing yet,
`temp_factor` is treated as `1.0` and `cabin_load_kW = 0`, so Phases
1-5 below behave exactly as before.

## Prerequisites (on VM1)

Provided by the existing cloud-init on VM1:

| Thing | Where it comes from |
|---|---|
| `kuksa-client` Python package | `input/user-data-vm1` `pip3 install ... kuksa-client ...` |
| `eclipse-zenoh` Python package (used by `zenoh_client.py`) | `input/user-data-vm1` `... eclipse-zenoh ...` |
| Kuksa Databroker on `127.0.0.1:55555` | `xmel-start-runtime` runs `ghcr.io/eclipse-autowrx/sdv-runtime:latest` with `--network host` and `RUNTIME_NAME=ev-range` |
| Docker (for the Kuksa CLI container) | Cloud-init installs `docker.io` |

Pre-flight sanity checks on VM1:

```bash
# 1. kuksa-client + eclipse-zenoh importable
python3 -c "from kuksa_client.grpc.aio import VSSClient; import zenoh; print('OK')"

# 2. Databroker reachable on :55555
ss -ltn | grep 55555

# 3. ev-range runtime container alive
docker ps --filter name=sdv-runtime

# 4. Catalog has all six VSS paths (run inside the Kuksa CLI):
#    metadata Vehicle.Powertrain.TractionBattery.CurrentCurrent
#    metadata Vehicle.Powertrain.TractionBattery.CurrentVoltage
#    metadata Vehicle.Powertrain.TractionBattery.StateOfCharge.Current
#    metadata Vehicle.Cabin.HVAC.AmbientAirTemperature
#    metadata Vehicle.Cabin.Seat.Row1.DriverSide.Heating
#    metadata Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
```

If any of those fail, see Troubleshooting at the bottom.

## Step-by-step demo

You'll need **3 SSH terminals to VM1** (Range AI / Kuksa CLI / Zenoh
client) and **2 SSH terminals to VM2** (Kuksa CLI for cabin publishes
+ Zenoh publisher). All commands assume password `ubuntu`.

| Terminal | VM | What runs there |
|---|---|---|
| A | VM1 | `range_ai.py` |
| B | VM1 | Kuksa CLI on VM1 (publishes battery signals, watches `Range`) |
| C | VM1 | `zenoh_client.py` (Phase 6+) |
| D1 | VM2 | Kuksa CLI on VM2 (publishes cabin signals; Phase 6+ and Phase 7) |
| D2 | VM2 | `zenoh_publisher.py` (forwards VM2 Kuksa updates over Zenoh; Phase 6+) |

### Step 1 - copy the app to BOTH VMs (from the host)

```bash
cd /home/goutham/Gitrepos/epam-service-connector-fork/eclipse-sdv-blueprint/qemu-image-creator
sshpass -p 'ubuntu' scp -r ev-range-extender ubuntu@192.168.100.10:/home/ubuntu/   # VM1
sshpass -p 'ubuntu' scp -r ev-range-extender ubuntu@192.168.100.11:/home/ubuntu/   # VM2
ssh ubuntu@192.168.100.10 "cd /home/ubuntu/ev-range-extender/vm1 && sed -i 's/\r$//' *.py"
ssh ubuntu@192.168.100.11 "cd /home/ubuntu/ev-range-extender/vm2 && sed -i 's/\r$//' *.py"
```

### Step 2 - Terminal A (VM1): start Range Compute AI

```bash
ssh ubuntu@192.168.100.10
cd /home/ubuntu/ev-range-extender/vm1
python3 range_ai.py
```

Expected:
```
[range-ai] Connecting to Kuksa Databroker at 127.0.0.1:55555...
[range-ai] Connected.
[range-ai]   Subscribing to 6 signal(s):
[range-ai]     - Vehicle.Powertrain.TractionBattery.CurrentCurrent          (battery, from Kuksa CLI on VM1)
[range-ai]     - Vehicle.Powertrain.TractionBattery.CurrentVoltage          (battery, from Kuksa CLI on VM1)
[range-ai]     - Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   (battery, from Kuksa CLI on VM1)
[range-ai]     - Vehicle.Cabin.HVAC.AmbientAirTemperature                   (cabin, from Kuksa CLI on VM2 via zenoh)
[range-ai]     - Vehicle.Cabin.Seat.Row1.DriverSide.Heating                 (cabin, from Kuksa CLI on VM2 via zenoh)
[range-ai]     - Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling          (cabin, from Kuksa CLI on VM2 via zenoh)
[range-ai]   Will publish to:
[range-ai]     - Vehicle.Powertrain.Range
[range-ai]   Model: capacity=75.0 kWh, consumption=0.18 kWh/km, cruise=18.0 kW, cold-threshold=15.0 degC, cold-penalty=2.5%/deg, seat-heater-max=2000 W, seat-vent-max=500 W
[range-ai] output : <waiting for StateOfCharge to be set>
```

### Step 3 - Terminal B (VM1): open the Kuksa CLI on VM1

```bash
ssh ubuntu@192.168.100.10
docker run -it --rm --network host \
    ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main
```

Sanity check (paste all six):
```text
metadata Vehicle.Powertrain.TractionBattery.CurrentCurrent
metadata Vehicle.Powertrain.TractionBattery.CurrentVoltage
metadata Vehicle.Powertrain.TractionBattery.StateOfCharge.Current
metadata Vehicle.Cabin.HVAC.AmbientAirTemperature
metadata Vehicle.Cabin.Seat.Row1.DriverSide.Heating
metadata Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
metadata Vehicle.Powertrain.Range
```
All seven must return `[metadata] OK`.

### Step 4 - Phases 1-5: battery scenarios from Terminal B

After each block, Terminal A logs a fresh `Vehicle.Powertrain.Range`.

#### Phase 1 — cold start, fully charged

```text
publish Vehicle.Powertrain.TractionBattery.CurrentVoltage         420.0
publish Vehicle.Powertrain.TractionBattery.CurrentCurrent          25.5
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current  100.0
```
Terminal A → `Range = 417 km` (P = 10.7 kW < 18 kW cruise → no penalty).

#### Phase 2 — normal cruising drains the battery

```text
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   75
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   50
```
Terminal A → 312 km → 208 km.

#### Phase 3 — hard acceleration (current spikes)

```text
publish Vehicle.Powertrain.TractionBattery.CurrentCurrent          90.0
```
Terminal A → 99 km (P = 37.8 kW ≈ 2.1× cruise, consumption scales by 2.1).

#### Phase 4 — driver eases off, voltage sags

```text
publish Vehicle.Powertrain.TractionBattery.CurrentCurrent          25.5
publish Vehicle.Powertrain.TractionBattery.CurrentVoltage         380.0
```
Terminal A → 208 km → 208 km (P = 9.7 kW back below cruise; voltage sag alone doesn't move the number).

#### Phase 5 — critical SoC, low-battery trigger

```text
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   12
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   25
```
Terminal A → **50 km (trigger)** → 104 km (recovery, simulating a fast-DC stop).

> `Range = 50 km` is the trigger condition for the EV Range Extender
> feature. The Power-save manager (next milestone) will subscribe to
> `Vehicle.Powertrain.Range` and react to this threshold by publishing
> reduced cabin loads back to VM2.

### Step 5 - bring up the VM1 ↔ VM2 Zenoh bridge (one-time per session)

The next two phases need cabin signals from VM2. Start the bridge once
and leave it running.

#### Terminal C (VM1) - start the Zenoh client

```bash
ssh ubuntu@192.168.100.10
cd /home/ubuntu/ev-range-extender/vm1
python3 zenoh_client.py
```

Expected:
```
[zenoh-cli] Connecting to Kuksa Databroker at 127.0.0.1:55555...
[zenoh-cli] Connected to Kuksa.
[zenoh-cli] Whitelisted VSS paths (VM2 publishes -> client writes here):
[zenoh-cli]     - Vehicle.Cabin.HVAC.AmbientAirTemperature
[zenoh-cli]     - Vehicle.Cabin.Seat.Row1.DriverSide.Heating
[zenoh-cli]     - Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
[zenoh-cli] Opening Zenoh session, listen=tcp/0.0.0.0:7447, subscribed to 'ev-range/vm2/**'
[zenoh-cli] Zenoh client running. Ctrl+C to stop.
```

#### Terminal D2 (VM2) - start the Zenoh publisher

```bash
ssh ubuntu@192.168.100.11
cd /home/ubuntu/ev-range-extender/vm2
python3 zenoh_publisher.py
```

Expected: see `ev-range-extender/vm2/README.md`.

#### Terminal D1 (VM2) - open the Kuksa CLI on VM2

```bash
ssh ubuntu@192.168.100.11
docker run -it --rm --network host \
    ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main
```

> **VSS catalog on VM2** is preloaded - cloud-init runs the same
> `sdv-runtime` image on VM2 as on VM1, so the standard COVESA VSS
> catalog is already there. Verify once with:
> ```text
> metadata Vehicle.Cabin.HVAC.AmbientAirTemperature
> metadata Vehicle.Cabin.Seat.Row1.DriverSide.Heating
> metadata Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
> ```
> If any returns `not_found`, the wrong container image is running -
> see `ev-range-extender/vm2/README.md` "VSS catalog sanity check".

### Step 6 - Phase 6: cold snap from VM2 (Kuksa CLI on VM2)

Continuing from end of Phase 5 (SoC = 25 %, V = 380, I = 25.5).

#### Warm baseline (T = 22 °C)

In Terminal D1:
```text
publish Vehicle.Cabin.HVAC.AmbientAirTemperature 22.0
```

Watch the chain:

| Terminal | Output |
|---|---|
| **D1** (CLI on VM2) | `[publish] OK` |
| **D2** (zenoh-pub on VM2) | `[zenoh-pub] FWD  Vehicle.Cabin.HVAC.AmbientAirTemperature = 22.0  ->  zenoh (... B)` |
| **C** (zenoh-cli on VM1) | `[zenoh-cli] OK   Vehicle.Cabin.HVAC.AmbientAirTemperature = 22.0 (from vm2)` |
| **A** (range_ai on VM1) | `Range = 104 km (..., T=22.000 degC, tempFactor=1.00, cabin=0 W)` |

#### Phase 6a — cool autumn (T = 10 °C)

```text
publish Vehicle.Cabin.HVAC.AmbientAirTemperature 10.0
```
Terminal A → **93 km** (tempFactor = 1.12).

#### Phase 6b — winter morning (T = 0 °C)

```text
publish Vehicle.Cabin.HVAC.AmbientAirTemperature 0.0
```
Terminal A → **76 km** (tempFactor = 1.38).

#### Phase 6c — arctic (T = -10 °C)

```text
publish Vehicle.Cabin.HVAC.AmbientAirTemperature -10.0
```
Terminal A → **64 km** (tempFactor = 1.62).

#### Phase 6d — cold + critical SoC

Leave T = -10 °C in D1. In **Terminal B** (Kuksa CLI on VM1):
```text
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current 12
```
Terminal A → **31 km** (vs. 50 km warm at the same SoC = 38 % less range purely from cold).

#### Recovery (T = 22 °C)

In Terminal D1:
```text
publish Vehicle.Cabin.HVAC.AmbientAirTemperature 22.0
```
Terminal A → **50 km** (back to Phase 5 trigger baseline).

### Step 7 - Phase 7: seat heater + ventilation from VM2

Driver flips the seat heating switch and the cabin ventilation - both
draw additional power, both shorten range. Continuing from end of
Phase 6 recovery (SoC = 12, T = 22 °C).

#### Phase 7a — driver seat heater full on (Heating = 100 %)

In Terminal D1:
```text
publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating 100
```
Terminal A → **42 km** (down from 50 km baseline; cabin = 2000 W).

#### Phase 7b — heater half (Heating = 50 %)

```text
publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating 50
```
Terminal A → **46 km** (cabin = 1000 W).

#### Phase 7c — heater off, ventilation only (HeatingCooling = -100 %)

```text
publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating 0
publish Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling -100
```
Terminal A → **48 km** (cabin = 500 W; vent draws less than heating).

#### Phase 7d — both heating signals on (worst case)

```text
publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating 100
publish Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling 50
```
Terminal A → **39 km** (cabin = 2000 + 1000 = 3000 W).

#### Phase 7e — cold + heater (compounding penalties)

```text
publish Vehicle.Cabin.HVAC.AmbientAirTemperature -10.0
publish Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling 0
publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating 100
```

Then in **Terminal B** drop SoC back to a higher value to see the
combined effect more clearly:
```text
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current 25
```
Terminal A → **~58 km** (vs. 64 km cold-only at SoC 25 %; vs. 91 km warm + heater alone). Cold + heater stack.

#### Recovery (everything off)

In Terminal D1:
```text
publish Vehicle.Cabin.HVAC.AmbientAirTemperature 22.0
publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating 0
publish Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling 0
```
Terminal A → **104 km** (back to Phase 5 recovery baseline).

> **Why is Phase 7 the trigger for the next milestone?** With cabin
> loads in the picture, low-SoC + cold + cabin-on can drop `Range`
> below the trigger even though the battery looks OK. The Power-save
> manager will react by publishing `Heating = 0` and `HeatingCooling
> = 0` back to VM2 over Zenoh - the reverse of the bridge we just
> built.

### Step 8 - verify all 7 signals are recorded on the ev-range runtime

Still in Terminal B (the Kuksa CLI on VM1):
```text
get Vehicle.Powertrain.TractionBattery.CurrentCurrent
get Vehicle.Powertrain.TractionBattery.CurrentVoltage
get Vehicle.Powertrain.TractionBattery.StateOfCharge.Current
get Vehicle.Cabin.HVAC.AmbientAirTemperature
get Vehicle.Cabin.Seat.Row1.DriverSide.Heating
get Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
get Vehicle.Powertrain.Range
```
All seven return current values. The first three came from Terminal B
itself; the next three originated on VM2 and traveled `D1 → D2 → C →
VM1 Kuksa`; the last was published by `range_ai.py`.

To stream the computed Range live so you can keep poking values:
```text
subscribe Vehicle.Powertrain.Range
```

### Step 9 - script a scenario from the host (optional)

For a non-interactive demo run:

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

`zenoh_client.py`:

| Flag | Default | Purpose |
|---|---|---|
| `--listen` | `tcp/0.0.0.0:7447` | Zenoh listen endpoint |
| `--key` | `ev-range/vm2/**` | Zenoh key expression to subscribe to |
| `--kuksa-host` | `127.0.0.1` | Local Kuksa Databroker host |
| `--kuksa-port` | `55555` | Local Kuksa Databroker port |

## Troubleshooting

**`ModuleNotFoundError: No module named 'kuksa_client'`**

Cloud-init didn't install it. On VM1:
```bash
sudo pip3 install --break-system-packages --ignore-installed \
    kuksa-client grpcio grpcio-tools eclipse-zenoh
```

**Range AI hangs at "Connecting to Kuksa Databroker..."**

The `ev-range` SDV Runtime container is not running or not listening:
```bash
docker ps --filter name=sdv-runtime
ss -ltn | grep 55555
tail -n 50 /tmp/xmel-runtime.log
sudo /usr/local/bin/xmel-start-runtime    # restart if needed
```

**`publish Vehicle.Powertrain.TractionBattery.* ...` fails with `not_found`**

The path is not in the VM1 runtime's VSS catalog. List what's available:
```text
metadata Vehicle.Powertrain.**
```
Update `SIGNAL_CURRENT`, `SIGNAL_VOLTAGE`, `SIGNAL_SOC` at the top of
`range_ai.py` to match if your runtime is different.

**`publish Vehicle.Cabin.Seat.Row1.DriverSide.* ...` fails with `not_found` on VM1's CLI**

VM1's `ev-range` runtime has the seat paths (the `sdv-runtime` image
uses the COVESA VSS catalog), but if you ever rebuild it without those
paths the `zenoh_client.py` writes will be rejected. Verify on VM1:
```text
metadata Vehicle.Cabin.Seat.Row1.DriverSide.Heating
metadata Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
```

**`publish ...` fails on VM2's CLI**

The wrong Databroker image is running on VM2. Cloud-init now uses
`ghcr.io/eclipse-autowrx/sdv-runtime:latest` (same image as VM1, with
the standard COVESA VSS catalog preloaded). Re-run the helper:
```bash
sudo /usr/local/bin/xmel-start-databroker
```
See `ev-range-extender/vm2/README.md` "VSS catalog sanity check" for
detailed diagnostics.

**`Unknown command. See 'help' for a list of available commands.`**

The CLI shipped in `kuksa-databroker-cli:main` uses `publish` (not
`set`) to write a value. `help` inside the prompt lists the rest.

**Range AI keeps printing `<waiting for StateOfCharge to be set>`**

You haven't published any `StateOfCharge.Current` from the Kuksa CLI on
**VM1** yet. Run Phase 1.

**VM2 publishes but VM1 sees nothing**

- Make sure `zenoh_client.py` is running on VM1 (Step 5 / Terminal C).
- Make sure the host has the inter-VM forward rule:
  ```bash
  sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT
  ```
- Verify TCP connectivity: from VM2, `nc -zv 192.168.100.10 7447` must succeed.
