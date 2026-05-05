"""Zenoh publisher (runs on VM2).

Subscribes to VM2's local Kuksa Databroker for the cabin signals the
user drives via the Kuksa CLI on VM2, then republishes each update as
a JSON payload on Zenoh so VM1's `zenoh_client.py` can mirror it into
the ev-range Databroker that `range_ai.py` consumes.

End-to-end (no Python publisher anywhere - the Kuksa CLI is the only
source of truth for input signals):

    Kuksa CLI on VM2  --publish-->  VM2 Kuksa Databroker (127.0.0.1:55555)
                                            |
                                            | subscribe_current_values
                                            v
                                  zenoh_publisher.py (this file)
                                            |
                                            | zenoh.put(JSON)
                                            v   tcp/192.168.100.10:7447
                                  VM1 zenoh_client.py
                                            |
                                            v
                                  VM1 ev-range Kuksa Databroker
                                            |
                                            v
                                  range_ai.py (recomputes Range)

Bridged signals (each must exist in VM2's Kuksa VSS catalog - load the
EPAM JSON with `--metadata` when starting the broker, see vm2/README.md):

    Vehicle.Cabin.HVAC.AmbientAirTemperature        (sensor, float, celsius)
    Vehicle.Cabin.Seat.Row1.DriverSide.Heating      (actuator, int8, percent)
    Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
                                                    (actuator, int8, percent;
                                                     negative = cooling/ventilation,
                                                     positive = heating)

Prerequisites on VM2:
  * Kuksa Databroker running on 127.0.0.1:55555 with the EPAM/COVESA
    VSS catalog loaded (see ev-range-extender/vm2/README.md for the
    one-time `--metadata` setup).
  * eclipse-zenoh + kuksa-client Python packages.
"""

import argparse
import asyncio
import json
import socket
import sys
from datetime import datetime, timezone

import zenoh
from kuksa_client.grpc.aio import VSSClient


DEFAULT_KUKSA_HOST = "127.0.0.1"
DEFAULT_KUKSA_PORT = 55555
DEFAULT_ZENOH_PEER = "tcp/192.168.100.10:7447"


# Kuksa VSS path -> (Zenoh key expression, unit string for the JSON
# payload). VM1's zenoh_client.py whitelists the same Kuksa paths via
# its own BRIDGED_PATHS table; adding a new sensor here without also
# adding it to VM1 will silently no-op (the client logs "ignoring
# unknown path"). Keep them in sync.
BRIDGED_PATHS = {
    "Vehicle.Cabin.HVAC.AmbientAirTemperature": (
        "ev-range/vm2/cabin/Vehicle.Cabin.HVAC.AmbientAirTemperature",
        "celsius",
    ),
    "Vehicle.Cabin.Seat.Row1.DriverSide.Heating": (
        "ev-range/vm2/seat/Vehicle.Cabin.Seat.Row1.DriverSide.Heating",
        "percent",
    ),
    "Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling": (
        "ev-range/vm2/seat/Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling",
        "percent",
    ),
}


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{ts}] [zenoh-pub] {msg}", flush=True)


def build_zenoh_config(peer: str) -> zenoh.Config:
    config = zenoh.Config()
    config.insert_json5("connect/endpoints", f'["{peer}"]')
    config.insert_json5("listen/endpoints", '["tcp/0.0.0.0:0"]')
    return config


def make_payload(path: str, value, unit, source: str) -> bytes:
    payload = {
        "path": path,
        "value": float(value) if isinstance(value, (int, float)) else value,
        "unit": unit,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    return json.dumps(payload).encode("utf-8")


async def run(kuksa_host: str, kuksa_port: int, zenoh_peer: str) -> None:
    source = socket.gethostname()
    log(f"Connecting to local Kuksa Databroker at {kuksa_host}:{kuksa_port}...")
    async with VSSClient(kuksa_host, kuksa_port) as kuksa:
        log("Connected to local Kuksa.")
        log("Subscribed Kuksa paths -> Zenoh keys:")
        for k, (zk, unit) in BRIDGED_PATHS.items():
            log(f"    {k}")
            log(f"      -> {zk}  (unit={unit})")

        log(f"Opening Zenoh session, connecting to {zenoh_peer}")
        with zenoh.open(build_zenoh_config(zenoh_peer)) as session:
            publishers = {
                path: session.declare_publisher(zk)
                for path, (zk, _u) in BRIDGED_PATHS.items()
            }
            log(
                "Publisher running. Drive values from the Kuksa CLI on VM2 "
                "(e.g. `publish Vehicle.Cabin.HVAC.AmbientAirTemperature 22.0`). "
                "Ctrl+C to stop."
            )

            paths = list(BRIDGED_PATHS.keys())
            async for updates in kuksa.subscribe_current_values(paths):
                for path, dp in updates.items():
                    if dp is None or dp.value is None:
                        continue
                    cfg = BRIDGED_PATHS.get(path)
                    if cfg is None:
                        continue
                    _zk, unit = cfg
                    try:
                        payload = make_payload(path, dp.value, unit, source)
                        publishers[path].put(payload)
                        log(f"FWD  {path} = {dp.value}  ->  zenoh ({len(payload)} B)")
                    except Exception as exc:
                        log(f"ERROR forwarding {path}: {exc}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Zenoh publisher (VM2). Subscribes to VM2's local "
                    "Kuksa Databroker and forwards updates over Zenoh to VM1."
    )
    p.add_argument("--kuksa-host", default=DEFAULT_KUKSA_HOST,
                   help=f"Local Kuksa Databroker host (default: {DEFAULT_KUKSA_HOST})")
    p.add_argument("--kuksa-port", type=int, default=DEFAULT_KUKSA_PORT,
                   help=f"Local Kuksa Databroker port (default: {DEFAULT_KUKSA_PORT})")
    p.add_argument("--zenoh-peer", default=DEFAULT_ZENOH_PEER,
                   help=f"VM1's Zenoh listener endpoint (default: {DEFAULT_ZENOH_PEER})")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args.kuksa_host, args.kuksa_port, args.zenoh_peer))
    except KeyboardInterrupt:
        log("Stopping.")
        return 0
    except Exception as exc:
        log(f"FATAL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
