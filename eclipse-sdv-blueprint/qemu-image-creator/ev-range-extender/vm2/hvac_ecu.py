"""HVAC ECU - runs on VM2.

Auto-deployed onto VM2 by cloud-init (no manual scp). Started
automatically by the `ev-range-hvac.service` systemd unit on boot.

Role:
    The HVAC ECU is the device-side ECU that owns the cabin HVAC
    branch of the local Kuksa Databroker on VM2. It receives a value
    from the host PyTk dashboard (`hardware-sim/pytk_dashboard.py`)
    over Zenoh on `sim/cabin/temp` and writes it into the local
    `ev-range-cabin` Kuksa Databroker as
    `Vehicle.Cabin.HVAC.AmbientAirTemperature`. From there VM2's
    `zenoh_publisher.py` bridges the value to VM1's `ev-range` Kuksa,
    which `range_ai.py` consumes.

    NOTE on the dashboard label: the slider is labelled "Fan Speed"
    in the GUI for the demo narrative, but the underlying Zenoh key
    (`sim/cabin/temp`) and the VSS path
    (`Vehicle.Cabin.HVAC.AmbientAirTemperature`) are deliberately
    kept exactly as the original signal catalogue defines them. The
    range model on VM1 interprets the numeric value as fan-speed
    percent for the demo - see `vm1/range_ai.py` for the math.

End-to-end:

    pytk_dashboard.py (host, 192.168.100.1)
        |
        | zenoh.put on:
        |   sim/cabin/temp  (float, 0..100)
        v   tcp/192.168.100.11:7461
    hvac_ecu.py (this file, VM2)
        |
        v
    VM2 ev-range-cabin Kuksa Databroker (127.0.0.1:55555)
        - Vehicle.Cabin.HVAC.AmbientAirTemperature = float
        |
        | (zenoh_publisher.py forwards over Zenoh to VM1)
        v
    VM1 ev-range Kuksa Databroker
        |
        v
    range_ai.py (recomputes Range)

Wire format:
    Each Zenoh sample is a tiny JSON document:
        {"value": <number>, "source": "<host>", "ts": "<iso>"}

Manual control (when the systemd service is stopped):
    sudo systemctl stop ev-range-hvac
    cd /home/ubuntu/ev-range-extender/vm2
    python3 hvac_ecu.py
    python3 hvac_ecu.py --listen tcp/0.0.0.0:7461
"""

import argparse
import asyncio
import json
import sys
import threading
from datetime import datetime
from typing import Any

import zenoh
from kuksa_client.grpc import Datapoint
from kuksa_client.grpc.aio import VSSClient


DEFAULT_LISTEN = "tcp/0.0.0.0:7461"
DEFAULT_KUKSA_HOST = "127.0.0.1"
DEFAULT_KUKSA_PORT = 55555


KEY_TO_VSS = {
    "sim/cabin/temp": (
        "Vehicle.Cabin.HVAC.AmbientAirTemperature",
        float,
    ),
}

KEY_PREFIX = "sim/cabin/temp"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{ts}] [hvac] {msg}", flush=True)


def build_zenoh_config(listen_endpoint: str) -> zenoh.Config:
    config = zenoh.Config()
    config.insert_json5("listen/endpoints", f'["{listen_endpoint}"]')
    return config


class _LatestValueQueue:
    """Coalescing latest-value queue for a small number of VSS paths.

    Producers (the Zenoh worker thread) call `offer(path, value, cast,
    src)` on every incoming sample. When multiple samples for the same
    path arrive before the consumer drains, only the LAST one survives.
    The single consumer (one asyncio task) calls `take()` and gets a
    snapshot of all pending paths, then clears the slot.

    This caps Kuksa RPC traffic at the asyncio loop tick rate, no matter
    how fast the dashboard's slider drags fire, so a fast drag never
    queues up a backlog of stale writes - the user always sees the
    most recent value land in Kuksa with ~asyncio-tick latency.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[Any, Any, str]] = {}
        self._evt = asyncio.Event()

    def offer(self, path: str, value: Any, cast: Any, src: str) -> None:
        """Producer side. Safe to call from any thread; never blocks."""
        with self._lock:
            self._pending[path] = (value, cast, src)
        # Wake the consumer task on the asyncio loop thread.
        self._loop.call_soon_threadsafe(self._evt.set)

    async def take(self) -> dict[str, tuple[Any, Any, str]]:
        """Consumer side. Awaits at least one offered value, returns snapshot."""
        while True:
            await self._evt.wait()
            with self._lock:
                if self._pending:
                    snapshot = self._pending
                    self._pending = {}
                    self._evt.clear()
                    return snapshot
                # Spurious wake-up (offer raced with a previous take's
                # critical section). Clear and re-await.
                self._evt.clear()


async def _consumer(queue: "_LatestValueQueue", kuksa: VSSClient) -> None:
    """Drain the latest-value queue and push to Kuksa with dedup.

    One Kuksa RPC per asyncio loop tick that has pending data. Identical
    re-writes (same path + same coerced value as last time) are dropped
    so the broker isn't woken up needlessly during back-and-forth scrubs.
    """
    last_sent: dict[str, Any] = {}
    while True:
        pending = await queue.take()
        updates: dict[str, Datapoint] = {}
        log_lines: list[str] = []
        for path, (raw_value, cast, src) in pending.items():
            try:
                coerced = cast(raw_value)
            except (TypeError, ValueError) as exc:
                log(f"WARN cannot cast {raw_value!r} -> {cast.__name__} for {path}: {exc}")
                continue
            if last_sent.get(path) == coerced:
                continue
            updates[path] = Datapoint(coerced)
            last_sent[path] = coerced
            log_lines.append(f"OK   {path} = {coerced} (from {src})")
        if not updates:
            continue
        try:
            await kuksa.set_current_values(updates)
        except Exception as exc:
            log(f"ERROR writing {len(updates)} key(s) to Kuksa: {exc}")
            continue
        for line in log_lines:
            log(line)


async def run(listen: str, kuksa_host: str, kuksa_port: int) -> None:
    log(f"Connecting to Kuksa Databroker at {kuksa_host}:{kuksa_port}...")
    async with VSSClient(kuksa_host, kuksa_port) as kuksa:
        log("Connected to Kuksa.")
        log("Subscribed Zenoh keys -> VSS paths:")
        for k, (vss, cast) in KEY_TO_VSS.items():
            log(f"    {k}  ->  {vss}  ({cast.__name__})")

        loop = asyncio.get_running_loop()
        queue = _LatestValueQueue(loop)
        consumer_task = asyncio.create_task(_consumer(queue, kuksa))
        log(f"Opening Zenoh session, listen={listen}, subscribed to '{KEY_PREFIX}'")
        with zenoh.open(build_zenoh_config(listen)) as session:
            stop_event = asyncio.Event()

            def listener(sample: zenoh.Sample) -> None:
                key = str(sample.key_expr)
                cfg = KEY_TO_VSS.get(key)
                if cfg is None:
                    log(f"WARN ignoring unknown key '{key}'")
                    return
                vss_path, cast = cfg
                try:
                    raw = sample.payload.to_string()
                    msg = json.loads(raw)
                except Exception as exc:
                    log(f"WARN bad payload on '{key}': {exc}")
                    return
                value = msg.get("value")
                src = msg.get("source", "?")
                if value is None:
                    log(f"WARN payload missing 'value' on '{key}': {msg}")
                    return
                queue.offer(vss_path, value, cast, src)

            _sub = session.declare_subscriber(KEY_PREFIX, listener)
            log("HVAC ECU running. Drive values from the host PyTk dashboard. Ctrl+C to stop.")
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                pass
            finally:
                consumer_task.cancel()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="HVAC ECU on VM2. Listens on a Zenoh endpoint for "
                    "sim/cabin/temp samples driven by the host PyTk "
                    "dashboard, and writes the values into the local "
                    "ev-range-cabin Kuksa Databroker."
    )
    p.add_argument("--listen", default=DEFAULT_LISTEN,
                   help=f"Zenoh listen endpoint (default: {DEFAULT_LISTEN})")
    p.add_argument("--kuksa-host", default=DEFAULT_KUKSA_HOST,
                   help=f"Kuksa Databroker host (default: {DEFAULT_KUKSA_HOST})")
    p.add_argument("--kuksa-port", type=int, default=DEFAULT_KUKSA_PORT,
                   help=f"Kuksa Databroker port (default: {DEFAULT_KUKSA_PORT})")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args.listen, args.kuksa_host, args.kuksa_port))
    except KeyboardInterrupt:
        log("Stopping.")
        return 0
    except Exception as exc:
        log(f"FATAL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
