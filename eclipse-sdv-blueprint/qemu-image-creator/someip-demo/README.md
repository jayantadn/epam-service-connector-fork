# SOME/IP demo: VM2 -> VM1

Minimal Eclipse-SCore-flavoured SOME/IP pub/sub demo running between
the two QEMU VMs that `setup.sh` / `vm2_launch.sh` provision. **VM2
offers** a tiny `Hello` service, **VM1 subscribes**.

> **Not the main project demo.** This folder is a lightweight SOME/IP
> connectivity smoke-test for `udp/30490` (SD multicast) +
> `udp/30509`/`udp/30510` (event traffic) between the two VMs. The
> production-style SOME/IP use (with VSS payloads bridged into Kuksa
> Databrokers on both sides) lives in
> [`../ev-range-extender/`](../ev-range-extender/) - see
> `ev-range-extender/vm2/someip_publisher.py` (publisher) and
> `ev-range-extender/vm1/someip_client.py` (subscriber).
>
> Sister demo: [`../zenoh-demo/`](../zenoh-demo/) - same VM-to-VM
> connectivity smoke-test using Eclipse Zenoh instead of SOME/IP.

## Topology

```
VM2 (192.168.100.11)                              VM1 (192.168.100.10)
+------------------------+   udp/30490 (SD)        +------------------------+
|  server.py             |  multicast 224.224.   ->|  client.py             |
|  SOME/IP server        |    224.245              |  SOME/IP client        |
|  service 0x1000        |   --------------------> |  subscribes to         |
|  eventgroup 0x0001     |   udp/30509 (events)    |  eventgroup 0x0001     |
|  event 0x8001          |   --------------------> |  prints payload        |
+------------------------+                         +------------------------+
```

- `server.py` opens a SOME/IP-SD listener on the bridge interface
  (`192.168.100.11`), offers the `Hello` service every 2 seconds and
  emits an ASCII payload (`Hello from vm2 #N at <ts>`) on event
  `0x8001` once per `--interval` (default 1 s).
- `client.py` joins the SD multicast on `192.168.100.10`, subscribes
  to eventgroup `0x0001` and prints each payload it receives.

No daemon and no extra container - someipy v1.x runs the whole stack
in-process. Wireshark on the host's `br0` decodes the packets natively
with the built-in `someip` dissector.

## Service contract

| Field            | Value     |
|------------------|-----------|
| Service ID       | `0x1000`  |
| Instance ID      | `0x0001`  |
| Major version    | `1`       |
| Eventgroup ID    | `0x0001`  |
| Event ID         | `0x8001`  |
| Payload          | UTF-8 ASCII string |
| SD multicast     | `udp/224.224.224.245:30490` |
| Server endpoint  | `udp/192.168.100.11:30509` |
| Client endpoint  | `udp/192.168.100.10:30510` |

## Prerequisites

`someipy` (>=1.0,<2.0) must be installed on both VMs. Cloud-init
already does this:

- VM1: `input/user-data-vm1` (the big `pip3 install ...` in `runcmd`)
- VM2: `input/user-data-vm2` (same idea)

Quick verification on each VM:

```bash
python3 -c "import someipy; print('someipy OK', someipy.__version__ if hasattr(someipy, '__version__') else 'unknown')"
```

If the import fails, reinstall manually:

```bash
sudo pip3 install --break-system-packages --ignore-installed 'someipy>=1.0,<2.0'
```

## Step-by-step run

### 1. Copy the demo onto both VMs (from the host)

```bash
cd /path/to/qemu-image-creator

# VM2 needs server.py
sshpass -p 'ubuntu' scp someip-demo/server.py ubuntu@192.168.100.11:/home/ubuntu/

# VM1 needs client.py
sshpass -p 'ubuntu' scp someip-demo/client.py ubuntu@192.168.100.10:/home/ubuntu/
```

### 2. Start the server on VM2

```bash
ssh ubuntu@192.168.100.11        # password: ubuntu
python3 server.py
```

Expected output:

```
[SRV] Starting SOME/IP-SD on 224.224.224.245:30490 via 192.168.100.11
[SRV] Offering service 0x1000 instance 0x0001 on 192.168.100.11:30509
[SRV] event 0x8001 (... B) -> 'Hello from vm2 #0 at 2026-...'
[SRV] event 0x8001 (... B) -> 'Hello from vm2 #1 at 2026-...'
```

Leave this terminal open.

### 3. Start the client on VM1

In a separate terminal on the host:

```bash
ssh ubuntu@192.168.100.10        # password: ubuntu
python3 client.py
```

Expected output on VM1:

```
[CLI] Starting SOME/IP-SD on 224.224.224.245:30490 via 192.168.100.10
[CLI] Subscribed to service 0x1000 eventgroup 0x0001 on 192.168.100.10:30510. Waiting for events...
[CLI] event 0x8001 (... B) -> 'Hello from vm2 #N at 2026-...'
[CLI] event 0x8001 (... B) -> 'Hello from vm2 #N+1 at 2026-...'
```

If you see those events flowing, SOME/IP is now carrying messages
between the two VMs end to end.

## Useful flags

`server.py`:

| Flag | Default | Purpose |
|------|---------|---------|
| `--interface-ip` | `192.168.100.11` | Bridge IP of VM2 (used as SD source + event source) |
| `--port` | `30509` | Outbound UDP port for events |
| `--interval` | `1.0` | Seconds between events |
| `--count` | `0` (forever) | Stop after N events |
| `--name` | hostname | Sender name embedded in payload |
| `--debug` | off | Verbose someipy logging |

`client.py`:

| Flag | Default | Purpose |
|------|---------|---------|
| `--interface-ip` | `192.168.100.10` | Bridge IP of VM1 (used as SD source + event sink) |
| `--port` | `30510` | UDP port to receive events on |
| `--debug` | off | Verbose someipy logging |

## Troubleshooting

**`client.py` never receives any event**

Most often the bridge is not passing UDP multicast. From the WSL host
(NOT inside a VM):

```bash
sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT
```

That single rule unblocks all VM<->VM traffic on `br0`, including the
SD multicast.

**`OSError: [Errno 99] Cannot assign requested address`**

The `--interface-ip` you passed isn't actually configured on the VM.
Verify with `ip -4 a` and use the IP shown for `ens3` (the bridge
NIC), e.g. `192.168.100.10` on VM1, `192.168.100.11` on VM2.

**Address already in use**

The previous run is still alive. Kill it:

```bash
pkill -f 'python3 server.py'
pkill -f 'python3 client.py'
```

Or pass `--port 30511` (server) / `--port 30512` (client) and try
again.

**Inspecting the wire**

From the host, on the bridge interface:

```bash
sudo tcpdump -ni br0 -X 'udp port 30490 or udp port 30509 or udp port 30510'
```

Or open Wireshark on `br0` and apply display filter `someip` - the
SOME/IP-SD `Offer` and `Subscribe` entries plus the notification
events all decode natively.

## End-to-end verification runbook

This folder is the smoke test. The order below is the canonical
"does the SOME/IP / Eclipse-SCore conversion actually work end-to-end"
checklist:

1. **Host prep** (once per host / per reboot) - see
   [`../README.md`](../README.md) "Quick start" steps 1-3 (cleanup
   stale tap interfaces, install host tools, enable
   `iptables FORWARD -i br0 -o br0 -j ACCEPT`, run `setup.sh`).

2. **Per-VM imports OK?** SSH each VM and run:

   ```bash
   python3 -c "from kuksa_client.grpc.aio import VSSClient; import someipy, zenoh; print('OK')"
   ```

   If `someipy` is missing, install on the affected VM:

   ```bash
   sudo pip3 install --break-system-packages --ignore-installed 'someipy>=1.0,<2.0'
   ```

3. **Smoke test** (this folder). Server on VM2, client on VM1 - see
   "Step-by-step run" above. You must see one event per second flow
   from VM2 to VM1 before moving on.

4. **EV Range Extender end-to-end** with all 7 phases. Full step-by-step
   (3 terminals on VM1 + 2 terminals on VM2, 7 phases, expected
   `Range = ... km` numbers) is in
   [`../ev-range-extender/vm1/README.md`](../ev-range-extender/vm1/README.md)
   "Step-by-step demo". Use `someip_publisher.py` and `someip_client.py`
   for the active SOME/IP path; the same runbook works with
   `zenoh_publisher.py` / `zenoh_client.py` if you want to compare
   against the legacy Zenoh transport.
