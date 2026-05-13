# QEMU Multi-VM SDV Lab — EV Range Extender

A zero-touch multi-VM lab built on **QEMU + KVM + cloud-init** that runs the
**EV Range Extender** end-to-end demo. Two Ubuntu 24.04 ("noble") VMs are
provisioned automatically, joined to a private Layer-2 bridge, and each
runs its own `digital.auto sdv-runtime` Kuksa Databroker.

A **PyTk Hardware-Simulator dashboard** running on the host pushes signal
values into per-VM ECUs (BMS, HVAC, Seat) over **Eclipse Zenoh**. A
Range-Compute AI on VM1 consumes those signals and publishes
`Vehicle.Powertrain.Range` back into the same Databroker.

> Reference prototype on the digital.auto playground:
> [ev-range prototype](https://playground.digital.auto/model/67f76c0d8c609a0027662a69/library/prototype/69ce30f438bb8e98f0af5ac8/dashboard)

The whole stack is **auto-deployed** — every Python app under
`ev-range-extender/` is embedded into the cloud-init seed and lands on
the matching VM at first boot, started by systemd. The developer only
runs **two** things: `setup.py` (or `setup.sh`) on the host, then
`pytk_dashboard.py` in a second terminal.

---

## Architecture at a glance

```
WSL / Linux host (192.168.100.1 on br0)
  +---------------------------------------+
  |  hardware-sim/pytk_dashboard.py       |
  |    Battery V / A / %                  |
  |    Fan Speed                          |
  |    Seat Heating + Seat Cooling toggles|
  +---------------------------------------+
       |                |                 |
       | tcp/7460       | tcp/7461        | tcp/7462    (Zenoh, JSON payloads)
       v                v                 v
  br0  +-- tap1 ------------------+   +-- tap2 -----------------------+
       | VM1  192.168.100.10 (HPC)|   | VM2  192.168.100.11 (Zonal)   |
       | sdv-runtime (Kuksa :55555)|  | sdv-runtime (Kuksa :55555)    |
       |                          |   |                               |
       | bms.service              |   | hvac.service                  |
       |   sub sim/battery/**     |   |   sub sim/cabin/temp          |
       |   -> Vehicle.Powertrain. |   |   -> Vehicle.Cabin.HVAC.      |
       |      TractionBattery.{V,A,SoC}|     AmbientAirTemperature    |
       |                          |   |                               |
       | range-ai.service         |   | seat.service                  |
       |   <- Kuksa (battery+cabin)|  |   sub sim/cabin/seat/**       |
       |   -> Vehicle.Powertrain. |   |   -> Vehicle.Cabin.Seat.Row1. |
       |      Range               |   |      DriverSide.{Heating,HC}  |
       |                          |   |                               |
       | zenoh-client.service  <--+--<+ zenoh-publisher.service       |
       |   tcp/7447 listener      |   |   forwards VM2 cabin signals  |
       +--------------------------+   +-------------------------------+
```

**Six systemd services run automatically on every boot — three per VM.**

| VM1 (HPC, 192.168.100.10) | VM2 (Zonal, 192.168.100.11) |
|---|---|
| `ev-range-bms.service` | `ev-range-hvac.service` |
| `ev-range-range-ai.service` | `ev-range-seat.service` |
| `ev-range-zenoh-client.service` | `ev-range-zenoh-publisher.service` |

You never log into the VMs to start anything.

---

## What's in this folder

| Path | Purpose |
|---|---|
| `setup.py` / `setup.sh` | One-shot host provisioning. Downloads the Ubuntu cloud image, runs `tools/compose_userdata.py`, builds qcow2 disks + cloud-init seeds, brings up `br0`/`tap1`/`tap2`, launches both VMs, waits until VM1's Kuksa answers on `:55555`. Both files are equivalent — pick whichever you prefer. |
| `requirements.txt` | Host-side Python deps: **PyYAML** (used by the composer) + **eclipse-zenoh** (used by the dashboard). VM-side deps are installed by cloud-init on first boot, not from here. |
| `vm1_launch.sh` / `vm2_launch.sh` | QEMU invocations called by `setup.py` / `setup.sh`. |
| `tools/compose_userdata.py` | Build-time helper that injects every Python file under `ev-range-extender/` plus six systemd unit files into copies of the cloud-init templates. Outputs `output/user-data-vm{1,2}.composed`. |
| `input/user-data-vm1`, `input/user-data-vm2` | Cloud-init **templates**: install docker + Python deps, start the SDV Runtime container with the standard COVESA VSS catalog. |
| `input/network-vm1.yaml`, `input/network-vm2.yaml` | Static IP for the bridge NIC; DHCP for the SLIRP NIC (outbound internet). |
| `ev-range-extender/vm1/` | `bms.py`, `range_ai.py`, `zenoh_client.py` (auto-deployed to VM1). |
| `ev-range-extender/vm2/` | `hvac_ecu.py`, `seat_ecu.py`, `zenoh_publisher.py` (auto-deployed to VM2). |
| `hardware-sim/pytk_dashboard.py` | The host-side Tk GUI you interact with during the demo. |
| `zenoh-demo/` | A bare Zenoh pub/sub example (independent of the EV demo). |
| `output/` | Generated qcow2 disks, seed images, composed cloud-init, base Ubuntu image. Gitignored. |

The two deep-dive READMEs live next to the source:
[`ev-range-extender/vm1/README.md`](ev-range-extender/vm1/README.md) and
[`ev-range-extender/vm2/README.md`](ev-range-extender/vm2/README.md).

---

## Quick start

You only need two terminals on the host. **Steps 1-3 are one-time per
clone.** After that, every demo run is just steps 4 + 5.

### Step 1 — Install host packages

```bash
sudo apt update
sudo apt install -y \
    qemu-system qemu-utils cloud-image-utils wget bridge-utils \
    python3 python3-tk python3-venv
```

### Step 2 — Create a virtualenv and install Python deps

```bash
cd path/to/eclipse-sdv-blueprint/qemu-image-creator
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

> If you prefer **not** to use a virtualenv:
> `python3 -m pip install --user --break-system-packages -r requirements.txt`.

### Step 3 — Confirm KVM is usable (one check, no install)

```bash
ls -l /dev/kvm           # should exist
groups | grep -w kvm     # your user should be in the kvm group
```

If `/dev/kvm` is missing or you're not in the `kvm` group, `setup.py`
still works but the VMs will be very slow.

### Step 4 — Provision and launch both VMs (Terminal 1)

```bash
cd path/to/eclipse-sdv-blueprint/qemu-image-creator
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 setup.py        # OR ./setup.sh — same behaviour
```

What happens, in order:

1. The composer embeds the entire `ev-range-extender/` tree and **six**
   systemd unit files into `output/user-data-vm{1,2}.composed`.
2. `cloud-localds` packs each composed file into a seed image.
3. The Ubuntu cloud image is downloaded on first run (~600 MB, ~3 min).
4. `br0` + `tap1` + `tap2` are created; `iptables` and `ip_forward` are set.
5. Both VMs are launched in the background.
6. The script polls `192.168.100.10:55555` until VM1's Kuksa answers.

When the script prints `[SUCCESS] VM1 setup completed and runtime
created!`, every ECU service is already running on its VM.

> **First boot** takes ~5 min total (VM1 needs to `pip install` and
> `docker pull` ~250 MB before its Kuksa starts answering). Every later
> run starts in well under a minute.

### Step 5 — Launch the dashboard (Terminal 2)

`hardware-sim/` lives next to `qemu-image-creator/` at the repo root,
so reuse the same virtualenv created in Step 2 (the one in
`qemu-image-creator/.venv`). [`requirements.txt`](requirements.txt)
already covers both `setup.py` (PyYAML) and the dashboard
(`eclipse-zenoh`); the system `python3-tk` package was installed via
`apt` in Step 1.

```bash
cd path/to/eclipse-sdv-blueprint/qemu-image-creator
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cd ../hardware-sim
python3 pytk_dashboard.py
```

A Tk window opens with three sections:

| Section | Controls | Drives VSS path |
|---|---|---|
| **Battery (VM1 — bms.py)** | Battery Voltage (320–420 V) | `Vehicle.Powertrain.TractionBattery.CurrentVoltage` |
| | Battery Current (0–200 A) | `Vehicle.Powertrain.TractionBattery.CurrentCurrent` |
| | Battery % (0–100) | `Vehicle.Powertrain.TractionBattery.StateOfCharge.Current` |
| **Cabin HVAC (VM2 — hvac_ecu.py)** | Fan Speed (0–100) | `Vehicle.Cabin.HVAC.AmbientAirTemperature` (the slider rides on this VSS path; `range_ai.py` interprets the value as fan-speed percent) |
| **Cabin Seat (VM2 — seat_ecu.py)** | Seat Heating toggle | `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` (0 / 100) |
| | Seat Cooling toggle | `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` (0 / -100) |

Heating and Cooling are mutually exclusive — flipping one **on**
automatically forces the other **off**.

That's the whole runtime stack. Move sliders / toggles, watch
`Vehicle.Powertrain.Range` change. Numbers update every time a signal
changes.

---

## Showing the demo

The simplest narrative is a 6-step tour. Inputs are all on the
dashboard; the only output you need to watch is the recomputed range.

| # | What you do on the dashboard | Why it matters |
|---|---|---|
| **1** | Leave defaults: V=380, I=30, Battery%=80, Fan=0, both seat toggles off | Baseline range. Cruise mode (current is below the acceleration threshold), no cabin load. |
| **2** | Drag Battery % from 80 → 50 → 25 | Range scales linearly with state-of-charge. The most direct demonstration. |
| **3** | Drag Battery Current from 30 → 100 → 200 A | Above ~48 A the model engages a hard-acceleration penalty (`load_factor = power / 18 kW`); Range drops sharply. |
| **4** | Drag Fan Speed from 0 → 50 → 100 | The HVAC station load grows linearly: 0 kW at fan=0, 2 kW at fan=100. Range drops as the fan goes up. |
| **5** | Toggle **Seat Heating** on, then off, then **Seat Cooling** on | The seat heater pulls 2 kW; the cooler pulls 0.5 kW. Range moves accordingly. The mutex is visible — Heating switches off automatically when you turn Cooling on. |
| **6** | Reset to defaults | Range returns to baseline. |

To watch the recomputed range as it happens, in a third terminal:

```bash
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-range-ai.log'
```

The line you're looking for:

```
[range-ai] output : Vehicle.Powertrain.Range = 242 km (computed 242.4 km; SoC=80.0 %, ...)
```

The exact numbers depend on the slider positions; the math is in
[`vm1/range_ai.py`](ev-range-extender/vm1/range_ai.py) (`compute_range`).

---

## Useful commands

### Inspect / control the auto-started services on a VM

```bash
# All 3 services on VM1
ssh ubuntu@192.168.100.10 \
    'systemctl list-units --type=service --state=active "ev-range-*"'

# All 3 services on VM2
ssh ubuntu@192.168.100.11 \
    'systemctl list-units --type=service --state=active "ev-range-*"'

# Live logs (no sudo needed — log files are world-readable)
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-bms.log'
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-range-ai.log'
ssh ubuntu@192.168.100.10 'tail -f /tmp/ev-range-zenoh-client.log'

ssh ubuntu@192.168.100.11 'tail -f /tmp/ev-range-hvac.log'
ssh ubuntu@192.168.100.11 'tail -f /tmp/ev-range-seat.log'
ssh ubuntu@192.168.100.11 'tail -f /tmp/ev-range-zenoh-publisher.log'

# Restart one if it's misbehaving
ssh ubuntu@192.168.100.10 'sudo systemctl restart ev-range-bms'
```

Default password for the `ubuntu` user is `ubuntu`.

### Stop / wipe / restart

```bash
# Power both VMs off cleanly
ssh ubuntu@192.168.100.10 'sudo poweroff'
ssh ubuntu@192.168.100.11 'sudo poweroff'

# Tear down host-side networking + any orphan QEMU processes
sudo pkill -9 -f 'qemu-system-x86_64' 2>/dev/null
sudo ip link delete tap1 2>/dev/null
sudo ip link delete tap2 2>/dev/null
sudo ip link delete br0  2>/dev/null

# Wipe the VM disks but keep the cached Ubuntu image
rm -f output/vm{1,2}.qcow2 output/seed{1,2}.img

# Wipe everything (image is re-downloaded on next setup.py)
rm -rf output/*
```

---

## Troubleshooting

### A. `setup.py` hangs at "Waiting for SDV Runtime to download and launch..."

Cloud-init on VM1 is still pulling `ghcr.io/eclipse-autowrx/sdv-runtime`
or installing pip packages. On a clean first boot this can take 3-5 min.
If it goes longer than ~7 min, log into VM1 and check:

```bash
ssh ubuntu@192.168.100.10 'cloud-init status --long; tail -40 /tmp/evrange-runtime.log'
```

Common causes:

- The VM lost outbound DNS (SLIRP doesn't carry IPv6, glibc prefers
  IPv6). The cloud-init template forces IPv4 for apt/pip; if you see
  "Temporary failure in name resolution", retry once — apt is set to
  retry — or run `sudo apt -o Acquire::ForceIPv4=true update` manually.
- `ghcr.io` rate-limited the docker pull. Wait a minute and run
  `sudo /usr/local/bin/evrange-start-runtime` on VM1 to re-pull.

### B. `qemu-system-x86_64: ... could not configure /dev/net/tun (tap1): Device or resource busy`

A previous QEMU process is still alive holding the tap interface.

```bash
sudo pkill -9 -f 'qemu-system-x86_64' 2>/dev/null
sudo ip link delete tap1 2>/dev/null
sudo ip link delete tap2 2>/dev/null
sudo ip link delete br0  2>/dev/null
# then re-run setup.py
```

### C. A service shows `inactive` or `failed` after `setup.py`

Each unit waits up to 10 min for the local Kuksa Databroker to start
listening on `:55555` (`ExecStartPre`). If the broker eventually came
up but the unit had already exited, restart it once:

```bash
ssh ubuntu@192.168.100.10 'sudo systemctl restart ev-range-bms ev-range-range-ai ev-range-zenoh-client'
ssh ubuntu@192.168.100.11 'sudo systemctl restart ev-range-hvac ev-range-seat ev-range-zenoh-publisher'
```

If a service still won't start, dump the journal:

```bash
ssh ubuntu@192.168.100.10 'sudo journalctl -u ev-range-bms --no-pager -n 80'
```

### D. Dashboard slider moves but the Range value doesn't change

Check, in order:

1. The matching ECU is running:
   ```bash
   ssh ubuntu@192.168.100.10 'systemctl is-active ev-range-bms'
   ssh ubuntu@192.168.100.11 'systemctl is-active ev-range-hvac ev-range-seat'
   ```
2. The cross-VM bridge pair is running (cabin signals only):
   ```bash
   ssh ubuntu@192.168.100.10 'systemctl is-active ev-range-zenoh-client'
   ssh ubuntu@192.168.100.11 'systemctl is-active ev-range-zenoh-publisher'
   ```
3. `range_ai.py` is running:
   ```bash
   ssh ubuntu@192.168.100.10 'systemctl is-active ev-range-range-ai'
   ```
4. Reachability of the ECU TCP port from the host:
   ```bash
   nc -zv 192.168.100.10 7460   # bms
   nc -zv 192.168.100.11 7461   # hvac
   nc -zv 192.168.100.11 7462   # seat
   ```
5. The `iptables` rule that allows VM↔VM traffic on the bridge
   (`setup.py` adds it automatically — only needed if you reset firewall):
   ```bash
   sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT
   sudo sysctl -w net.ipv4.ip_forward=1
   ```

### E. Dashboard prints `ModuleNotFoundError: No module named 'zenoh'` (or `yaml`)

The host-side virtualenv isn't active or the deps aren't installed:

```bash
cd path/to/eclipse-sdv-blueprint/qemu-image-creator
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Outside a virtualenv use
`python3 -m pip install --user --break-system-packages -r requirements.txt`.

### F. Tk window shows up empty / cursor invisible (WSLg)

Already handled in code (forces `cursor=left_ptr`, lifts and focuses
the window on launch). If it still happens, click anywhere in the
window once and the controls will respond.

---

## Credits

Built on top of the
[eclipse-sdv-blueprint](../README.md) EV Range Extender use case. Uses:

- [`ghcr.io/eclipse-autowrx/sdv-runtime`](https://github.com/eclipse-autowrx/sdv-runtime) — Kuksa Databroker pre-loaded with the standard COVESA VSS catalog.
- [Eclipse Zenoh](https://zenoh.io/) — peer-to-peer pub/sub used for both host↔ECU and the VM2→VM1 cabin signal bridge.
- [kuksa-client](https://github.com/eclipse-kuksa/kuksa-python-sdk) — Python client for the Kuksa Databroker.
