# Zenoh demo: VM1 -> VM2

Minimal Eclipse Zenoh pub/sub demo running between the two QEMU VMs that
`setup.sh` / `vm2_launch.sh` provision. **VM1 publishes**, **VM2 subscribes**.

## Topology

```
VM1 (192.168.100.10)                       VM2 (192.168.100.11)
+---------------------+    tcp/7447        +---------------------+
|  pub.py             |  ----------------> |  sub.py             |
|  zenoh publisher    |                    |  zenoh subscriber   |
|  connects to        |                    |  listens on         |
|  tcp/192.168.100.11 |                    |  tcp/0.0.0.0:7447   |
+---------------------+                    +---------------------+
```

- `sub.py` opens a Zenoh session that **listens** on `tcp/0.0.0.0:7447` and
  subscribes to the wildcard key `demo/vm/**`.
- `pub.py` opens a Zenoh session that **connects** to
  `tcp/192.168.100.11:7447` and publishes a JSON payload on
  `demo/vm/vm1` once per second.

No `zenohd` router is needed — the two Zenoh sessions peer directly over
the existing `br0` bridge.

## Prerequisites

`eclipse-zenoh` must be installed on both VMs. Cloud-init already does this:

- VM1: `input/user-data-vm1` (the big `pip3 install ...` in `runcmd`)
- VM2: `input/user-data-vm2` (the `pip3 install ... eclipse-zenoh ...` in `runcmd`)

Quick verification on each VM:

```bash
python3 -c "import zenoh; print('zenoh OK', zenoh.__version__)"
```

If the import fails, reinstall manually:

```bash
sudo pip3 install --break-system-packages --ignore-installed eclipse-zenoh
```

## Step-by-step run

### 1. Copy the demo onto both VMs (from the host)

```bash
cd /path/to/qemu-image-creator

# VM2 needs sub.py
sshpass -p 'ubuntu' scp zenoh-demo/sub.py ubuntu@192.168.100.11:/home/ubuntu/

# VM1 needs pub.py
sshpass -p 'ubuntu' scp zenoh-demo/pub.py ubuntu@192.168.100.10:/home/ubuntu/
```

### 2. Start the subscriber on VM2

```bash
ssh ubuntu@192.168.100.11        # password: ubuntu
python3 sub.py
```

Expected output:

```
[SUB] Opening Zenoh session, listening on 'tcp/0.0.0.0:7447'
[SUB] Subscribed to 'demo/vm/**'. Waiting for samples... (Ctrl+C to exit)
```

Leave this terminal open.

### 3. Start the publisher on VM1

In a separate terminal on the host:

```bash
ssh ubuntu@192.168.100.10        # password: ubuntu
python3 pub.py
```

Expected output on VM1:

```
[PUB] Opening Zenoh session, dialling 'tcp/192.168.100.11:7447'
[PUB] Publishing on 'demo/vm/vm1' every 1.0s
[PUB] key=demo/vm/vm1 msg={'from': 'vm1', 'index': 0, ...}
[PUB] key=demo/vm/vm1 msg={'from': 'vm1', 'index': 1, ...}
```

Expected output on VM2 (`sub.py`):

```
[SUB] key=demo/vm/vm1 msg={'from': 'vm1', 'index': 0, ...}
[SUB] key=demo/vm/vm1 msg={'from': 'vm1', 'index': 1, ...}
```

If you see those samples flowing, Zenoh is now carrying messages between
the two VMs end to end.

## Useful flags

`pub.py`:

| Flag | Default | Purpose |
|------|---------|---------|
| `--peer` | `tcp/192.168.100.11:7447` | Where to dial the subscriber |
| `--key` | `demo/vm/vm1` | Key expression to publish on |
| `--interval` | `1.0` | Seconds between publishes |
| `--count` | `0` (forever) | Stop after N messages |
| `--name` | hostname | Sender name embedded in payload |

`sub.py`:

| Flag | Default | Purpose |
|------|---------|---------|
| `--listen` | `tcp/0.0.0.0:7447` | Endpoint to listen on |
| `--key` | `demo/vm/**` | Key expression to subscribe to |

## Troubleshooting

**`pub.py` cannot reach VM2**

```bash
# From VM1
nc -vz 192.168.100.11 7447
```

If that fails, `sub.py` is not running (or not listening on `0.0.0.0:7447`),
or WSL is dropping bridged traffic. The repo's main `README.md` documents
the WSL fix:

```bash
# On the WSL host (NOT inside a VM)
sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT
```

**Port 7447 already in use on VM2**

The XMEL platform also listens on `7447`. If you have `xmel-platform`
running on VM2, stop it or pass `--listen tcp/0.0.0.0:7448` to `sub.py`
and `--peer tcp/192.168.100.11:7448` to `pub.py`.
