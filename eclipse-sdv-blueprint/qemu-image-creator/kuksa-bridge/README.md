# kuksa-bridge — bidirectional VSS sync between two Kuksa Databrokers over Eclipse Zenoh

`kuksa-bridge` is the project's own implementation of the
`kuksa-bridge / eclipse-zenoh` arrow in the Phase 1 architecture
diagram. One instance runs on each VM; the two peers connect to each
other over a single TCP-backed Eclipse Zenoh session and **carry VSS
current-value updates in both directions on the same connection**.

**Scope today: 3 cabin signals only.**

```
Vehicle.Cabin.HVAC.AmbientAirTemperature           (float)
Vehicle.Cabin.Seat.Row1.DriverSide.Heating         (int)
Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling  (int)
```

These three signals are all marked `bidirectional`, so:

* Dashboard slider movements on the host write into VM2's Kuksa via
  `hvac_ecu.py` / `seat_ecu.py`, then flow VM2 → VM1 so anything on
  VM1 (the EV Range Extender app, future range models, etc.) can
  read them from VM1's local Kuksa.
* Writes made by the EV Range Extender app on VM1 (the prototype
  from `playground.digital.auto`) flow VM1 → VM2 so the HVAC ECU
  and Seat Control Module on VM2 emit an `ACT` log line and forward
  the resulting status to the dashboard's on-screen indicators.

Battery state and the computed `Vehicle.Powertrain.Range` are
**not** bridged. Nothing on VM2 consumes them today, and the EV
Range Extender app on VM1 already owns those paths on its local
Kuksa — bridging them would just add log noise. If you ever need
to surface them on VM2, add the signal entries back to the two
JSON configs (the bridge code itself is unchanged).

> **Why a project-local implementation instead of upstream
> [`zenoh-kuksa-provider`](https://github.com/eclipse-kuksa/kuksa-incubation/tree/main/zenoh-kuksa-provider)?**
> The upstream provider is built for the device-side actuator pattern
> (Kuksa side: `subscribe_target_values`; Zenoh side: only consumes
> samples whose attachment is `"currentValue"`). Two instances on two
> Databrokers therefore *do not* mirror current values to each other,
> which is exactly what the cabin pipeline needs. `kuksa-bridge` is a
> small, config-driven Python service that does mirror current values,
> while reusing the same JSON wire envelope our existing
> `zenoh_publisher.py` / `zenoh_client.py` pair already uses.

This component is deployed alongside, not instead of, the legacy
`zenoh_publisher.service` (VM2) / `zenoh_client.service` (VM1) for
this first cut so the demo keeps working while we validate the new
bridge end-to-end. They run on different Zenoh ports (legacy `7447`
vs `kuksa-bridge` `7448`) so they cannot collide.

## What it does

```
   VM1 ev-range Kuksa Databroker  (127.0.0.1:55555)
      ^   |
      |   | EV Range Extender app writes  (VM1 -> VM2 direction)
      |   v
      |   cabin : BIDIRECTIONAL on VM1
      |
      |       VM1 kuksa-bridge
      |       listen tcp/0.0.0.0:7448
      |                |
      |                v   one Zenoh peer
      |          eclipse-zenoh   <----- bidirectional ----->     VM2 kuksa-bridge
      |                                                          connect tcp/192.168.100.10:7448
      |                |                                                         |
      |                v   key prefix: kuksa-bridge/...                          v
      |          cabin : BIDIRECTIONAL on VM1                    cabin : BIDIRECTIONAL on VM2
      v                                                                         |
   VM1 Kuksa accepts cabin writes from VM2 (dashboard path)                     v
                                                              VM2 ev-range-cabin Kuksa Databroker
                                                                  (127.0.0.1:55555)
                                                                  ^
                                                                  | hvac_ecu / seat_ecu write cabin
                                                                  | (from host PyTk dashboard)
                                                                  |
                                                              host PyTk dashboard
```

The bridged signals:

| VSS path | Type | VM1 role | VM2 role | Effective flow |
|---|---|---|---|---|
| `Vehicle.Cabin.HVAC.AmbientAirTemperature` | float | `bidirectional` | `bidirectional` | both ways |
| `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` | int | `bidirectional` | `bidirectional` | both ways |
| `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` | int | `bidirectional` | `bidirectional` | both ways |

Wire envelope (per Zenoh sample):

```json
{
  "path": "Vehicle.Cabin.HVAC.AmbientAirTemperature",
  "value": 50.0,
  "unit": "percent",
  "timestamp": "2026-...Z",
  "source": "vm2"
}
```

The `source` field is what makes the bridge safe to run with every
signal `bidirectional`: each peer drops samples it sees whose
`source` equals its own `source_label`, so it cannot echo its own
writes back into Kuksa. A second line of defence — the receiver-side
`last_sent` dedup cache inside `_inbound_consumer` — also suppresses
the single-round echo that would otherwise come back from the peer.

## Files

| Path | Purpose |
|---|---|
| `kuksa_bridge.py` | The bridge itself. One process can run any combination of inbound / outbound / bidirectional roles; the per-signal direction is set in the config. |
| `bridge-config-vm1.json` | VM1: 3 cabin signals, all bidirectional. Listens on `tcp/0.0.0.0:7448`. |
| `bridge-config-vm2.json` | VM2: 3 cabin signals, all bidirectional. Dials `tcp/192.168.100.10:7448`. |
| `README.md` | This file. |

## Per-signal direction reference

| `direction` | What it means for *this* VM |
|---|---|
| `outbound` | This VM is the source of truth: subscribe to current values on the local Kuksa, publish every change to Zenoh. |
| `inbound` | This VM is a mirror: subscribe to Zenoh, write incoming current values to the local Kuksa (with type coercion). |
| `bidirectional` | Both. Echo prevention via the `source` tag in the envelope + the `last_sent` dedup cache inside `_inbound_consumer`. |

## Running it

The cloud-init pipeline auto-deploys this folder to each VM under
`/home/ubuntu/kuksa-bridge/` and starts it via the
`ev-range-kuksa-bridge.service` systemd unit on every boot — same
pattern as every other ECU service. **You should not need to start
anything by hand.**

If you're iterating on the bridge by hand:

```bash
# Inside VM1
sudo systemctl stop ev-range-kuksa-bridge
cd /home/ubuntu/kuksa-bridge
python3 kuksa_bridge.py --config bridge-config.json
```

```bash
# Inside VM2
sudo systemctl stop ev-range-kuksa-bridge
cd /home/ubuntu/kuksa-bridge
python3 kuksa_bridge.py --config bridge-config.json
```

Validate-only mode is handy when editing configs:

```bash
python3 kuksa_bridge.py --config bridge-config-vm1.json --validate-config
```

It parses the JSON, prints the resulting role / signal table, and
exits without touching Kuksa or Zenoh.

## Side-by-side with the legacy bridge

During this Phase A cutover, both bridges are enabled by default:

| Bridge | VM1 unit | VM2 unit | Zenoh port | Direction |
|---|---|---|---|---|
| Legacy (`zenoh_publisher.py` + `zenoh_client.py`) | `ev-range-zenoh-client.service` | `ev-range-zenoh-publisher.service` | `7447` | One-way: cabin VM2 → VM1 |
| `kuksa-bridge` (this folder) | `ev-range-kuksa-bridge.service` | `ev-range-kuksa-bridge.service` | `7448` | Two-way: 3 cabin signals VM2 ↔ VM1 |

For the cabin signals both bridges write the same value into VM1's
Kuksa — that is harmless (the broker dedups identical re-writes, and
each bridge dedups locally with a `last_sent` cache). The legacy
pair has never carried the VM1 → VM2 direction; that's the
capability the new `kuksa-bridge` adds.

The legacy logs are `/tmp/ev-range-zenoh-{publisher,client}.log`; the
new bridge logs to `/tmp/ev-range-kuksa-bridge.log`. Compare while
demoing to convince yourself they agree on the cabin signals.

## Watch the new bridge live

VM1:

```bash
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-kuksa-bridge.log'
# expect lines like:
# OUT  1 key(s) -> zenoh: Vehicle.Cabin.HVAC.AmbientAirTemperature=0.0 (NNNB)
#       ^ EV Range Extender app wrote a new value on VM1's Kuksa
# IN   Vehicle.Cabin.HVAC.AmbientAirTemperature = 50.0 (from vm2)
#       ^ dashboard slider movement reached VM1 via the bridge
```

VM2:

```bash
ssh ubuntu@192.168.100.11 'tail -f /tmp/ev-range-kuksa-bridge.log'
# expect lines like:
# OUT  1 key(s) -> zenoh: Vehicle.Cabin.HVAC.AmbientAirTemperature=50.0 (NNNB)
#       ^ dashboard slider movement; VM2 publishes outward
# IN   Vehicle.Cabin.HVAC.AmbientAirTemperature = 0.0 (from vm1)
#       ^ EV Range Extender app's write landed on VM2's Kuksa
```

The `IN ... (from vm1)` line on VM2 is the closed-loop EV-app →
HVAC-ECU path. It also fires the matching `ACT` log line in
`/tmp/ev-range-hvac.log` and updates the dashboard's HVAC indicator.

You can also confirm the latest cabin values landed in VM2's Kuksa:

```bash
ssh ubuntu@192.168.100.11 \
  'docker run --rm --network host ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main \
     get Vehicle.Cabin.HVAC.AmbientAirTemperature \
         Vehicle.Cabin.Seat.Row1.DriverSide.Heating \
         Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling'
```

## Once the new bridge is trusted

Phase B (a separate change, not done in this cutover) will:

1. Disable + remove `ev-range-zenoh-publisher.service` and
   `ev-range-zenoh-client.service`.
2. Delete `ev-range-extender/vm1/zenoh_client.py` and
   `ev-range-extender/vm2/zenoh_publisher.py`.
3. Update the architecture diagram + the top-level README to point at
   `kuksa-bridge/` as the only Zenoh-based bridge in the project.

To preview that state on a single VM without touching the source:

```bash
ssh ubuntu@192.168.100.11 'sudo systemctl disable --now ev-range-zenoh-publisher'
ssh ubuntu@192.168.100.10 'sudo systemctl disable --now ev-range-zenoh-client'
```

Then move sliders on the dashboard and check that
`tail -f /tmp/ev-range-kuksa-bridge.log` on VM1 still sees `IN ...`
lines for the cabin signals — that confirms the new bridge alone is
carrying them.
