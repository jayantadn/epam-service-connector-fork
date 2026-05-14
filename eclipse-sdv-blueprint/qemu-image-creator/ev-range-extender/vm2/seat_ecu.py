"""Seat Control Module - runs on VM2.

Auto-deployed onto VM2 by cloud-init (no manual scp). Started
automatically by the `ev-range-seat.service` systemd unit on boot.

Role:
    The Seat Control Module is the device-side ECU that owns the
    front-row driver-side seat heating and ventilation/cooling
    actuators of the local Kuksa Databroker on VM2. It has TWO
    duties:

    1. Inbound from the host dashboard:
       Receives setpoints from `hardware-sim/pytk_dashboard.py` over
       Zenoh on `sim/cabin/seat/heating` and `sim/cabin/seat/hc` and
       writes them into the local `ev-range-cabin` Kuksa Databroker
       (logged as `OK`). The kuksa-bridge mirrors the values to VM1.

    2. Inbound from the kuksa-bridge (NEW):
       Subscribes to both seat VSS paths on its OWN local Kuksa and,
       on every change, (a) logs an `ACT` line so the actuation is
       visible in `tail -F /tmp/ev-range-seat.log` and (b) forwards
       a key/value status envelope to the host dashboard over Zenoh
       on `dash/status/seat`. This is the path that carries writes
       made by the EV Range Extender app on VM1.

End-to-end:

    pytk_dashboard.py (host, 192.168.100.1)
        |   ^
        |   | zenoh on dash/status/seat
        |   |   {"key": "seat.heating" | "seat.heating_cooling",
        |   |    "value": <int>,
        |   |    "status": "heating" | "cooling" | "off"}
        |   |
        | zenoh.put on:
        |   sim/cabin/seat/heating  (int8,  0..100   percent)
        |   sim/cabin/seat/hc       (int8, -100..100 percent;
        |                            negative = ventilation/cooling,
        |                            positive = heating)
        v   tcp/192.168.100.11:7462
    seat_ecu.py (this file, VM2)
        |    ^
        |    | kuksa subscribe_current_values()
        v    | (ACT log + dashboard forward)
    VM2 ev-range-cabin Kuksa Databroker (127.0.0.1:55555)
        - Vehicle.Cabin.Seat.Row1.DriverSide.Heating         = int
        - Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling  = int
        |
        | (kuksa-bridge bridges over Zenoh to VM1)
        v
    VM1 ev-range Kuksa Databroker  <-- EV Range Extender app writes here
        |
        v
    range_ai.py (recomputes Range)

Wire format (host -> ECU):
    Each Zenoh sample on `sim/cabin/seat/**` is a tiny JSON document:
        {"value": <number>, "source": "<host>", "ts": "<iso>"}

Wire format (ECU -> dashboard, NEW):
    Each Zenoh sample on `dash/status/seat` is a key/value envelope:
        {"key": "seat.heating" | "seat.heating_cooling",
         "value": <int>,
         "status": "heating" | "cooling" | "off",
         "source": "vm2",
         "ts": "<iso>"}
    The dashboard reduces the two latest-status keys into one seat
    indicator: red if any signal says "heating", blue if any says
    "cooling", and blue/off otherwise.

Manual control (when the systemd service is stopped):
    sudo systemctl stop ev-range-seat
    cd /home/ubuntu/ev-range-extender/vm2
    python3 seat_ecu.py
    python3 seat_ecu.py --listen tcp/0.0.0.0:7462
"""

import argparse
import asyncio
import json
import sys
import threading
from datetime import datetime, timezone
from typing import Any

import zenoh
from kuksa_client.grpc import Datapoint
from kuksa_client.grpc.aio import VSSClient


DEFAULT_LISTEN = "tcp/0.0.0.0:7462"
DEFAULT_KUKSA_HOST = "127.0.0.1"
DEFAULT_KUKSA_PORT = 55555

# Source tag embedded in every `dash/status/seat` envelope so the
# dashboard can tell which VM the message came from (forensic only).
SOURCE_LABEL = "vm2"

# Reverse Zenoh key the dashboard subscribes to.
DASH_STATUS_KEY = "dash/status/seat"


KEY_TO_VSS = {
    "sim/cabin/seat/heating": (
        "Vehicle.Cabin.Seat.Row1.DriverSide.Heating",
        int,
    ),
    "sim/cabin/seat/hc": (
        "Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling",
        int,
    ),
}

KEY_PREFIX = "sim/cabin/seat/**"


# Mapping: local VSS path -> logical key the dashboard renders under.
# Listed independently of KEY_TO_VSS because the dashboard-forward
# path is decoupled from the host-Zenoh ingest path.
VSS_TO_DASH_KEY = {
    "Vehicle.Cabin.Seat.Row1.DriverSide.Heating":        "seat.heating",
    "Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling": "seat.heating_cooling",
}


def _seat_status(vss_path: str, value: Any) -> str:
    """Map a (path, value) pair to the dashboard indicator state.

    Indicator semantics (see module docstring):
       Heating          > 0  -> "heating"  (dashboard renders red)
       HeatingCooling   > 0  -> "heating"  (dashboard renders red)
       HeatingCooling   < 0  -> "cooling"  (dashboard renders blue)
       all other (=== 0)     -> "off"      (dashboard renders blue/idle)
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "off"
    if v > 0:
        return "heating"
    if vss_path.endswith("HeatingCooling") and v < 0:
        return "cooling"
    return "off"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{ts}] [seat] {msg}", flush=True)


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

    For seat the queue is especially useful because Heating and
    HeatingCooling toggles can flip near-simultaneously (the host
    dashboard's mutex publishes them in quick succession). Both end
    up in the same snapshot and are written to Kuksa in a single
    batched RPC.
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
                self._evt.clear()


async def _consumer(queue: "_LatestValueQueue", kuksa: VSSClient) -> None:
    """Drain the latest-value queue and push to Kuksa with dedup.

    Identical re-writes (same path + same coerced value as last time)
    are dropped so the broker isn't woken up needlessly when the user
    re-toggles to the same state.
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


async def _dashboard_forwarder(
    kuksa: VSSClient,
    dash_pub: "zenoh.Publisher",
) -> None:
    """Subscribe to both seat VSS paths on local Kuksa and forward each
    change to the host dashboard as a `{key, value, status}` envelope.

    See module docstring for the surface contract; semantics are kept
    intentionally tiny on this side so the dashboard can stay a dumb
    renderer that just maps `status` to a color.
    """
    last_status: dict[str, str] = {}
    paths = list(VSS_TO_DASH_KEY.keys())
    async for updates in kuksa.subscribe_current_values(paths):
        for path, dp in updates.items():
            if dp is None or dp.value is None:
                continue
            dash_key = VSS_TO_DASH_KEY.get(path)
            if dash_key is None:
                continue
            status = _seat_status(path, dp.value)
            payload = json.dumps({
                "key": dash_key,
                "value": int(dp.value) if isinstance(dp.value, (int, float))
                                       else dp.value,
                "status": status,
                "source": SOURCE_LABEL,
                "ts": datetime.now(timezone.utc).isoformat(),
            }).encode("utf-8")
            try:
                dash_pub.put(payload)
            except Exception as exc:
                log(f"ERROR forwarding {path} to dashboard: {exc}")
                continue
            changed = last_status.get(path) != status
            last_status[path] = status
            tag = "ACT " if changed else "act "
            log(f"{tag} {path} = {dp.value}  -> dashboard {dash_key} (status={status})")


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

            # Reverse channel to the host dashboard - declared on the
            # SAME Zenoh session so the existing TCP peer connection
            # (host -> ECU) is reused for ECU -> host samples too.
            dash_pub = session.declare_publisher(DASH_STATUS_KEY)
            log(f"Reverse channel publisher on '{DASH_STATUS_KEY}' ready.")
            forwarder_task = asyncio.create_task(
                _dashboard_forwarder(kuksa, dash_pub)
            )
            log(f"Kuksa->dashboard forwarder subscribed to: "
                f"{', '.join(VSS_TO_DASH_KEY.keys())}")

            log("Seat ECU running. Drive values from the host PyTk dashboard. Ctrl+C to stop.")
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                pass
            finally:
                consumer_task.cancel()
                forwarder_task.cancel()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Seat Control Module on VM2. Listens on a Zenoh "
                    "endpoint for sim/cabin/seat/* samples driven by "
                    "the host PyTk dashboard, and writes the values "
                    "into the local ev-range-cabin Kuksa Databroker."
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
