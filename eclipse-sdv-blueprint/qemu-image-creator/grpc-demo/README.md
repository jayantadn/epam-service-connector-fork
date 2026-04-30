# gRPC demo: VM1 -> VM2

Minimal Python gRPC demo running between the two QEMU VMs that
`setup.sh` / `vm2_launch.sh` provision. **VM1 is the client**,
**VM2 is the server**, mirroring the direction of the Zenoh demo.

## Topology

```
VM1 (192.168.100.10)                          VM2 (192.168.100.11)
+------------------------+    tcp/50051       +------------------------+
|  client.py             |  ----------------> |  server.py             |
|  gRPC client           |                    |  gRPC server           |
|  dials                 |                    |  listens on            |
|  192.168.100.11:50051  |                    |  0.0.0.0:50051         |
+------------------------+                    +------------------------+
```

## Service surface (`proto/demo.proto`)

Two RPCs to cover the most common gRPC patterns:

| RPC | Style | What it does |
|---|---|---|
| `SayHello(HelloRequest) -> HelloReply` | Unary (req/resp) | Server greets the client and returns its hostname |
| `StreamSignals(SignalRequest) -> stream SignalUpdate` | Server streaming | Server pushes N synthetic signal samples one at a time |

The streaming RPC is the closest gRPC analogue of the Zenoh pub/sub demo —
useful for comparing the two transports side by side.

## Files

```
grpc-demo/
├── proto/
│   └── demo.proto         # service definition
├── generate.sh            # regenerate Python stubs from demo.proto
├── server.py              # runs on VM2
├── client.py              # runs on VM1
└── README.md              # this file
```

`demo_pb2.py` and `demo_pb2_grpc.py` are *generated* by `generate.sh` from
the proto file and live next to `server.py` / `client.py` after that.

## Prerequisites

| VM | Need to install? | Command |
|---|---|---|
| **VM1** | No — cloud-init installs `grpcio` and `grpcio-tools` (`user-data-vm1` line 207). | — |
| **VM2** | No (on freshly provisioned VMs) — cloud-init now installs `grpcio` and `grpcio-tools` too (`user-data-vm2`). | — |

Quick verification on each VM:

```bash
python3 -c "import grpc; print('grpc OK', grpc.__version__)"
```

> If you have an **existing VM2** that was provisioned **before** this
> cloud-init change, install the packages once by hand:
>
> ```bash
> sudo pip3 install --break-system-packages --ignore-installed grpcio grpcio-tools
> ```

## Step-by-step run

### Step 1 — copy the demo to both VMs (from the host)

```bash
cd /path/to/qemu-image-creator

sshpass -p 'ubuntu' scp -r grpc-demo ubuntu@192.168.100.10:/home/ubuntu/   # VM1 (client)
sshpass -p 'ubuntu' scp -r grpc-demo ubuntu@192.168.100.11:/home/ubuntu/   # VM2 (server)
```

### Step 2 — generate the Python stubs on each VM (once)

`generate.sh` produces `demo_pb2.py` + `demo_pb2_grpc.py` from
`proto/demo.proto`. Run it on **each** VM (both have `grpcio-tools`
thanks to cloud-init):

```bash
ssh ubuntu@192.168.100.10 'cd /home/ubuntu/grpc-demo && ./generate.sh'
ssh ubuntu@192.168.100.11 'cd /home/ubuntu/grpc-demo && ./generate.sh'
```

> Tip: you can also generate once on VM1, then SCP `demo_pb2*.py` to VM2
> (`scp ubuntu@192.168.100.10:/home/ubuntu/grpc-demo/demo_pb2*.py
> ubuntu@192.168.100.11:/home/ubuntu/grpc-demo/`). Either way works.

### Step 3 — start the server on VM2

```bash
ssh ubuntu@192.168.100.11
cd /home/ubuntu/grpc-demo
python3 server.py
```

Expected output:

```
[server] gRPC server listening on 0.0.0.0:50051
[server] Press Ctrl+C to stop
```

Leave this terminal open.

### Step 4 — run the client on VM1

In a separate terminal on the host:

```bash
ssh ubuntu@192.168.100.10
cd /home/ubuntu/grpc-demo
python3 client.py
```

Expected client output:

```
[client] Connecting to 192.168.100.11:50051...
[client] -> SayHello(name='vm1')
[client] <- HelloReply(message='Hello, vm1! Greetings from VM2.', server_hostname='vm2')
[client] -> StreamSignals(signal='Vehicle.Speed', count=10, interval=1.0s)
[client] <- seq=  0 signal=Vehicle.Speed value=50.000 ts=2026-04-29T...
[client] <- seq=  1 signal=Vehicle.Speed value=53.281 ts=2026-04-29T...
...
[client] Done.
```

Expected server-side output (on VM2) for the same call:

```
[server] SayHello from ipv4:192.168.100.10:xxxxx: name='vm1'
[server] StreamSignals from ipv4:192.168.100.10:xxxxx: signal='Vehicle.Speed' count=10 interval=1.0s
[server]  -> seq=0 value=50.000
[server]  -> seq=1 value=53.281
...
[server] Stream complete (10 samples sent)
```

If you see those, gRPC is working end-to-end between the two VMs.

## Useful flags

`server.py`:

| Flag | Default | Purpose |
|---|---|---|
| `--bind` | `0.0.0.0:50051` | Address:port to listen on |
| `--workers` | `8` | Thread-pool size |

`client.py`:

| Flag | Default | Purpose |
|---|---|---|
| `--server` | `192.168.100.11:50051` | Where the server lives |
| `--name` | hostname | Name sent in `SayHello` |
| `--signal` | `Vehicle.Speed` | Signal name requested in the stream |
| `--count` | `10` | Number of streamed samples |
| `--interval` | `1.0` | Server-side delay between samples (s) |
| `--connect-timeout` | `5.0` | Channel-ready timeout (s) |
| `--skip-stream` | off | Only run the unary call |

## Quick comparison: gRPC vs Zenoh in this repo

| | `zenoh-demo/` | `grpc-demo/` |
|---|---|---|
| Pattern | Pub/sub (one-to-many, anonymous) | RPC (one-to-one, named methods) |
| Discovery | Endpoint config or multicast scouting | Static address:port |
| Schema | None — payloads are opaque bytes | `.proto` file, generated stubs |
| Codec | Whatever you choose (we used JSON) | Protobuf (binary) |
| Direction (in this demo) | VM1 ↦ VM2 | VM1 ↦ VM2 |
| Port | `tcp/7447` | `tcp/50051` |
| Best for | Telemetry, broadcast, late joiners | Commands, queries, structured request/response |

## Troubleshooting

**`ImportError: No module named demo_pb2`**

You haven't run `./generate.sh` on the machine where you're running
`server.py` / `client.py`. Run it once next to the scripts.

**`ImportError: No module named grpc` on VM2**

Cloud-init didn't install it. Fix:

```bash
sudo pip3 install --break-system-packages --ignore-installed grpcio
```

**Client times out connecting to `192.168.100.11:50051`**

```bash
# From VM1
nc -vz 192.168.100.11 50051
```

If that fails, the server isn't running, or WSL is dropping bridged traffic.
The repo's main `README.md` documents the WSL fix:

```bash
# On the WSL host (NOT inside a VM)
sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT
```

**`Address already in use` on VM2**

Something else is on `:50051` (e.g. another gRPC service). Either stop it
or pass a different port:

```bash
# VM2
python3 server.py --bind 0.0.0.0:50052

# VM1
python3 client.py --server 192.168.100.11:50052
```
