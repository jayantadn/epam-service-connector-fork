# QEMU Multi-VM SDV Lab — EV Range Extender

A zero-touch multi-VM lab built on QEMU + KVM + cloud-init that hosts the
**EV Range Extender** end-to-end demo from the
[eclipse-sdv-blueprint](../README.md). Two Ubuntu 24.04 ("noble") VMs are
provisioned automatically, joined to a private Layer-2 bridge, and each
runs its own `digital.auto sdv-runtime` Kuksa Databroker. Vehicle
signals are exchanged between the two brokers over **Eclipse Zenoh**;
**Eclipse Kuksa CLI** is the only signal source.

Reference prototype on the digital.auto playground:
[ev-range prototype](https://playground.digital.auto/model/67f76c0d8c609a0027662a69/library/prototype/69ce30f438bb8e98f0af5ac8/dashboard)

---

## Architecture at a glance

```
WSL host (192.168.100.1)
  br0  +-- tap1 -----------------------+      +--- tap2 --------------------+
       |  VM1 (192.168.100.10)         |      |  VM2 (192.168.100.11)       |
       |  ev-range SDV Runtime          |      |  ev-range-cabin SDV Runtime |
       |  Kuksa Databroker :55555       |      |  Kuksa Databroker :55555    |
       |                                |      |                              |
       |  range_ai.py    --publishes--> |      |                              |
       |    Vehicle.Powertrain.Range    |      |                              |
       |                                |      |                              |
       |  zenoh_client.py <--Zenoh------|<-----| zenoh_publisher.py           |
       |    :7447                       | tcp  |   bridges local Kuksa to     |
       |    writes cabin signals into   | 7447 |   Zenoh keys ev-range/vm2/** |
       |    the local Databroker        |      |                              |
       +--------------------------------+      +------------------------------+
            |                                            |
            | Kuksa CLI (docker)                         | Kuksa CLI (docker)
            v                                            v
       3 battery signals                          3 cabin signals
       (current, voltage, SoC)                    (ambient temp, seat heating,
                                                   seat heating-cooling)
```

- **VM1** runs the `ev-range` runtime, the Range Compute AI
  (`range_ai.py`) and a Zenoh subscriber (`zenoh_client.py`).
- **VM2** runs the `ev-range-cabin` runtime (same image, different
  `RUNTIME_NAME`) and a Zenoh publisher (`zenoh_publisher.py`).
- **All input signals** are typed into the Kuksa CLI on the appropriate
  VM. There is no Python publisher / sensor simulator anywhere.
- **All cross-VM transport** is Zenoh. **All intra-VM transport** is the
  local Kuksa Databroker on `127.0.0.1:55555`.

---

## What's in this folder

| Path | Purpose |
|---|---|
| `setup.sh` | Downloads the Ubuntu cloud image, builds two qcow2 disks + cloud-init seeds, brings up `br0`/`tap1`/`tap2`, launches both VMs, polls for the broker on VM1 to be healthy. |
| `vm1_launch.sh` / `vm2_launch.sh` | QEMU invocations for each VM (called by `setup.sh`). |
| `input/user-data-vm1` | Cloud-init for VM1: installs `docker.io`, Python deps (`kuksa-client`, `eclipse-zenoh`, `grpcio`, ...), runs `xmel-start-runtime` to bring up `ghcr.io/eclipse-autowrx/sdv-runtime:latest` with `RUNTIME_NAME=ev-range`. |
| `input/user-data-vm2` | Cloud-init for VM2: installs same packages, runs `xmel-start-databroker` to bring up the **same `sdv-runtime` image** with `RUNTIME_NAME=ev-range-cabin` (so VM2's Databroker boots with the standard COVESA VSS catalog already loaded — no JSON files anywhere). |
| `input/network-vm1.yaml` / `input/network-vm2.yaml` | Static IP for the bridge NIC; DHCP for the SLIRP NIC (outbound internet). |
| **`ev-range-extender/`** | **The main demo.** See the section below. |
| `zenoh-demo/` | A minimal "VM1 -> VM2" Zenoh pub/sub example used as a connectivity smoke test (independent of the EV demo). |
| `grpc-demo/` | A minimal "VM1 -> VM2" Python gRPC client/server example (independent of the EV demo). |
| `output/` | Generated qcow2 disks, seed images, base Ubuntu image (gitignored). |

### `ev-range-extender/` layout

```
ev-range-extender/
├── vm1/
│   ├── README.md            # full VM1 docs incl. step-by-step Phases 1-7
│   ├── range_ai.py          # subscribes to 6 inputs, publishes Vehicle.Powertrain.Range
│   └── zenoh_client.py      # listens on tcp/0.0.0.0:7447, writes into local Kuksa
└── vm2/
    ├── README.md            # full VM2 docs (Kuksa-CLI-only workflow)
    └── zenoh_publisher.py   # subscribes to local Kuksa, publishes JSON over Zenoh -> VM1
```

The two demo READMEs (`ev-range-extender/vm1/README.md`,
`ev-range-extender/vm2/README.md`) contain the deep dive: VSS path
choices, the range model, the cold-weather and cabin-load formulas,
and the full 7-phase scenario.

---

## IP map and ports

| Host / VM | IP | Listening ports |
|---|---|---|
| WSL host | 192.168.100.1 (`br0`) | — |
| VM1 | 192.168.100.10 (`ens3`) | 22 (ssh), 55555 (ev-range Kuksa), 7447 (Zenoh client) |
| VM2 | 192.168.100.11 (`ens3`) | 22 (ssh), 55555 (ev-range-cabin Kuksa) |

Both VMs also have a SLIRP NIC (`ens4`, 10.0.2.15/24) for **outbound**
internet (apt, pip, docker pull). SLIRP does not carry IPv6 — see
"Known issues" below for why this matters and how it's worked around.

---

## Quick start (the happy path, ~10 min on a fresh host)

### 1. Host-side prep (once per host / per reboot)

```bash
# Kill any orphan VMs from a previous run (fixes "tap1: Device or resource busy")
sudo pkill -9 -f 'qemu-system-x86_64' 2>/dev/null
sudo ip link delete tap1 2>/dev/null
sudo ip link delete tap2 2>/dev/null
sudo ip link delete br0  2>/dev/null

# Install host tools
sudo apt update
sudo apt install -y qemu-system qemu-utils cloud-image-utils wget bridge-utils sshpass

# Allow VM <-> VM traffic on the bridge (WSL Netfilter blocks it by default)
sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT
sudo sysctl -w net.ipv4.ip_forward=1
```

### 2. Provision and boot both VMs

```bash
chmod +x *.sh
./setup.sh
```

The script downloads `noble-server-cloudimg-amd64.img` on first run
(~600 MB, ~3 min), builds the qcow2 disks, brings up the bridge, boots
both VMs, then polls VM1's Databroker port until it answers.

> **If `./setup.sh` hangs at the "Waiting for SDV Runtime ..." dot
> spinner for more than 5 min**, cloud-init's apt step probably failed
> (see "Known issues") and Docker is missing inside the VM. Hit Ctrl+C
> and follow the manual recovery below.

### 3. Verify both VMs are reachable (password: `ubuntu`)

```bash
ssh ubuntu@192.168.100.10 'hostname && docker ps && ss -ltn | grep 55555'
ssh ubuntu@192.168.100.11 'hostname && docker ps && ss -ltn | grep 55555'
```

You should see `vm1` / `vm2`, the `sdv-runtime` / `kuksa-databroker`
container, and a listener on `:55555`.

### 4. Run the EV Range Extender demo

The full step-by-step (5 terminals, 7 phases, 31 steps) lives in
[`ev-range-extender/vm1/README.md`](ev-range-extender/vm1/README.md).

The shortest possible kickoff — open one Kuksa CLI on VM1 and one on
VM2, then start `range_ai.py` in a third shell:

```bash
# Terminal A — VM1
ssh ubuntu@192.168.100.10
cd /home/ubuntu/ev-range-extender/vm1
python3 range_ai.py
```

```bash
# Terminal B — VM1, Kuksa CLI
ssh ubuntu@192.168.100.10
docker run -it --rm --network host ghcr.io/eclipse-kuksa/kuksa-databroker-cli:main
```

In Terminal B:
```text
publish Vehicle.Powertrain.TractionBattery.CurrentVoltage         420.0
publish Vehicle.Powertrain.TractionBattery.CurrentCurrent          25.5
publish Vehicle.Powertrain.TractionBattery.StateOfCharge.Current  100.0
```
Terminal A logs `Range = 417 km`. From there the per-VM READMEs walk
through Phases 2-7 (cruising, hard accel, low SoC trigger, cold snap
from VM2, seat heater + ventilation from VM2).

### 5. Shutdown

```bash
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
    kuksa-client eclipse-zenoh grpcio grpcio-tools

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

- `zenoh_client.py` is not running on VM1 (Terminal C in the demo flow).
- `iptables FORWARD -i br0 -o br0 -j ACCEPT` is missing on the host.
- Verify: `nc -zv 192.168.100.10 7447` from VM2 must succeed.

---

## Side demos (not required for EV Range Extender)

Both folders ship a self-contained "VM1 -> VM2" example that can be
used as a connectivity smoke test:

- **`zenoh-demo/`** — basic Zenoh pub/sub. VM1 runs `pub.py`, VM2 runs
  `sub.py`. Useful to confirm `tcp/7447` reachability between the VMs
  before bringing up the heavier ev-range-extender stack.
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
and [Eclipse Zenoh](https://zenoh.io/) for cross-VM transport.
