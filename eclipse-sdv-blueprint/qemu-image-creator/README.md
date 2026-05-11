# QEMU Multi-VM SDV Lab — EV Range Extender

A zero-touch multi-VM lab built on QEMU + KVM + cloud-init that hosts the
**EV Range Extender** end-to-end demo from the
[eclipse-sdv-blueprint](../README.md). Two Ubuntu 24.04 ("noble") VMs are
provisioned automatically, joined to a private Layer-2 bridge, and each
runs its own `digital.auto sdv-runtime` Kuksa Databroker. A **PyTk
hardware-simulator dashboard** running on the host pushes signal values
into per-VM ECUs (BMS, HVAC, Seat) that own the matching VSS branches in
each Databroker. **All Python apps are auto-deployed by cloud-init on
first boot — no manual `scp` anywhere.**

Reference prototype on the digital.auto playground:
[ev-range prototype](https://playground.digital.auto/model/67f76c0d8c609a0027662a69/library/prototype/69ce30f438bb8e98f0af5ac8/dashboard)

---

## Two cross-VM transport options — pick one

Cabin signals travel from VM2's local Kuksa Databroker to VM1's local
Kuksa Databroker over a small Python bridge (battery signals stay on
VM1, so they don't need a bridge). The repo ships **two fully working
bridge implementations**, side by side, sharing the same Kuksa
Databrokers, the same VSS paths, and the same `range_ai.py` consumer.
Pick the one that matches what you want to demonstrate:

| | **Method 1: Eclipse Zenoh** (default) | **Method 2: SOME/IP (Eclipse SCore)** |
|---|---|---|
| Why pick it | Lightweight pub/sub, simpler wire, easier to reason about for first-time SDV demos. Same transport the host PyTk dashboard uses to talk to the ECUs, so end-to-end stays consistent. | Matches the upstream blueprint's "Eclipse SCore / SOME-IP" HPC↔Zonal link. The protocol used in real automotive SOME/IP buses. |
| VM1 bridge file | `ev-range-extender/vm1/zenoh_client.py` | `ev-range-extender/vm1/someip_client.py` |
| VM2 bridge file | `ev-range-extender/vm2/zenoh_publisher.py` | `ev-range-extender/vm2/someip_publisher.py` |
| Wire protocol | Zenoh peer-to-peer over TCP, JSON payloads | SOME/IP over UDP, with SOME/IP-SD discovery |
| Ports | `tcp/7447` (VM1 listener; VM2 connects out) | `udp/30490` (SD multicast `224.224.224.245`), `udp/30509` (VM2 events), `udp/30510` (VM1 events) |
| Smoke-test demo folder | `zenoh-demo/` (`pub.py` + `sub.py`) | `someip-demo/` (`server.py` + `client.py`) |
| Python dep on each VM | `eclipse-zenoh` | `someipy>=1.0,<2.0` |

Both packages are pre-installed by cloud-init on every fresh boot, so
**no extra setup is needed to switch between them** — just run the
matching bridge pair and leave the other pair stopped. Don't run both
pairs at the same time or both will write the same VSS values into
VM1's Kuksa Databroker.

The "Quick start" below has separate **Method 1** and **Method 2**
blocks at Step 5. Everything before that step (host prep, boot, code
auto-deploy, dashboard launch) and everything after (the 7-phase demo
scenario, shutdown) is identical for both transports.

---

## Architecture at a glance

```
WSL host (192.168.100.1)
  +----------------------------+
  |  hardware-sim/             |
  |   pytk_dashboard.py        |
  |   Tk window: 6 sliders     |
  |   Zenoh peer publisher     |
  +----------------------------+
        |             |             |
        | tcp/7460    | tcp/7461    | tcp/7462
        v             v             v
  br0  +-- tap1 -----------------------+     +--- tap2 ----------------------+
       |  VM1 (192.168.100.10) [HPC]   |     |  VM2 (192.168.100.11) [Zonal] |
       |  ev-range SDV Runtime         |     |  ev-range-cabin SDV Runtime   |
       |  Kuksa Databroker :55555      |     |  Kuksa Databroker :55555      |
       |                               |     |                               |
       |  bms.py            (systemd)  |     |  hvac_ecu.py       (systemd)  |
       |   sub sim/battery/**          |     |   sub sim/cabin/temp          |
       |   -> Vehicle.Powertrain.      |     |   -> Vehicle.Cabin.HVAC.      |
       |      TractionBattery.{V,A,SoC}|     |      AmbientAirTemperature    |
       |                               |     |                               |
       |  range_ai.py     publishes -> |     |  seat_ecu.py       (systemd)  |
       |    Vehicle.Powertrain.Range   |     |   sub sim/cabin/seat/**       |
       |                               |     |   -> Vehicle.Cabin.Seat.      |
       |  zenoh_client.py <-zenoh------|<----| zenoh_publisher.py            |
       |    tcp/7447                   |     |   bridges local Kuksa cabin   |
       |    writes cabin signals into  |     |   signals over to VM1         |
       |    the local Databroker       |     |   tcp -> VM1:7447             |
       |                               |     |                               |
       |  (alt) someip_client.py       |     |  (alt) someip_publisher.py    |
       +-------------------------------+     +-------------------------------+
```

- **Host** runs the **PyTk hardware-simulator** (`hardware-sim/pytk_dashboard.py`).
  It's the canonical input layer: 6 sliders publish over Zenoh to the
  three ECUs.
- **VM1** (the HPC) runs `bms.py` (Battery Monitoring System,
  auto-started by systemd), the Range Compute AI (`range_ai.py`),
  and a cross-VM subscriber bridge — `zenoh_client.py` (default for
  this demo) or the alternative `someip_client.py`.
- **VM2** (the Zonal node) runs `hvac_ecu.py` and `seat_ecu.py`
  (both auto-started by systemd) and a cross-VM publisher bridge —
  `zenoh_publisher.py` or the alternative `someip_publisher.py`.
- **Zero manual deploy**: every Python app under `ev-range-extender/`
  is embedded into the cloud-init seed by `tools/compose_userdata.py`
  and lands on each VM at first boot. The three ECUs install +
  auto-start as `ev-range-bms.service`, `ev-range-hvac.service`, and
  `ev-range-seat.service`.
- **Cross-VM transport** for cabin signals is **Eclipse Zenoh** by
  default; an Eclipse-SCore SOME/IP alternative is also installed
  and importable on every boot. See "Two cross-VM transport options"
  below for switching between them.
- **All intra-VM transport** is the local Kuksa Databroker on
  `127.0.0.1:55555`.

---

## What's in this folder

| Path | Purpose |
|---|---|
| `setup.sh` | Downloads the Ubuntu cloud image, **runs `tools/compose_userdata.py` to embed all `ev-range-extender/` Python apps into the cloud-init seed**, builds two qcow2 disks + cloud-init seeds, brings up `br0`/`tap1`/`tap2`, launches both VMs, polls for the broker on VM1 to be healthy. |
| `vm1_launch.sh` / `vm2_launch.sh` | QEMU invocations for each VM (called by `setup.sh`). |
| `input/user-data-vm1` | Cloud-init **template** for VM1: installs `docker.io`, Python deps (`kuksa-client`, `eclipse-zenoh`, `someipy`, `grpcio`, ...), runs `evrange-start-runtime` to bring up `ghcr.io/eclipse-autowrx/sdv-runtime:latest` with `RUNTIME_NAME=ev-range`. The composer step appends auto-deploy entries; the merged file lands at `output/user-data-vm1.composed`. |
| `input/user-data-vm2` | Same idea for VM2: same packages, `RUNTIME_NAME=ev-range-cabin` so VM2's Databroker boots with the standard COVESA VSS catalog already loaded. |
| `input/network-vm1.yaml` / `input/network-vm2.yaml` | Static IP for the bridge NIC; DHCP for the SLIRP NIC (outbound internet). |
| **`ev-range-extender/`** | **The main demo apps** (auto-deployed onto the VMs). See the layout below. |
| **`hardware-sim/`** | **Host-side PyTk dashboard** that publishes slider values to the three ECUs over Zenoh. Replaces the old "type `publish ...` in a Kuksa CLI" workflow. |
| **`tools/compose_userdata.py`** | Build-time helper that injects every Python file under `ev-range-extender/` plus three systemd unit files into a copy of `input/user-data-vm{1,2}`, producing the `output/user-data-vm{1,2}.composed` files used by `cloud-localds`. |
| `someip-demo/` | A minimal "VM2 -> VM1" SOME/IP pub/sub example (independent of the EV demo). |
| `zenoh-demo/` | A minimal "VM1 -> VM2" Zenoh pub/sub example (independent of the EV demo). |
| `grpc-demo/` | A minimal "VM1 -> VM2" Python gRPC client/server example (independent of the EV demo). |
| `output/` | Generated qcow2 disks, seed images, composed cloud-config, base Ubuntu image (gitignored). |

### `ev-range-extender/` layout

```
ev-range-extender/                  (auto-deployed to /home/ubuntu/ev-range-extender on each VM)
├── common/
│   ├── __init__.py
│   └── someip_service.py    # shared SOME/IP service IDs + payload codecs
├── vm1/
│   ├── README.md            # full VM1 docs
│   ├── range_ai.py          # consumes Kuksa, publishes Vehicle.Powertrain.Range
│   ├── bms.py               # Battery Monitoring System (auto-start: ev-range-bms.service)
│   ├── zenoh_client.py      # Cross-VM bridge subscriber (Zenoh, tcp/7447) - default
│   └── someip_client.py     # Cross-VM bridge subscriber (SOME/IP) - alternative
└── vm2/
    ├── README.md            # full VM2 docs
    ├── hvac_ecu.py          # HVAC ECU                 (auto-start: ev-range-hvac.service)
    ├── seat_ecu.py          # Seat Control Module       (auto-start: ev-range-seat.service)
    ├── zenoh_publisher.py   # Cross-VM bridge publisher  (Zenoh) - default
    └── someip_publisher.py  # Cross-VM bridge publisher  (SOME/IP) - alternative
```

### `hardware-sim/` layout

```
hardware-sim/                  (runs on the host, NOT inside the VMs)
├── README.md
├── requirements.txt          # eclipse-zenoh
└── pytk_dashboard.py         # Tk GUI: 6 sliders, Zenoh peer publisher
```

The two demo READMEs (`ev-range-extender/vm1/README.md`,
`ev-range-extender/vm2/README.md`) contain the deep dive: VSS path
choices, the range model, the cold-weather and cabin-load formulas,
the SOME/IP service contract, and the full 7-phase scenario.

---

## IP map and ports

| Host / VM | IP | Listening ports |
|---|---|---|
| WSL host | 192.168.100.1 (`br0`) | — (the PyTk dashboard is an outbound Zenoh peer; no listening port) |
| VM1 | 192.168.100.10 (`ens3`) | 22 (ssh), 55555 (ev-range Kuksa), **tcp/7460** (`bms.py`, host -> VM1), **tcp/7447** (`zenoh_client.py`, VM2 -> VM1), **udp/30490** (SOME/IP-SD), **udp/30510** (SOME/IP events) |
| VM2 | 192.168.100.11 (`ens3`) | 22 (ssh), 55555 (ev-range-cabin Kuksa), **tcp/7461** (`hvac_ecu.py`, host -> VM2), **tcp/7462** (`seat_ecu.py`, host -> VM2), **udp/30490** (SOME/IP-SD), **udp/30509** (SOME/IP events) |

There are two independent Zenoh layers in this lab:

- **Host -> ECU** (`tcp/7460`, `tcp/7461`, `tcp/7462`): the PyTk
  dashboard publishes slider values; the BMS / HVAC / Seat ECUs
  subscribe and write into their *local* Kuksa Databroker. Always on,
  always Zenoh.
- **VM2 -> VM1 cross-VM bridge** (`tcp/7447` for Zenoh, or
  `udp/30490`+`udp/30509`+`udp/30510` for SOME/IP): bridges cabin
  signals from VM2's Kuksa to VM1's Kuksa so `range_ai.py` can use
  them. Run **either** the Zenoh pair (`zenoh_publisher.py` +
  `zenoh_client.py`) **or** the SOME/IP pair (`someip_publisher.py`
  + `someip_client.py`), not both. SOME/IP-SD discovery uses the
  standard multicast group `224.224.224.245:30490`.

Both VMs also have a SLIRP NIC (`ens4`, 10.0.2.15/24) for **outbound**
internet (apt, pip, docker pull). SLIRP does not carry IPv6 — see
"Known issues" below for why this matters and how it's worked around.

---

## Quick start (~10 min on a fresh host) — fully self-contained

Seven numbered steps. Steps 1–4 and 6–7 are **identical for both
cross-VM transports**; **Step 5** is where you pick **Method 1
(Zenoh, default)** or **Method 2 (SOME/IP)**. Every command needed
to go from a clean host to a working end-to-end demo is inline in
this section — you never need to `scp` anything.

| Step | What it does | Shared / per-method |
|---|---|---|
| 1 | Host prep (cleanup, install qemu, iptables, ip_forward) | shared |
| 2 | Provision and boot both VMs (`./setup.sh`) — **also auto-deploys the ECUs** | shared |
| 3 | Verify VMs are reachable + Kuksa is up + the 3 ECU systemd services are running | shared |
| 4 | Launch the host **PyTk hardware-simulator dashboard** | shared |
| 5 | **Pick** Method 1 (Zenoh) **or** Method 2 (SOME/IP), start the cross-VM bridge + `range_ai.py` | per-method |
| 6 | Run the 7-phase scenario by moving sliders in the GUI | shared |
| 7 | Shutdown | shared |

### Step 1 — Host-side prep (once per host / per reboot)

```bash
# Kill any orphan VMs from a previous run (fixes "tap1: Device or resource busy")
sudo pkill -9 -f 'qemu-system-x86_64' 2>/dev/null
sudo ip link delete tap1 2>/dev/null
sudo ip link delete tap2 2>/dev/null
sudo ip link delete br0  2>/dev/null

# Install host tools (qemu + cloud-init utils + Tk for the dashboard +
# PyYAML for tools/compose_userdata.py + Python Zenoh binding)
sudo apt update
sudo apt install -y qemu-system qemu-utils cloud-image-utils wget bridge-utils \
                    python3-tk python3-yaml python3-pip
pip install --user --break-system-packages 'eclipse-zenoh>=1.0.0'

# Allow VM <-> VM and host <-> VM traffic on the bridge (WSL Netfilter
# blocks it by default). This single rule unblocks the host PyTk -> ECU
# Zenoh links AND the SOME/IP-SD multicast / Zenoh TCP cross-VM bridge.
sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT
sudo sysctl -w net.ipv4.ip_forward=1
```

### Step 2 — Provision and boot both VMs (with auto-deployed ECUs)

Run from inside the `qemu-image-creator/` folder of your local clone:

```bash
cd path/to/eclipse-sdv-blueprint/qemu-image-creator
chmod +x *.sh
./setup.sh
```

What happens, in order:

1. `tools/compose_userdata.py` reads the templates `input/user-data-vm{1,2}`
   and embeds every Python file under `ev-range-extender/` plus the three
   systemd unit files (`ev-range-bms.service`, `ev-range-hvac.service`,
   `ev-range-seat.service`) into `output/user-data-vm{1,2}.composed`.
2. `cloud-localds` packs that composed file into the seed image.
3. `setup.sh` downloads `noble-server-cloudimg-amd64.img` on first run
   (~600 MB, ~3 min), builds the qcow2 disks, brings up the bridge,
   boots both VMs, then polls VM1's Databroker port until it answers.
4. On first boot, cloud-init writes the `ev-range-extender/` tree under
   `/home/ubuntu/`, drops the systemd unit files under
   `/etc/systemd/system/`, runs `systemctl enable --now` on all three
   ECUs, and the dashboard's downstream listeners are live the moment
   each Kuksa Databroker comes up.

> **If `./setup.sh` hangs at "Waiting for SDV Runtime ..." for more
> than 5 min**, cloud-init's apt step probably failed (see "Known
> issues"). Ctrl+C and follow the manual recovery in section A.

### Step 3 — Verify VMs + Kuksa + the auto-started ECUs (password: `ubuntu`)

```bash
ssh ubuntu@192.168.100.10 'hostname && docker ps && ss -ltn | grep 55555'
ssh ubuntu@192.168.100.11 'hostname && docker ps && ss -ltn | grep 55555'
```

You should see `vm1` / `vm2`, the `sdv-runtime` / `kuksa-databroker`
container, and a listener on `:55555`. Now confirm the auto-deployed
ECUs are running:

```bash
# VM1 — should report 'active (running)' for ev-range-bms and a tcp/7460 listener
ssh ubuntu@192.168.100.10 'systemctl is-active ev-range-bms && ss -ltn | grep 7460'

# VM2 — should report 'active (running)' for both, plus tcp/7461 + tcp/7462 listeners
ssh ubuntu@192.168.100.11 'systemctl is-active ev-range-hvac ev-range-seat && ss -ltn | grep -E ":746[12]"'
```

If a service is `inactive` or `failed`, dump the journal:

```bash
ssh ubuntu@192.168.100.10 'sudo journalctl -u ev-range-bms --no-pager -n 60'
ssh ubuntu@192.168.100.11 'sudo journalctl -u ev-range-hvac --no-pager -n 60'
ssh ubuntu@192.168.100.11 'sudo journalctl -u ev-range-seat --no-pager -n 60'
```

The most common failure mode is "Kuksa not yet listening at boot
time" — each unit has a 120 s wait-for-databroker `ExecStartPre`, but
on a slow host the broker pull can take longer; just `systemctl
restart ev-range-bms` (etc.) once the broker is healthy.

### Step 4 — Launch the host PyTk dashboard

The dashboard is the canonical input layer for the demo. Open one
host terminal:

```bash
cd path/to/eclipse-sdv-blueprint/qemu-image-creator/hardware-sim
python3 pytk_dashboard.py
# expected on stdout:
#   [pytk] dialing Zenoh endpoints: ['tcp/192.168.100.10:7460',
#                                    'tcp/192.168.100.11:7461',
#                                    'tcp/192.168.100.11:7462']
# A Tk window appears with three labelled sections:
#   - Battery (VM1 - bms.py):     Voltage / Current / SoC
#   - Cabin HVAC (VM2 - hvac_ecu.py): Ambient Temp
#   - Cabin Seat (VM2 - seat_ecu.py): Heating, Heating-Cooling
```

Move any slider once. In a separate terminal you can confirm the
matching ECU received it:

```bash
ssh ubuntu@192.168.100.10 'sudo journalctl -u ev-range-bms --no-pager -n 5'
# expected: lines like
#   [bms] OK Vehicle.Powertrain.TractionBattery.StateOfCharge.Current = 75.0 (from <host>)
```

You can leave the dashboard open. Don't close it before Step 7.

> **The dashboard fully replaces the old "open a Kuksa CLI and type
> `publish ...`" workflow.** Step 6 below tells you which sliders to
> move for each phase. You CAN still open a Kuksa CLI (`docker run -it
> --rm --network host ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main`)
> for inspection / overrides, but it's no longer needed.

### Step 5 — Pick a cross-VM bridge transport and start it

You'll need **3 SSH terminals total**: 2 to VM1, 1 to VM2. The 3 ECUs
(BMS, HVAC, Seat) and both Kuksa Databrokers are already running
because they auto-started in Step 2. Only `range_ai.py` and the
cross-VM bridge pair are launched manually so you can watch their
output.

| Terminal | VM | What runs there | Method 1 (Zenoh, default) | Method 2 (SOME/IP, alt.) |
|---|---|---|---|---|
| **A** | VM1 | Range Compute AI | `python3 range_ai.py` | `python3 range_ai.py` |
| **C** | VM1 | Cross-VM bridge subscriber | `python3 zenoh_client.py` | `python3 someip_client.py` |
| **D2** | VM2 | Cross-VM bridge publisher | `python3 zenoh_publisher.py` | `python3 someip_publisher.py` |

Open them in this order: **C → D2 → A** (Zenoh) or **D2 → C → A** (SOME/IP).

> **Don't run both methods at once.** Both bridges write the same VSS
> paths into VM1's Kuksa Databroker, so running them simultaneously
> causes duplicate writes for every cabin signal.

#### Method 1 — Eclipse Zenoh (default)

```bash
# Terminal C — VM1: Zenoh client (start FIRST — it owns the listening socket)
ssh ubuntu@192.168.100.10
cd /home/ubuntu/ev-range-extender/vm1
python3 zenoh_client.py
# expected: "[zenoh-cli] Zenoh client running. Ctrl+C to stop."
```

```bash
# Terminal D2 — VM2: Zenoh publisher (connects out to VM1)
ssh ubuntu@192.168.100.11
cd /home/ubuntu/ev-range-extender/vm2
python3 zenoh_publisher.py
# expected: "[zenoh-pub] Publisher running ... Ctrl+C to stop."
```

```bash
# Terminal A — VM1: Range Compute AI
ssh ubuntu@192.168.100.10
cd /home/ubuntu/ev-range-extender/vm1
python3 range_ai.py
# expected: "[range-ai] output : <waiting for StateOfCharge to be set>"
```

The chain you should see on every cabin slider move in the dashboard:

| Where | What you'll see |
|---|---|
| Host (`pytk`) | status bar: `PUT sim/cabin/temp = 22.0 °C` |
| VM2 `journalctl -u ev-range-hvac` | `[hvac] OK Vehicle.Cabin.HVAC.AmbientAirTemperature = 22.0 (from <host>)` |
| D2 (`zenoh-pub`) | `FWD <path> = <value>  ->  zenoh (... B)` |
| C (`zenoh-cli`) | `OK <path> = <value> (from vm2)` |
| A (`range-ai`) | `Range = ... km` |

For battery sliders the chain is shorter (BMS writes directly into
VM1's Kuksa, no cross-VM hop): host -> `journalctl -u ev-range-bms`
-> `range-ai`.

#### Method 2 — SOME/IP (Eclipse SCore alternative)

```bash
# Terminal D2 — VM2: SOME/IP publisher (start FIRST so SD offers begin)
ssh ubuntu@192.168.100.11
cd /home/ubuntu/ev-range-extender/vm2
python3 someip_publisher.py
# expected: "[someip-pub] Publisher running ... Ctrl+C to stop."
```

```bash
# Terminal C — VM1: SOME/IP client
ssh ubuntu@192.168.100.10
cd /home/ubuntu/ev-range-extender/vm1
python3 someip_client.py
# expected: "[someip-cli] SOME/IP client running. Waiting for offers from VM2."
```

```bash
# Terminal A — VM1: Range Compute AI
ssh ubuntu@192.168.100.10
cd /home/ubuntu/ev-range-extender/vm1
python3 range_ai.py
```

The cabin chain becomes:

| Where | What you'll see |
|---|---|
| Host (`pytk`) | `PUT sim/cabin/temp = 22.0 °C` |
| VM2 `journalctl -u ev-range-hvac` | `[hvac] OK <path> = 22.0 ...` |
| D2 (`someip-pub`) | `FWD <path> = <value>  ->  someip event 0x800N (... B)` |
| C (`someip-cli`) | `OK <path> = <value> (from someip event 0x800N)` |
| A (`range-ai`) | `Range = ... km` |

### Step 6 — Run the 7-phase scenario (slider moves on the host)

All inputs come from the **PyTk dashboard** (Step 4). Watch Terminal A
for the recomputed `Range`. The bridge logs confirm the cross-VM
round trip for cabin signals; battery signals don't need the bridge
because BMS already lives on VM1.

| Section in the GUI | Slider | Sets VSS path |
|---|---|---|
| Battery | Voltage | `Vehicle.Powertrain.TractionBattery.CurrentVoltage` |
| Battery | Current | `Vehicle.Powertrain.TractionBattery.CurrentCurrent` |
| Battery | SoC | `Vehicle.Powertrain.TractionBattery.StateOfCharge.Current` |
| Cabin HVAC | Ambient Temp | `Vehicle.Cabin.HVAC.AmbientAirTemperature` |
| Cabin Seat | Heating | `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` |
| Cabin Seat | Heating-Cooling | `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` |

#### Phase 1 — cold start, fully charged (Battery section)

| Slider | Set to |
|---|---|
| Voltage | `420.0` V |
| Current | `25.5` A |
| SoC | `100` % |

Terminal A → `Range = 417 km`.

#### Phase 2 — normal cruising (Battery)

Move SoC: `75` → `50`. Terminal A → `312 km` → `208 km`.

#### Phase 3 — hard acceleration (Battery)

Set Current to `90.0` A. Terminal A → `99 km`.

#### Phase 4 — driver eases off, voltage sags (Battery)

Set Current back to `25.5` A. Set Voltage to `380.0` V.
Terminal A → `208 km`.

#### Phase 5 — critical SoC + recovery (Battery)

Move SoC: `12` → `25`. Terminal A → `50 km` (trigger) → `104 km`.

#### Phase 6 — cold snap (Cabin HVAC)

Move Ambient Temp: `22.0` → `10.0` → `0.0` → `-10.0` °C.
Terminal A → `104 km` → `93 km` → `76 km` → `64 km`.

While at `-10 °C`, drop SoC to `12` % (Battery section):
Terminal A → `31 km` (vs. 50 km warm = 38 % less range purely from cold).

Bring Ambient Temp back to `22.0` °C.

#### Phase 7 — seat heater + ventilation (Cabin Seat)

| Slider | Set to | Expected Range |
|---|---|---|
| Heating | `100` | `42 km` |
| Heating | `50`  | `46 km` |
| Heating | `0`   | `50 km` |
| Heating-Cooling | `-100` | `48 km` |
| Heating-Cooling | `50`   | `39 km` |
| Heating-Cooling | `0`    | `50 km` |

#### Reset to a quiet baseline (after the demo)

Battery section:

| Slider | Set to |
|---|---|
| Voltage | `400.0` V |
| Current | `20.0` A |
| SoC | `80` % |

Cabin section:

| Slider | Set to |
|---|---|
| Ambient Temp | `22.0` °C |
| Heating | `0` |
| Heating-Cooling | `0` |

Terminal A → `333 km` (everything green, no penalties).

The deeper deep-dive — VSS path choices, the range formulas, the
SOME/IP service contract, the cold/cabin penalty math — lives in
[`ev-range-extender/vm1/README.md`](ev-range-extender/vm1/README.md)
and [`ev-range-extender/vm2/README.md`](ev-range-extender/vm2/README.md).

### Step 7 — Shutdown

```bash
# Stop the inner processes:
#  - In each Kuksa CLI prompt:                    Ctrl+D
#  - In Terminals A, C, D2 (range_ai + bridges):  Ctrl+C

# Power both VMs off cleanly
ssh ubuntu@192.168.100.10 'sudo poweroff'
ssh ubuntu@192.168.100.11 'sudo poweroff'

# Tear down host-side networking and any orphan QEMU
sudo pkill -9 -f 'qemu-system-x86_64' 2>/dev/null
sudo ip link delete tap1 2>/dev/null
sudo ip link delete tap2 2>/dev/null
sudo ip link delete br0  2>/dev/null
```

---

## Known issues and workarounds

### A. `apt-get install` or `pip install` fails silently during cloud-init

**Symptom**: SSH works, but `which docker` says nothing on VM1, or
`python3 -c "import kuksa_client"` fails on VM2.

**Cause**: QEMU's user-mode SLIRP NAT (the second NIC on each VM) does
**not carry IPv6**, but glibc on Ubuntu prefers IPv6 over IPv4. DNS
resolution of `archive.ubuntu.com` / `pypi.org` returns AAAA records
that time out, so apt and pip can't fetch anything.

**Manual recovery on the running VM**:

```bash
# On the affected VM
sudo apt -o Acquire::ForceIPv4=true update
sudo apt -o Acquire::ForceIPv4=true install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
sudo pip3 install --break-system-packages --ignore-installed \
    kuksa-client 'someipy>=1.0,<2.0' eclipse-zenoh grpcio grpcio-tools

# Then re-run the runtime helper to bring up the broker
sudo /usr/local/bin/evrange-start-runtime         # VM1
# or
sudo /usr/local/bin/evrange-start-databroker      # VM2
```

After that, confirm:
```bash
docker --version && docker ps && ss -ltn | grep 55555
```

> The cloud-init files have an `apt: Acquire::ForceIPv4` block plus a
> retry-with-IPv4 pip wrapper to do this automatically on the **next**
> fresh boot. VMs provisioned **before** that change need the manual
> recovery above one time.

### B. `qemu-system-x86_64: ... could not configure /dev/net/tun (tap1): Device or resource busy`

**Cause**: A previous QEMU process is still alive holding the tap.
**Fix**: see step 1 of the Quick start (`pkill -9 qemu-system-x86_64`
+ delete the tap interfaces).

### C. VM2 Kuksa CLI says `not_found` for the cabin signals

**Cause**: VM2 is running the **bare** `kuksa-databroker:main` image
(no VSS preloaded) instead of the new `sdv-runtime:latest` image with
the standard COVESA catalog. This happens on VMs provisioned before
that cloud-init swap.

**Fix**: One-time on VM2:
```bash
ssh ubuntu@192.168.100.11
sudo docker rm -f kuksa-databroker
sudo docker pull ghcr.io/eclipse-autowrx/sdv-runtime:latest
sudo docker run -d --name kuksa-databroker --restart unless-stopped --network host \
    -e RUNTIME_NAME=ev-range-cabin \
    ghcr.io/eclipse-autowrx/sdv-runtime:latest
```
Then verify:
```text
metadata Vehicle.Cabin.HVAC.AmbientAirTemperature
metadata Vehicle.Cabin.Seat.Row1.DriverSide.Heating
metadata Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
```
All 3 must return `[metadata] OK`.

### D. VM2 publishes but VM1 sees nothing (cross-VM bridge)

Common to both methods:

- The matching bridge isn't running on VM1 (Terminal C in Step 5).
- `iptables -A FORWARD -i br0 -o br0 -j ACCEPT` is missing on the host
  (this rule is what allows VM↔VM traffic on `br0`, including
  SOME/IP-SD multicast and Zenoh TCP).

For **Method 1 — Zenoh**:

- Verify TCP reachability: `nc -zv 192.168.100.10 7447` from VM2 must
  succeed. If not, the host's `iptables FORWARD` rule above is missing
  or `zenoh_client.py` isn't running on VM1 yet.

For **Method 2 — SOME/IP**:

- Verify discovery and event traffic on VM1:
  ```bash
  sudo tcpdump -ni any -X 'udp port 30490' &      # SD multicast offers from VM2
  sudo tcpdump -ni any -X 'udp port 30510' &      # event traffic
  ```
  You should see one Offer entry roughly every 2 seconds while
  `someip_publisher.py` is running on VM2.
- If `someip_publisher.py` exits with `OSError: Cannot assign requested
  address`, `ens3` did not get `192.168.100.11` yet — wait a few
  seconds for cloud-init's networkd to finish, or pass
  `--interface 192.168.100.11` explicitly.

### E. Dashboard slider moves but the ECU sees nothing

- The matching ECU's systemd service is stopped:
  ```bash
  ssh ubuntu@192.168.100.10 'systemctl status ev-range-bms'
  ssh ubuntu@192.168.100.11 'systemctl status ev-range-hvac ev-range-seat'
  # restart if needed
  ssh ubuntu@192.168.100.10 'sudo systemctl restart ev-range-bms'
  ```
- The ECU service started before Kuksa was up, hit its 120 s wait
  timeout, and gave up. The unit's `Restart=on-failure` recovers it
  automatically after `RestartSec=5`, but you can force it with
  `sudo systemctl restart ev-range-{bms,hvac,seat}`.
- The host-side `iptables FORWARD -i br0 -o br0 -j ACCEPT` rule is
  missing (also blocks host -> VM Zenoh).
- Wrong VM IPs / ports passed to `pytk_dashboard.py`. Check the first
  line it prints: `[pytk] dialing Zenoh endpoints: [...]`. Override
  with `--vm1 / --vm2 / --bms-port / --hvac-port / --seat-port` if
  needed.
- `python3-tk` not installed on the host: `sudo apt install -y python3-tk`.
- `eclipse-zenoh` Python binding missing on the host:
  `pip install --user --break-system-packages eclipse-zenoh`.

---

## Side demos (not required for EV Range Extender)

Three small, self-contained pub/sub examples ship alongside the main
demo. They are **not part of the EV Range Extender flow above** —
each is just a one-shot connectivity check or learning example, with
its own README.

- **`zenoh-demo/`** — bare Zenoh pub/sub. VM1 runs `pub.py`, VM2
  runs `sub.py`. Useful as a connectivity check on `tcp/7447` if
  Method 1 is not delivering samples.
- **`someip-demo/`** — bare SOME/IP `Hello` service + client. VM2
  runs `server.py`, VM1 runs `client.py`. Useful as a connectivity
  check on `udp/30490`+`udp/30509`+`udp/30510` if Method 2 is not
  delivering events.
- **`grpc-demo/`** — basic Python gRPC client/server. VM2 runs
  `server.py`, VM1 runs `client.py`. Demonstrates both unary and
  server-streaming RPCs over `tcp/50051`.

---

## Cleaning the host

To wipe everything and re-download the Ubuntu image on the next run:

```bash
sudo rm -rf output
```

To wipe only the VMs but keep the cached Ubuntu image:

```bash
sudo rm -f output/vm{1,2}.qcow2 output/seed{1,2}.img
```

---

## Credits

Built on top of the [eclipse-sdv-blueprint](../README.md) EV Range
Extender use case. Uses
[`ghcr.io/eclipse-autowrx/sdv-runtime`](https://github.com/eclipse-autowrx/sdv-runtime),
[`ghcr.io/eclipse-kuksa/kuksa-databroker-cli`](https://github.com/eclipse-kuksa/kuksa-databroker),
[someipy](https://github.com/chrizog/someipy) (the SOME/IP transport
implementing the upstream "Eclipse SCore / SOME-IP" link, **Method 1**),
and [Eclipse Zenoh](https://zenoh.io/) (peer-to-peer pub/sub,
**Method 2**) for cross-VM communication. Both transports are
preinstalled on every VM and can be swapped at any time.
