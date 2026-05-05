"""Zenoh client / subscriber (runs on VM1).

The receiving half of the VM2 -> VM1 bridge. VM2's `zenoh_publisher.py`
forwards every Kuksa-CLI publish on VM2 over Zenoh; this client listens
on tcp/0.0.0.0:7447, decodes each JSON sample and writes the value into
the local ev-range Kuksa Databroker on 127.0.0.1:55555 - the same
Databroker that `range_ai.py` subscribes to.

End-to-end:

    Kuksa CLI on VM2 -> VM2 Kuksa -> zenoh_publisher.py
        |
        v   tcp/7447 (zenoh)
    VM1: zenoh_client.py (this file)
        zenoh.declare_subscriber("ev-range/vm2/**", listener)
            -> kuksa.set_current_values({path: Datapoint(value)})
        |
        v
    VM1 ev-range Kuksa Databroker (127.0.0.1:55555)
        |
        v
    range_ai.py (consumes the cabin signals + recomputes Range)
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime

import zenoh
from kuksa_client.grpc import Datapoint
from kuksa_client.grpc.aio import VSSClient


DEFAULT_LISTEN = "tcp/0.0.0.0:7447"
DEFAULT_KEY_EXPR = "ev-range/vm2/**"
DEFAULT_KUKSA_HOST = "127.0.0.1"
DEFAULT_KUKSA_PORT = 55555


# Whitelist of VSS paths the client is allowed to write into the local
# Kuksa Databroker, with the type we coerce the JSON 'value' to before
# handing it to Kuksa. Kuksa rejects writes with a type mismatch, so
# the casts here mirror the VSS `datatype` (int8 -> int, float -> float).
# Keep this in sync with vm2/zenoh_publisher.py BRIDGED_PATHS.
BRIDGED_PATHS = {
    "Vehicle.Cabin.HVAC.AmbientAirTemperature":            float,  # sensor, float, celsius
    "Vehicle.Cabin.Seat.Row1.DriverSide.Heating":          int,    # actuator, int8, percent
    "Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling":   int,    # actuator, int8, percent
}


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{ts}] [zenoh-cli] {msg}", flush=True)


def build_zenoh_config(listen_endpoint: str) -> zenoh.Config:
    config = zenoh.Config()
    config.insert_json5("listen/endpoints", f'["{listen_endpoint}"]')
    return config


async def push_to_kuksa(client: VSSClient, path: str, value, src: str) -> None:
    cast = BRIDGED_PATHS.get(path)
    if cast is None:
        log(f"WARN ignoring unknown path '{path}' (not in BRIDGED_PATHS)")
        return
    try:
        coerced = cast(value)
    except (TypeError, ValueError) as exc:
        log(f"WARN cannot cast {value!r} -> {cast.__name__} for {path}: {exc}")
        return
    try:
        await client.set_current_values({path: Datapoint(coerced)})
    except Exception as exc:
        log(f"ERROR writing {path}={coerced} to Kuksa: {exc}")
        return
    log(f"OK   {path} = {coerced} (from {src})")


async def run(listen: str, key_expr: str, kuksa_host: str, kuksa_port: int) -> None:
    log(f"Connecting to Kuksa Databroker at {kuksa_host}:{kuksa_port}...")
    async with VSSClient(kuksa_host, kuksa_port) as kuksa:
        log("Connected to Kuksa.")
        log("Whitelisted VSS paths (VM2 publishes -> client writes here):")
        for p in BRIDGED_PATHS:
            log(f"    - {p}")

        loop = asyncio.get_running_loop()
        log(f"Opening Zenoh session, listen={listen}, subscribed to '{key_expr}'")
        with zenoh.open(build_zenoh_config(listen)) as session:
            stop_event = asyncio.Event()

            def listener(sample: zenoh.Sample) -> None:
                # Zenoh callbacks fire on a Zenoh worker thread - we have
                # to bounce the kuksa write back into the asyncio loop.
                try:
                    raw = sample.payload.to_string()
                    msg = json.loads(raw)
                except Exception as exc:
                    log(f"WARN bad payload on '{sample.key_expr}': {exc}")
                    return
                path = msg.get("path")
                value = msg.get("value")
                src = msg.get("source", "?")
                if path is None or value is None:
                    log(f"WARN payload missing 'path'/'value': {msg}")
                    return
                asyncio.run_coroutine_threadsafe(
                    push_to_kuksa(kuksa, path, value, src), loop
                )

            _sub = session.declare_subscriber(key_expr, listener)
            log("Zenoh client running. Ctrl+C to stop.")
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Zenoh client (VM1) for the EV Range Extender. "
                    "Subscribes to VM2's Zenoh publisher and writes "
                    "values into the local ev-range Kuksa Databroker."
    )
    p.add_argument("--listen", default=DEFAULT_LISTEN,
                   help=f"Zenoh listen endpoint (default: {DEFAULT_LISTEN})")
    p.add_argument("--key", default=DEFAULT_KEY_EXPR,
                   help=f"Zenoh key expression to subscribe to (default: {DEFAULT_KEY_EXPR})")
    p.add_argument("--kuksa-host", default=DEFAULT_KUKSA_HOST,
                   help=f"Kuksa Databroker host (default: {DEFAULT_KUKSA_HOST})")
    p.add_argument("--kuksa-port", type=int, default=DEFAULT_KUKSA_PORT,
                   help=f"Kuksa Databroker port (default: {DEFAULT_KUKSA_PORT})")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args.listen, args.key, args.kuksa_host, args.kuksa_port))
    except KeyboardInterrupt:
        log("Stopping.")
        return 0
    except Exception as exc:
        log(f"FATAL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
