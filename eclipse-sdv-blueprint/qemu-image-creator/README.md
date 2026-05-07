# QEMU Multi-VM SDV Lab — EV Range Extender

A zero-touch multi-VM lab built on QEMU + KVM + cloud-init that hosts the
**EV Range Extender** end-to-end demo from the
[eclipse-sdv-blueprint](../README.md). Two Ubuntu 24.04 ("noble") VMs are
provisioned automatically, joined to a private Layer-2 bridge, and each
runs its own `digital.auto sdv-runtime` Kuksa Databroker.
**Eclipse Kuksa CLI** is the only signal source.

Reference prototype on the digital.auto playground:
[ev-range prototype](https://playground.digital.auto/model/67f76c0d8c609a0027662a69/library/prototype/69ce30f438bb8e98f0af5ac8/dashboard)

---

## Two cross-VM transport options — pick one

Vehicle signals travel from VM2's local Kuksa Databroker to VM1's local
Kuksa Databroker over a small Python bridge. The repo ships **two
fully working bridge implementations**, side by side, sharing the same
Kuksa Databrokers, the same VSS paths, and the same `range_ai.py`
consumer. Pick the one that matches what you want to demonstrate:

| | **Method 1: SOME/IP (Eclipse SCore)** | **Method 2: Eclipse Zenoh** |
|---|---|---|
| Why pick it | Matches the upstream blueprint's "Eclipse SCore / SOME-IP" HPC↔Zonal link. The protocol used in real automotive SOME/IP buses. | Lightweight pub/sub, simpler wire, easier to reason about for first-time SDV demos. |
| VM1 bridge file | `ev-range-extender/vm1/someip_client.py` | `ev-range-extender/vm1/zenoh_client.py` |
| VM2 bridge file | `ev-range-extender/vm2/someip_publisher.py` | `ev-range-extender/vm2/zenoh_publisher.py` |
| Wire protocol | SOME/IP over UDP, with SOME/IP-SD discovery | Zenoh peer-to-peer over TCP, JSON payloads |
| Ports | `udp/30490` (SD multicast `224.224.224.245`), `udp/30509` (VM2 events), `udp/30510` (VM1 events) | `tcp/7447` (VM1 listener; VM2 connects out) |
| Smoke-test demo folder | `someip-demo/` (`server.py` + `client.py`) | `zenoh-demo/` (`pub.py` + `sub.py`) |
| Python dep on each VM | `someipy>=1.0,<2.0` | `eclipse-zenoh` |

Both packages are pre-installed by cloud-init on every fresh boot, so
**no extra setup is needed to switch between them** — just run the
matching bridge pair and leave the other pair stopped. Don't run both
pairs at the same time or both will write the same VSS values into
VM1's Kuksa Databroker.

The "Quick start" below has separate **Method 1** and **Method 2**
blocks at Step 5; everything before that step (host prep, boot, code
staging) and everything after (the 7-phase demo scenario, shutdown) is
identical for both transports.

---

## Architecture at a glance

```
WSL host (192.168.100.1)
  br0  +-- tap1 ------------------------+     +--- tap2 ---------------------+
       |  VM1 (192.168.100.10) [HPC]    |     |  VM2 (192.168.100.11) [Zonal]|
       |  ev-range SDV Runtime          |     |  ev-range-cabin SDV Runtime  |
       |  Kuksa Databroker :55555       |     |  Kuksa Databroker :55555     |
       |                                |     |                              |
       |  range_ai.py    --publishes--> |     |                              |
       |    Vehicle.Powertrain.Range    |     |                              |
       |                                |     |                              |
       |  someip_client.py <-SOME/IP----|<----| someip_publisher.py          |
       |    udp/30490 (SD)              |     |   bridges local Kuksa to     |
       |    udp/30510 (events)          |     |   SOME/IP service 0xCAB0     |
       |    writes cabin signals into   |     |   eventgroup 0x0001          |
       |    the local Databroker        |     |   events 0x8001..0x8003      |
       |                                |     |                              |
       |  (legacy) zenoh_client.py      |     |  (legacy) zenoh_publisher.py |
       |    tcp/7447                    |     |   tcp -> VM1:7447            |
       +--------------------------------+     +------------------------------+
            |                                            |
            | Kuksa CLI (docker)                         | Kuksa CLI (docker)
            v                                            v
       3 battery signals                          3 cabin signals
       (current, voltage, SoC)                    (ambient temp, seat heating,
                                                   seat heating-cooling)
```

- **VM1** (the HPC) runs the `ev-range` runtime, the Range Compute AI
  (`range_ai.py`), and a subscriber bridge — either `someip_client.py`
  (Method 1) or `zenoh_client.py` (Method 2).
- **VM2** (the Zonal node) runs the `ev-range-cabin` runtime (same
  image, different `RUNTIME_NAME`) and a publisher bridge — either
  `someip_publisher.py` (Method 1) or `zenoh_publisher.py` (Method 2).
- **All input signals** are typed into the Kuksa CLI on the appropriate
  VM. There is no Python publisher / sensor simulator anywhere.
- **Cross-VM transport** is whichever method you picked at Step 5
  below. Both stacks (`someipy` and `eclipse-zenoh`) are installed
  and importable on every boot, so switching is just "stop one pair,
  start the other". Don't run both pairs at the same time.
- **All intra-VM transport** is the local Kuksa Databroker on
  `127.0.0.1:55555`.

---

## What's in this folder

| Path | Purpose |
|---|---|
| `setup.sh` | Downloads the Ubuntu cloud image, builds two qcow2 disks + cloud-init seeds, brings up `br0`/`tap1`/`tap2`, launches both VMs, polls for the broker on VM1 to be healthy. |
| `vm1_launch.sh` / `vm2_launch.sh` | QEMU invocations for each VM (called by `setup.sh`). |
| `input/user-data-vm1` | Cloud-init for VM1: installs `docker.io`, Python deps (`kuksa-client`, `someipy`, `eclipse-zenoh`, `grpcio`, ...), runs `xmel-start-runtime` to bring up `ghcr.io/eclipse-autowrx/sdv-runtime:latest` with `RUNTIME_NAME=ev-range`. |
| `input/user-data-vm2` | Cloud-init for VM2: installs same packages, runs `xmel-start-databroker` to bring up the **same `sdv-runtime` image** with `RUNTIME_NAME=ev-range-cabin` (so VM2's Databroker boots with the standard COVESA VSS catalog already loaded — no JSON files anywhere). |
| `input/network-vm1.yaml` / `input/network-vm2.yaml` | Static IP for the bridge NIC; DHCP for the SLIRP NIC (outbound internet). |
| **`ev-range-extender/`** | **The main demo.** See the section below. |
| `someip-demo/` | A minimal "VM2 -> VM1" SOME/IP pub/sub example. Use it as a connectivity smoke test before running **Method 1**. |
| `zenoh-demo/` | A minimal "VM1 -> VM2" Zenoh pub/sub example. Use it as a connectivity smoke test before running **Method 2**. |
| `grpc-demo/` | A minimal "VM1 -> VM2" Python gRPC client/server example (independent of the EV demo). |
| `output/` | Generated qcow2 disks, seed images, base Ubuntu image (gitignored). |

### `ev-range-extender/` layout

```
ev-range-extender/
├── common/
│   ├── __init__.py
│   └── someip_service.py    # shared SOME/IP service IDs + payload codecs
├── vm1/
│   ├── README.md            # full VM1 docs incl. step-by-step Phases 1-7
│   ├── range_ai.py          # subscribes to 6 inputs, publishes Vehicle.Powertrain.Range
│   ├── someip_client.py     # Method 1: SD multicast + udp/30510, writes into local Kuksa
│   └── zenoh_client.py      # Method 2: listens on tcp/0.0.0.0:7447, writes into local Kuksa
└── vm2/
    ├── README.md            # full VM2 docs (Kuksa-CLI-only workflow)
    ├── someip_publisher.py  # Method 1: subscribes to local Kuksa, sends SOME/IP events to VM1
    └── zenoh_publisher.py   # Method 2: same idea but JSON over Zenoh
```

The two demo READMEs (`ev-range-extender/vm1/README.md`,
`ev-range-extender/vm2/README.md`) contain the deep dive: VSS path
choices, the range model, the cold-weather and cabin-load formulas,
the SOME/IP service contract, and the full 7-phase scenario.

---

## IP map and ports

| Host / VM | IP | Listening ports |
|---|---|---|
| WSL host | 192.168.100.1 (`br0`) | — |
| VM1 | 192.168.100.10 (`ens3`) | 22 (ssh), 55555 (ev-range Kuksa), **udp/30490** (SOME/IP-SD), **udp/30510** (SOME/IP events), **tcp/7447** (Zenoh client) |
| VM2 | 192.168.100.11 (`ens3`) | 22 (ssh), 55555 (ev-range-cabin Kuksa), **udp/30490** (SOME/IP-SD), **udp/30509** (SOME/IP events) |

The SOME/IP ports are used by **Method 1**; the Zenoh `tcp/7447` port
is used by **Method 2**. Both transports run on the same two VMs and
talk to the same two Kuksa Databrokers, so you only need to start one
of them at a time — run **either** the SOME/IP pair
(`someip_publisher.py` + `someip_client.py`) **or** the Zenoh pair
(`zenoh_publisher.py` + `zenoh_client.py`), not both, to avoid
double-writing into the local Kuksa Databroker on VM1. SOME/IP-SD
discovery uses the standard multicast group `224.224.224.245:30490`.

Both VMs also have a SLIRP NIC (`ens4`, 10.0.2.15/24) for **outbound**
internet (apt, pip, docker pull). SLIRP does not carry IPv6 — see
"Known issues" below for why this matters and how it's worked around.

---

## Quick start (~10 min on a fresh host)

The first four steps and the 7-phase demo (Steps 6-7) are **identical
for both transports**. Step 5 is where you pick **Method 1 (SOME/IP)**
or **Method 2 (Zenoh)**.

### Step 1 — Host-side prep (once per host / per reboot)

```bash
# Kill any orphan VMs from a previous run (fixes "tap1: Device or resource busy")
sudo pkill -9 -f 'qemu-system-x86_64' 2>/dev/null
sudo ip link delete tap1 2>/dev/null
sudo ip link delete tap2 2>/dev/null
sudo ip link delete br0  2>/dev/null

# Install host tools
sudo apt update
sudo apt install -y qemu-system qemu-utils cloud-image-utils wget bridge-utils sshpass

# Allow VM <-> VM traffic on the bridge (WSL Netfilter blocks it by default).
# This rule is also what unblocks SOME/IP-SD multicast and Zenoh TCP between the VMs.
sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT
sudo sysctl -w net.ipv4.ip_forward=1
```

### Step 2 — Provision and boot both VMs

```bash
cd /home/goutham/Gitrepos/epam-service-connector-fork/eclipse-sdv-blueprint/qemu-image-creator
chmod +x *.sh
./setup.sh
```

The script downloads `noble-server-cloudimg-amd64.img` on first run
(~600 MB, ~3 min), builds the qcow2 disks, brings up the bridge, boots
both VMs, then polls VM1's Databroker port until it answers.

> **If `./setup.sh` hangs at "Waiting for SDV Runtime ..." for more
> than 5 min**, cloud-init's apt step probably failed (see "Known
> issues"). Ctrl+C and follow the manual recovery in section A.

### Step 3 — Verify both VMs are reachable (password: `ubuntu`)

```bash
ssh ubuntu@192.168.100.10 'hostname && docker ps && ss -ltn | grep 55555'
ssh ubuntu@192.168.100.11 'hostname && docker ps && ss -ltn | grep 55555'
```

You should see `vm1` / `vm2`, the `sdv-runtime` / `kuksa-databroker`
container, and a listener on `:55555`. Then verify the Python deps for
both transports are present:

```bash
ssh ubuntu@192.168.100.10 'python3 -c "from kuksa_client.grpc.aio import VSSClient; import someipy, zenoh; print(\"VM1 OK\")"'
ssh ubuntu@192.168.100.11 'python3 -c "from kuksa_client.grpc.aio import VSSClient; import someipy, zenoh; print(\"VM2 OK\")"'
```

If either fails with `ModuleNotFoundError`, install the missing pip
package on the affected VM (see "Known issues" section A).

### Step 4 — Stage the demo code on both VMs

Copy the `ev-range-extender/` tree (which contains both transport
implementations) to both VMs:

```bash
cd /home/goutham/Gitrepos/epam-service-connector-fork/eclipse-sdv-blueprint/qemu-image-creator
SSHOPTS="-o StrictHostKeyChecking=accept-new"

sshpass -p 'ubuntu' scp $SSHOPTS -r ev-range-extender ubuntu@192.168.100.10:/home/ubuntu/
sshpass -p 'ubuntu' scp $SSHOPTS -r ev-range-extender ubuntu@192.168.100.11:/home/ubuntu/

# Strip Windows CRLFs (defensive — fixes "bash\r: No such file or directory")
sshpass -p 'ubuntu' ssh $SSHOPTS ubuntu@192.168.100.10 \
    "cd /home/ubuntu/ev-range-extender && sed -i 's/\r\$//' vm1/*.py common/*.py"
sshpass -p 'ubuntu' ssh $SSHOPTS ubuntu@192.168.100.11 \
    "cd /home/ubuntu/ev-range-extender && sed -i 's/\r\$//' vm2/*.py common/*.py"
```

> The `-o StrictHostKeyChecking=accept-new` flag is important — without
> it, `sshpass` cannot answer the very first "yes/no/[fingerprint]?"
> prompt, the `scp` exits silently, and you end up with empty
> `/home/ubuntu/ev-range-extender/` on the VMs.

Verify the files landed:

```bash
ssh ubuntu@192.168.100.10 'ls /home/ubuntu/ev-range-extender/vm1 /home/ubuntu/ev-range-extender/common'
ssh ubuntu@192.168.100.11 'ls /home/ubuntu/ev-range-extender/vm2 /home/ubuntu/ev-range-extender/common'
```

VM1 should list `range_ai.py`, `someip_client.py`, `zenoh_client.py`,
`README.md` plus `__init__.py`, `someip_service.py`. VM2 should list
`someip_publisher.py`, `zenoh_publisher.py`, `README.md` plus the same
common files.

### Step 5 — Pick a transport and start the bridge

You'll need **5 SSH terminals total**: 3 to VM1, 2 to VM2.

| Terminal | VM | What runs there | Method 1 (SOME/IP) | Method 2 (Zenoh) |
|---|---|---|---|---|
| **A** | VM1 | Range Compute AI | `python3 range_ai.py` | `python3 range_ai.py` |
| **B** | VM1 | Kuksa CLI for battery | docker kuksa-databroker-cli | docker kuksa-databroker-cli |
| **C** | VM1 | Subscriber bridge | `python3 someip_client.py` | `python3 zenoh_client.py` |
| **D2** | VM2 | Publisher bridge | `python3 someip_publisher.py` | `python3 zenoh_publisher.py` |
| **D1** | VM2 | Kuksa CLI for cabin | docker kuksa-databroker-cli | docker kuksa-databroker-cli |

Open them in this order: **D2 → C → A → B → D1**. The order matters
only for SOME/IP-SD timing; running them in the listed order means C
sees the publisher's first Offer within ~2 seconds. For Zenoh the
order is more relaxed (TCP retries until the listener appears).

#### Method 1 — SOME/IP (Eclipse SCore)

```bash
# Terminal D2 — VM2: SOME/IP publisher (start FIRST so SD offers begin)
ssh ubuntu@192.168.100.11
cd /home/ubuntu/ev-range-extender/vm2
python3 someip_publisher.py
# expected: "Publisher running. Drive values from the Kuksa CLI on VM2 ..."

# Terminal C — VM1: SOME/IP client
ssh ubuntu@192.168.100.10
cd /home/ubuntu/ev-range-extender/vm1
python3 someip_client.py
# expected: "SOME/IP client running. Waiting for offers from VM2."

# Terminal A — VM1: Range Compute AI
ssh ubuntu@192.168.100.10
cd /home/ubuntu/ev-range-extender/vm1
python3 range_ai.py
# expected: "output : <waiting for StateOfCharge to be set>"

# Terminal B — VM1: Kuksa CLI (battery signals)
ssh ubuntu@192.168.100.10
docker run -it --rm --network host ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main
# expected: "kuksa.val.v1 >" prompt

# Terminal D1 — VM2: Kuksa CLI (cabin signals)
ssh ubuntu@192.168.100.11
docker run -it --rm --network host ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main
# expected: "kuksa.val.v1 >" prompt
```

The chain you should see on every VM2 publish:

| Where | What you'll see |
|---|---|
| D1 (VM2 CLI) | `[publish] OK` |
| D2 (`someip-pub`) | `FWD <path> = <value>  ->  someip event 0x800N (... B)` |
| C (`someip-cli`) | `OK <path> = <value> (from someip event 0x800N)` |
| A (`range-ai`) | `Range = ... km` |

#### Method 2 — Eclipse Zenoh (legacy / reference)

```bash
# Terminal C — VM1: Zenoh client (start FIRST — it owns the listening socket)
ssh ubuntu@192.168.100.10
cd /home/ubuntu/ev-range-extender/vm1
python3 zenoh_client.py
# expected: "Zenoh client running. Ctrl+C to stop."

# Terminal D2 — VM2: Zenoh publisher (connects out to VM1)
ssh ubuntu@192.168.100.11
cd /home/ubuntu/ev-range-extender/vm2
python3 zenoh_publisher.py
# expected: "Publisher running. Drive values from the Kuksa CLI on VM2 ..."

# Terminal A — VM1: Range Compute AI
ssh ubuntu@192.168.100.10
cd /home/ubuntu/ev-range-extender/vm1
python3 range_ai.py
# expected: "output : <waiting for StateOfCharge to be set>"

# Terminal B — VM1: Kuksa CLI (battery signals)
ssh ubuntu@192.168.100.10
docker run -it --rm --network host ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main

# Terminal D1 — VM2: Kuksa CLI (cabin signals)
ssh ubuntu@192.168.100.11
docker run -it --rm --network host ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main
```

The chain you should see on every VM2 publish:

| Where | What you'll see |
|---|---|
| D1 (VM2 CLI) | `[publish] OK` |
| D2 (`zenoh-pub`) | `FWD <path> = <value>  ->  zenoh (... B)` |
| C (`zenoh-cli`) | `OK <path> = <value> (from vm2)` |
| A (`range-ai`) | `Range = ... km` |

> **Don't run both methods at once.** Both bridges write the same VSS
> paths into VM1's Kuksa Databroker, so running them simultaneously
> causes duplicate writes and duplicate `[range-ai] input :` lines for
> every publish.

### Step 6 — Run the 7-phase scenario (same for both methods)

All `publish` commands go inside the Kuksa CLI prompts. Watch
Terminal A for the recomputed `Range`. Each phase below has a single
expected `Range` value; the bridge logs (`[someip-pub]` /
`[someip-cli]` for Method 1, or `[zenoh-pub]` / `[zenoh-cli]` for
Method 2) confirm the round trip.

#### Phase 1 — cold start, fully charged (Terminal B, VM1 CLI)

```text
publish Vehicle.Powertrain.TractionBattery.CurrentVoltage         420.0
publish Vehicle.Powertrain.TractionBattery.CurrentCurrent          25.5
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current  100.0
```
Terminal A → `Range = 417 km`.

#### Phase 2 — normal cruising (Terminal B)

```text
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   75
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   50
```
Terminal A → `312 km` → `208 km`.

#### Phase 3 — hard acceleration (Terminal B)

```text
publish Vehicle.Powertrain.TractionBattery.CurrentCurrent          90.0
```
Terminal A → `99 km`.

#### Phase 4 — driver eases off, voltage sags (Terminal B)

```text
publish Vehicle.Powertrain.TractionBattery.CurrentCurrent          25.5
publish Vehicle.Powertrain.TractionBattery.CurrentVoltage         380.0
```
Terminal A → `208 km`.

#### Phase 5 — critical SoC + recovery (Terminal B)

```text
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   12
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   25
```
Terminal A → `50 km` (trigger) → `104 km`.

#### Phase 6 — cold snap from VM2 (Terminal D1, VM2 CLI)

```text
publish Vehicle.Cabin.HVAC.AmbientAirTemperature  22.0
publish Vehicle.Cabin.HVAC.AmbientAirTemperature  10.0
publish Vehicle.Cabin.HVAC.AmbientAirTemperature   0.0
publish Vehicle.Cabin.HVAC.AmbientAirTemperature -10.0
```
Terminal A → `104 km` → `93 km` → `76 km` → `64 km`.

Drop SoC at -10 °C (Terminal B):
```text
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current 12
```
Terminal A → `31 km` (vs. 50 km warm = 38 % less range purely from cold).

Recovery (Terminal D1):
```text
publish Vehicle.Cabin.HVAC.AmbientAirTemperature 22.0
```

#### Phase 7 — seat heater + ventilation from VM2 (Terminal D1)

```text
publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating 100
publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating  50
publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating   0
publish Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling -100
publish Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling   50
publish Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling    0
```
Terminal A → `42 km` → `46 km` → `50 km` → `48 km` → `39 km` → `50 km`.

#### Reset to a quiet baseline (after the demo)

Terminal B (battery to nominal cruise):
```text
publish Vehicle.Powertrain.TractionBattery.CurrentVoltage         400.0
publish Vehicle.Powertrain.TractionBattery.CurrentCurrent          20.0
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   80.0
```
Terminal D1 (cabin off, mild day):
```text
publish Vehicle.Cabin.HVAC.AmbientAirTemperature                 22.0
publish Vehicle.Cabin.Seat.Row1.DriverSide.Heating                  0
publish Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling           0
```
Terminal A → `333 km` (everything green, no penalties).

The deeper deep-dive — VSS path choices, the range formulas, the
SOME/IP service contract, the cold/cabin penalty math — lives in
[`ev-range-extender/vm1/README.md`](ev-range-extender/vm1/README.md)
and [`ev-range-extender/vm2/README.md`](ev-range-extender/vm2/README.md).

### Step 7 — Shutdown

```bash
# In each Kuksa CLI prompt: Ctrl+D
# Ctrl+C in Terminals A, C, D2

ssh ubuntu@192.168.100.10 'sudo poweroff'
ssh ubuntu@192.168.100.11 'sudo poweroff'
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
sudo /usr/local/bin/xmel-start-runtime         # VM1
# or
sudo /usr/local/bin/xmel-start-databroker      # VM2
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

### D. VM2 publishes but VM1 sees nothing

Common to both methods:

- The matching bridge isn't running on VM1 (Terminal C in Step 5).
- `iptables -A FORWARD -i br0 -o br0 -j ACCEPT` is missing on the host
  (this rule is what allows VM↔VM traffic on `br0`, including
  SOME/IP-SD multicast and Zenoh TCP).

For **Method 1 — SOME/IP**:

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

For **Method 2 — Zenoh**:

- Verify TCP reachability: `nc -zv 192.168.100.10 7447` from VM2 must
  succeed. If not, the host's `iptables FORWARD` rule above is missing
  or `zenoh_client.py` isn't running on VM1 yet.

---

## Side demos (not required for EV Range Extender)

Each folder ships a self-contained example that can be used as a
connectivity smoke test:

- **`someip-demo/`** — basic SOME/IP service + client (Eclipse-SCore
  flavour). VM2 runs `server.py`, VM1 runs `client.py`. Useful to
  confirm `udp/30490` (SD multicast) + `udp/30509`/`udp/30510`
  (events) before bringing up the heavier ev-range-extender SOME/IP
  bridge.
- **`zenoh-demo/`** — basic Zenoh pub/sub (legacy reference transport).
  VM1 runs `pub.py`, VM2 runs `sub.py`. Useful to confirm `tcp/7447`
  reachability between the VMs.
- **`grpc-demo/`** — basic Python gRPC client/server. VM1 runs
  `client.py`, VM2 runs `server.py`. Demonstrates both unary and
  server-streaming RPCs over `tcp/50051`.

Each has its own README.

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
