# QEMU Multi-VM SDV Lab — EV Range Extender

A zero-touch multi-VM lab built on QEMU + KVM + cloud-init that hosts the
**EV Range Extender** end-to-end demo from the
[eclipse-sdv-blueprint](../README.md). Two Ubuntu 24.04 ("noble") VMs are
provisioned automatically, joined to a private Layer-2 bridge, and each
runs its own `digital.auto sdv-runtime` Kuksa Databroker. A **PyTk
hardware-simulator dashboard** running on the host pushes signal values
into per-VM ECUs (BMS, HVAC, Seat) that own the matching VSS branches in
each Databroker.

**Zero manual steps on the VMs.** Every Python app under
`ev-range-extender/` is auto-deployed by cloud-init on first boot and
auto-started as a `systemd` service. The developer only ever runs the
host-side PyTk dashboard.

Reference prototype on the digital.auto playground:
[ev-range prototype](https://playground.digital.auto/model/67f76c0d8c609a0027662a69/library/prototype/69ce30f438bb8e98f0af5ac8/dashboard)

---

## Architecture at a glance

```
HOST (192.168.100.1, br0)
└── hardware-sim/pytk_dashboard.py       <-- the ONLY thing the developer launches
        | Zenoh JSON over TCP:
        |   sim/battery/voltage          -> tcp/192.168.100.10:7460
        |   sim/battery/current          -> tcp/192.168.100.10:7460
        |   sim/battery/soc              -> tcp/192.168.100.10:7460
        |   sim/cabin/fan-speed          -> tcp/192.168.100.11:7461
        |   sim/cabin/seat/heating       -> tcp/192.168.100.11:7462
        |   sim/cabin/seat/hc            -> tcp/192.168.100.11:7462
        v
VM1 (192.168.100.10)                          VM2 (192.168.100.11)
+---------------------------------+           +---------------------------------+
| sdv-runtime container           |           | sdv-runtime container           |
|   Kuksa Databroker :55555       |           |   Kuksa Databroker :55555       |
|                                 |           |                                 |
| systemd auto-starts 3 services: |           | systemd auto-starts 3 services: |
|  ev-range-bms.service           |           |  ev-range-hvac.service          |
|  ev-range-zenoh-client.service  |<---zenoh--|  ev-range-zenoh-publisher.svc   |
|  ev-range-range-ai.service      |    7447   |  ev-range-seat.service          |
+---------------------------------+           +---------------------------------+
```

Three roles on each VM:

- **ECUs** — `bms.py` (VM1), `hvac_ecu.py` + `seat_ecu.py` (VM2). They
  receive dashboard slider/toggle samples over Zenoh and write them into
  their **local** Kuksa Databroker. None of them need to know that the
  other VM exists.
- **VM2 → VM1 bridge** — `zenoh_publisher.py` (VM2) subscribes to VM2's
  Kuksa for the cabin signals and forwards them over Zenoh to
  `zenoh_client.py` (VM1), which writes them into VM1's Kuksa. Battery
  signals don't need this bridge — `bms.py` is on VM1 already.
- **Range Compute AI** — `range_ai.py` (VM1) subscribes to battery + fan
  + seat signals on VM1's Kuksa, computes
  `Vehicle.Powertrain.Range`, and publishes it back into VM1's Kuksa.

All cross-process communication uses **only Eclipse Zenoh** (host↔ECU
and VM2↔VM1) plus the **local Kuksa Databroker** on each VM. No SOME/IP,
no gRPC, no manual `scp`.

---

## What's in this folder

| Path | Purpose |
|---|---|
| `setup.sh` | Downloads the Ubuntu cloud image, **runs `tools/compose_userdata.py` to embed all `ev-range-extender/` Python apps + their systemd units into the cloud-init seed**, builds two qcow2 disks + cloud-init seeds, brings up `br0` / `tap1` / `tap2`, launches both VMs, polls VM1's Databroker port until healthy. |
| `vm1_launch.sh` / `vm2_launch.sh` | QEMU invocations for each VM (called by `setup.sh`). |
| `input/user-data-vm1` | Cloud-init **template** for VM1: installs `docker.io`, Python deps (`kuksa-client`, `eclipse-zenoh`, …), runs `evrange-start-runtime` to bring up `ghcr.io/eclipse-autowrx/sdv-runtime:latest` with `RUNTIME_NAME=ev-range`. The composer step appends auto-deploy entries; the merged file lands at `output/user-data-vm1.composed`. |
| `input/user-data-vm2` | Same idea for VM2 (`RUNTIME_NAME=ev-range-cabin`) so VM2's Databroker boots with the standard COVESA VSS catalog already loaded. |
| `input/network-vm1.yaml` / `input/network-vm2.yaml` | Static IP for the bridge NIC; DHCP for the SLIRP NIC (outbound internet). |
| **`ev-range-extender/`** | **The demo apps** (auto-deployed onto the VMs). See the layout below. |
| **`hardware-sim/`** | **Host-side PyTk dashboard** that publishes slider / toggle values to the three ECUs over Zenoh. The only piece the developer starts by hand. |
| **`tools/compose_userdata.py`** | Build-time helper that injects every Python file under `ev-range-extender/` plus six systemd unit files into a copy of `input/user-data-vm{1,2}`, producing the `output/user-data-vm{1,2}.composed` files used by `cloud-localds`. Re-runs on every `./setup.sh`. |
| `zenoh-demo/` | A minimal, self-contained Zenoh pub/sub example (independent of the EV demo). Useful as a connectivity smoke-test on `tcp/7447`. |
| `output/` | Generated qcow2 disks, seed images, composed cloud-config, base Ubuntu image (gitignored). |

### `ev-range-extender/` layout

```
ev-range-extender/                  (auto-deployed to /home/ubuntu/ev-range-extender on each VM)
├── vm1/
│   ├── README.md            # full VM1 docs
│   ├── bms.py               # Battery Monitoring System  (auto-start: ev-range-bms.service)
│   ├── zenoh_client.py      # VM2 -> VM1 Zenoh subscriber (auto-start: ev-range-zenoh-client.service)
│   └── range_ai.py          # Range Compute AI            (auto-start: ev-range-range-ai.service)
└── vm2/
    ├── README.md            # full VM2 docs
    ├── hvac_ecu.py          # HVAC ECU                    (auto-start: ev-range-hvac.service)
    ├── seat_ecu.py          # Seat Control Module         (auto-start: ev-range-seat.service)
    └── zenoh_publisher.py   # VM2 -> VM1 Zenoh publisher  (auto-start: ev-range-zenoh-publisher.service)
```

Every file shown above is dropped onto the matching VM by cloud-init
on first boot and started by its own systemd unit. There is no
manual `python3 …` step on the VMs — ever.

### `hardware-sim/` layout

```
hardware-sim/                  (runs on the host, NOT inside the VMs)
├── README.md
├── requirements.txt          # eclipse-zenoh
└── pytk_dashboard.py         # Tk GUI: 3 sliders + 1 fan slider + 2 toggles, Zenoh peer publisher
```

The two demo READMEs (`ev-range-extender/vm1/README.md`,
`ev-range-extender/vm2/README.md`) contain the deep dive on VSS path
choices, the range model, and the cabin-load formula.

---

## IP map and ports

| Host / VM | IP | Listening ports |
|---|---|---|
| WSL host | 192.168.100.1 (`br0`) | — (the PyTk dashboard is an outbound Zenoh peer; no listening port) |
| VM1 | 192.168.100.10 (`ens3`) | 22 (ssh), 55555 (`ev-range` Kuksa), **tcp/7460** (`bms.py`, host → VM1), **tcp/7447** (`zenoh_client.py`, VM2 → VM1) |
| VM2 | 192.168.100.11 (`ens3`) | 22 (ssh), 55555 (`ev-range-cabin` Kuksa), **tcp/7461** (`hvac_ecu.py`, host → VM2), **tcp/7462** (`seat_ecu.py`, host → VM2) |

There are two independent Zenoh layers in this lab — both use
peer-to-peer TCP with a tiny JSON envelope `{"value": …, "source": …, "ts": …}`:

- **Host → ECU** (`tcp/7460`, `tcp/7461`, `tcp/7462`): the PyTk
  dashboard dials each ECU and publishes slider / toggle values; the
  BMS / HVAC / Seat ECUs subscribe and write into their *local* Kuksa
  Databroker.
- **VM2 → VM1 cross-VM bridge** (`tcp/7447`): VM2's
  `zenoh_publisher.py` connects out to `zenoh_client.py` on VM1 and
  forwards every cabin update.

Both VMs also have a SLIRP NIC (`ens4`, 10.0.2.15/24) for **outbound**
internet (apt, pip, docker pull). SLIRP does not carry IPv6 — see
"Known issues" below for why this matters and how the templates work
around it.

---

## Quick start (~10 min on a fresh host) — fully self-contained

| Step | What it does |
|---|---|
| 1 | Host prep (cleanup, install qemu, Tk, Zenoh, iptables, ip_forward) |
| 2 | Provision and boot both VMs (`./setup.sh` + `./vm2_launch.sh`) — also auto-deploys all 6 services |
| 3 | Verify all 6 systemd services are `active` on the VMs |
| 4 | Launch the host **PyTk hardware-simulator dashboard** and run the demo |
| 5 | Shut down |

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
# Zenoh links AND the VM2 -> VM1 cross-VM Zenoh bridge.
sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT
sudo sysctl -w net.ipv4.ip_forward=1
```

### Step 2 — Provision and boot both VMs (with auto-deployed apps)

Run from inside the `qemu-image-creator/` folder of your local clone:

```bash
cd path/to/eclipse-sdv-blueprint/qemu-image-creator
chmod +x *.sh

# Wipe per-VM state if you have changed anything under ev-range-extender/
# or tools/compose_userdata.py - cloud-init re-runs on the regenerated
# qcow2 + seed and picks up the new units. (Skip these `rm` lines on the
# very first run.)
sudo rm -f output/vm1.qcow2 output/vm2.qcow2 \
            output/seed1.img output/seed2.img \
            output/user-data-vm1.composed output/user-data-vm2.composed

./setup.sh           # launches VM1 in this terminal; blocks until VM1 Kuksa is up
```

In a second host terminal:

```bash
cd path/to/eclipse-sdv-blueprint/qemu-image-creator
./vm2_launch.sh      # launches VM2
```

What happens, in order:

1. `tools/compose_userdata.py` reads the templates `input/user-data-vm{1,2}`
   and embeds every Python file under `ev-range-extender/` plus the **six**
   systemd unit files (`ev-range-bms.service`,
   `ev-range-zenoh-client.service`, `ev-range-range-ai.service` on VM1
   and `ev-range-hvac.service`, `ev-range-seat.service`,
   `ev-range-zenoh-publisher.service` on VM2) into
   `output/user-data-vm{1,2}.composed`.
2. `cloud-localds` packs that composed file into the seed image.
3. `setup.sh` downloads `noble-server-cloudimg-amd64.img` on first run
   (~600 MB, ~3 min), builds the qcow2 disks, brings up the bridge,
   boots both VMs, then polls VM1's Databroker port until it answers.
4. On first boot, cloud-init writes the `ev-range-extender/` tree under
   `/home/ubuntu/`, drops the systemd unit files under
   `/etc/systemd/system/`, runs `daemon-reload + enable + start
   --no-block` for each unit, and they wait inside `ExecStartPre` until
   pip + the SDV Runtime container are ready.

> **First-boot timing:** the SDV Runtime image is ~250 MB, and pip
> needs to fetch `kuksa-client` + `eclipse-zenoh`. On a slow link this
> can take 2-5 min before the services flip to `active`. Each unit
> tolerates that out of the box (10-min wait for `:55555`, 5-min wait
> for the Python imports, `StartLimitIntervalSec=0` so it never gives
> up). Subsequent boots flip everything to `active` in ~10-30 s.

### Step 3 — Verify all 6 services are auto-running

Three on VM1, three on VM2. None of these are started manually — they
are pure cloud-init + systemd output:

```bash
ssh ubuntu@192.168.100.10 'systemctl is-active ev-range-bms.service \
                                                ev-range-zenoh-client.service \
                                                ev-range-range-ai.service'
ssh ubuntu@192.168.100.11 'systemctl is-active ev-range-hvac.service \
                                                ev-range-seat.service \
                                                ev-range-zenoh-publisher.service'
# expected: 6 lines saying "active"
```

Sanity-check the listening sockets on both VMs:

```bash
ssh ubuntu@192.168.100.10 'ss -ltn | grep -E ":55555|:7447|:7460"'
ssh ubuntu@192.168.100.11 'ss -ltn | grep -E ":55555|:7461|:7462"'
# VM1 expected: 55555, 7447, 7460
# VM2 expected: 55555, 7461, 7462
```

If any service is `inactive` / `failed`, dump its journal:

```bash
ssh ubuntu@192.168.100.10 'sudo journalctl -u ev-range-bms.service -n 60 --no-pager'
ssh ubuntu@192.168.100.10 'sudo journalctl -u ev-range-range-ai.service -n 60 --no-pager'
ssh ubuntu@192.168.100.11 'sudo journalctl -u ev-range-zenoh-publisher.service -n 60 --no-pager'
```

Each unit also writes its stdout/stderr to `/tmp/ev-range-*.log` (root-owned,
mode 0644 so plain `tail -f` works without sudo):

```bash
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-bms.log \
                                    /tmp/ev-range-zenoh-client.log \
                                    /tmp/ev-range-range-ai.log'
ssh ubuntu@192.168.100.11 'tail -f /tmp/ev-range-hvac.log \
                                    /tmp/ev-range-seat.log \
                                    /tmp/ev-range-zenoh-publisher.log'
```

### Step 4 — Launch the host PyTk dashboard and demo

The dashboard is the **only** piece the presenter starts by hand. Open
one host terminal:

```bash
cd path/to/eclipse-sdv-blueprint/qemu-image-creator
python3 hardware-sim/pytk_dashboard.py
# expected on stdout:
#   [pytk] dialing Zenoh endpoints: ['tcp/192.168.100.10:7460',
#                                    'tcp/192.168.100.11:7461',
#                                    'tcp/192.168.100.11:7462']
```

The Tk window appears with three labelled sections:

| Section | Control | Range / Behaviour | VSS path written by the matching ECU |
|---|---|---|---|
| Battery (VM1 — `bms.py`) | **Battery Voltage** slider | 320 – 420 V (default 400 V) | `Vehicle.Powertrain.TractionBattery.CurrentVoltage` |
| Battery | **Battery Current** slider | 0 – 200 A (default 25 A) — non-negative only | `Vehicle.Powertrain.TractionBattery.CurrentCurrent` |
| Battery | **Battery %** slider | 0 – 100 % (default 80 %) | `Vehicle.Powertrain.TractionBattery.StateOfCharge.Current` |
| Cabin HVAC (VM2 — `hvac_ecu.py`) | **Fan Speed** slider | 0 – 100 % (default 0) | `Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed` |
| Cabin Seat (VM2 — `seat_ecu.py`) | **Seat Heating** toggle | OFF / ON; ON publishes 100, OFF publishes 0 | `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` |
| Cabin Seat | **Seat Cooling** toggle | OFF / ON; ON publishes -100, OFF publishes 0; **mutually exclusive with Seat Heating** | `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` |

Move any slider once. The status bar shows the publish:
`[10:18:31]  PUT sim/battery/soc = 75 %`. In another terminal you can
confirm the matching ECU reacted:

```bash
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-bms.log'
# expected: [bms] OK Vehicle.Powertrain.TractionBattery.StateOfCharge.Current = 75.0 (from <host>)
```

#### Demo scenario (slider moves on the host)

All inputs come from the dashboard. Watch the `range_ai` log for the
recomputed `Range`:

```bash
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-range-ai.log'
```

##### Phase 1 — cold start, fully charged

| Control | Set to |
|---|---|
| Battery Voltage | 420 V |
| Battery Current | 25 A |
| Battery % | 100 % |
| Fan Speed | 0 % |
| Seat Heating | OFF |
| Seat Cooling | OFF |

Pack power = 25 A × 420 V = 10.5 kW (below the 18 kW cruise threshold,
so no acceleration penalty).
`range_ai` log → `Range = 417 km`.

##### Phase 2 — normal cruising drains the battery

Move Battery % from `75` → `50` → `25`.
`range_ai` log → `312 km` → `208 km` → `104 km`.

##### Phase 3 — hard acceleration (current spikes)

First bring Battery % back to `50` so the effect of the load factor is
easy to see. Then set Battery Current to `90` A. Pack power becomes
90 × 420 = 37.8 kW (~2.1× the 18 kW cruise threshold), so traction
consumption scales by 2.1×.
`range_ai` log → ~`99 km` (at SoC 50 %).

##### Phase 4 — voltage sag under load

Leave Battery % at `50`, leave Battery Current at `90` A. Drop Battery
Voltage to `360` V (typical pack droop under heavy discharge).
Pack power becomes 90 × 360 = 32.4 kW (~1.8× cruise), so the load
factor relaxes a touch.
`range_ai` log → ~`116 km` (at SoC 50 %, current 90 A, voltage 360 V).

Reset Battery Current back to `25` A and Voltage to `400` V before
moving on. SoC stays at `50 %`.

##### Phase 5 — cabin HVAC fan

Move Fan Speed: `0` → `50` → `100` %. With the battery now back at
cruise (10 kW pack power), the HVAC compressor + blower load is the
only thing affecting consumption.
`range_ai` log → `208 km` (Fan 0) → ~`191 km` (Fan 50, +1 kW) →
~`176 km` (Fan 100, +2 kW).

Bring Fan Speed back to `0` before the next phase.

##### Phase 6 — seat heating / cooling toggles (mutually exclusive)

Continuing from end of Phase 5 (SoC 50 %, all other inputs at the quiet
baseline), the baseline range is `208 km`.

| Click | Result on dashboard | What you'll see in `range_ai` |
|---|---|---|
| Tick **Seat Heating** | Heating ON (100); Cooling auto-OFF (0) — `seat/heating = 100` and `seat/hc = 0` are both published | Range drops to ~`176 km` (added ~2 kW driver-zone heater load) |
| Tick **Seat Cooling** | Cooling ON (-100); Heating auto-clears (0) — `seat/hc = -100` and `seat/heating = 0` are both published | Range rises to ~`199 km` vs. heating on (added ~0.5 kW vent load instead of 2 kW heat) |
| Untick the lit toggle | Both OFF (0, 0) | Range returns to the `208 km` baseline |

The dashboard's status bar reads, for example:
`[10:42:11]  PUT sim/cabin/seat/heating = ON (100)  | auto-OFF: Seat Cooling`.

##### Reset to a quiet baseline (full pack, no penalties, no cabin load)

| Control | Set to |
|---|---|
| Battery Voltage | 400 V |
| Battery Current | 25 A |
| Battery % | 80 % |
| Fan Speed | 0 % |
| Seat Heating | OFF |
| Seat Cooling | OFF |

`range_ai` log → ~`333 km`.

The deep dive — VSS path choices, the range formulas, the cabin-load
breakdown — lives in
[`ev-range-extender/vm1/README.md`](ev-range-extender/vm1/README.md)
and [`ev-range-extender/vm2/README.md`](ev-range-extender/vm2/README.md).

### Step 5 — Shutdown

```bash
# Close the dashboard (Ctrl-C in its terminal or click the X).

# Power both VMs off cleanly
ssh ubuntu@192.168.100.10 'sudo poweroff'
ssh ubuntu@192.168.100.11 'sudo poweroff'

# Tear down host-side networking and any orphan QEMU
sudo pkill -9 -f 'qemu-system-x86_64' 2>/dev/null
sudo ip link delete tap1 2>/dev/null
sudo ip link delete tap2 2>/dev/null
sudo ip link delete br0  2>/dev/null
```

Re-running `./setup.sh` later (with the existing qcow2 images intact)
re-uses the VM state — you only pay the docker-pull / pip-install cost
once. All 6 services come back to `active` in ~10-30 s.

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
    kuksa-client eclipse-zenoh

# Then re-run the runtime helper to bring up the broker
sudo /usr/local/bin/evrange-start-runtime         # VM1
# or
sudo /usr/local/bin/evrange-start-databroker      # VM2

# Finally, restart the auto-start services so they pick up the new deps
sudo systemctl restart ev-range-*.service
```

After that, confirm:
```bash
docker --version && docker ps && ss -ltn | grep 55555
```

The cloud-init files have an `apt: Acquire::ForceIPv4` block plus a
retry-with-IPv4 pip wrapper to do this automatically on the **next**
fresh boot. VMs provisioned **before** that change need the manual
recovery above one time.

### B. `qemu-system-x86_64: ... could not configure /dev/net/tun (tap1): Device or resource busy`

**Cause**: A previous QEMU process is still alive holding the tap.
**Fix**: see Step 1 of the Quick start (`pkill -9 qemu-system-x86_64`
+ delete the tap interfaces).

### C. VM2 Kuksa says `not_found` for the cabin signals

**Cause**: VM2 is running the **bare** `kuksa-databroker:main` image
(no VSS preloaded) instead of the `sdv-runtime:latest` image with the
standard COVESA catalog. This happens on VMs provisioned before the
cloud-init image swap.

**Fix**: One-time on VM2:
```bash
ssh ubuntu@192.168.100.11
sudo docker rm -f kuksa-databroker
sudo docker pull ghcr.io/eclipse-autowrx/sdv-runtime:latest
sudo docker run -d --name kuksa-databroker --restart unless-stopped --network host \
    -e RUNTIME_NAME=ev-range-cabin \
    ghcr.io/eclipse-autowrx/sdv-runtime:latest
sudo systemctl restart ev-range-hvac.service ev-range-seat.service ev-range-zenoh-publisher.service
```

### D. Dashboard slider/toggle does not move the range

In order, check:

1. **The matching ECU service is up.**
   ```bash
   ssh ubuntu@192.168.100.10 'systemctl is-active ev-range-bms.service'
   ssh ubuntu@192.168.100.11 'systemctl is-active ev-range-hvac.service ev-range-seat.service'
   ```
   If any is `failed`, check `journalctl -u <unit>` and `/tmp/ev-range-<name>.log`.

2. **The cross-VM bridge is up** (only matters for cabin signals).
   ```bash
   ssh ubuntu@192.168.100.11 'systemctl is-active ev-range-zenoh-publisher.service'
   ssh ubuntu@192.168.100.10 'systemctl is-active ev-range-zenoh-client.service'
   ```
   Both must be `active`. From VM2: `nc -zv 192.168.100.10 7447` should
   succeed.

3. **`range_ai.service` is up** on VM1.
   ```bash
   ssh ubuntu@192.168.100.10 'systemctl is-active ev-range-range-ai.service'
   ```

4. **Host bridge forwarding rule is in place** (Step 1):
   ```bash
   sudo iptables -L FORWARD -n -v | grep -E 'br0.*br0'
   ```
   If missing: `sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT`.

5. **Wrong VM IPs / ports passed to `pytk_dashboard.py`**. Check the
   first line it prints:
   `[pytk] dialing Zenoh endpoints: [...]`. Override with `--vm1 / --vm2 /
   --bms-port / --hvac-port / --seat-port` if needed.

6. **`python3-tk` not installed on the host**:
   `sudo apt install -y python3-tk`.

7. **`eclipse-zenoh` Python binding missing on the host**:
   `pip install --user --break-system-packages eclipse-zenoh`.

### E. Cursor invisible / dashboard window opens behind the terminal (WSLg)

Both are fixed in code (`pytk_dashboard.py` pins `cursor="left_ptr"`
and lifts itself with `attributes("-topmost", True)`), but if you still
see them on a stale WSLg session:

```bash
# In Windows PowerShell:
wsl --shutdown
# Then re-open WSL and retry. Optionally install a real cursor theme:
sudo apt install -y xcursor-themes adwaita-icon-theme
echo 'export XCURSOR_THEME=Adwaita' >> ~/.bashrc
echo 'export XCURSOR_SIZE=24'        >> ~/.bashrc
source ~/.bashrc
```

---

## Side demo (not required for EV Range Extender)

- **`zenoh-demo/`** — bare Zenoh pub/sub. VM1 runs `pub.py`, VM2 runs
  `sub.py`. Useful as a connectivity check on `tcp/7447` if the
  cross-VM bridge is not delivering samples.

---

## Cleaning the host

To wipe everything and re-download the Ubuntu image on the next run:

```bash
sudo rm -rf output
```

To wipe only the per-VM state but keep the cached Ubuntu image (most
common when iterating on `ev-range-extender/` source or
`tools/compose_userdata.py`):

```bash
sudo rm -f output/vm{1,2}.qcow2 output/seed{1,2}.img \
            output/user-data-vm{1,2}.composed
```

---

## Credits

Built on top of the [eclipse-sdv-blueprint](../README.md) EV Range
Extender use case. Uses
[`ghcr.io/eclipse-autowrx/sdv-runtime`](https://github.com/eclipse-autowrx/sdv-runtime)
(packaged Kuksa Databroker + standard COVESA VSS catalog) and
[Eclipse Zenoh](https://zenoh.io/) (peer-to-peer pub/sub) for both the
host↔ECU input layer and the VM2↔VM1 cross-VM bridge.
