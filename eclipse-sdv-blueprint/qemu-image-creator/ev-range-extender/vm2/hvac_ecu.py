"""HVAC ECU - runs on VM2.

Auto-deployed onto VM2 by cloud-init (no manual scp). Started
automatically by the `ev-range-hvac.service` systemd unit on boot.

Role:
    The HVAC ECU is the device-side ECU that owns the cabin HVAC
    branch of the local Kuksa Databroker on VM2. It has TWO duties:

    1. Inbound from the host dashboard:
       Receives a value from `hardware-sim/pytk_dashboard.py` over
       Zenoh on `sim/cabin/temp` and writes it into the local
       `ev-range-cabin` Kuksa Databroker as
       `Vehicle.Cabin.HVAC.AmbientAirTemperature` (logged as `OK`).
       From there the kuksa-bridge mirrors the value to VM1's Kuksa.

    2. Inbound from the kuksa-bridge (NEW):
       Subscribes to the same VSS path on its OWN local Kuksa and,
       on every change, (a) logs an `ACT` line so the actuation is
       visible in `tail -F /tmp/ev-range-hvac.log` and (b) forwards
       a key/value status envelope to the host dashboard over Zenoh
       on `dash/status/hvac`. This is the path that carries writes
       made by the EV Range Extender app on VM1: the app writes
       to VM1's Kuksa -> kuksa-bridge mirrors VM1 -> VM2 -> this
       subscriber fires here -> dashboard indicator turns green/red.

    NOTE on the dashboard label: the slider is labelled "Fan Speed"
    in the GUI for the demo narrative, but the underlying Zenoh key
    (`sim/cabin/temp`) and the VSS path
    (`Vehicle.Cabin.HVAC.AmbientAirTemperature`) are deliberately
    kept exactly as the original signal catalogue defines them. The
    range model on VM1 interprets the numeric value as fan-speed
    percent for the demo - see `vm1/range_ai.py` for the math.

End-to-end:

    pytk_dashboard.py (host, 192.168.100.1)
        |   ^
        |   | zenoh on dash/status/hvac
        |   |   {"key": "hvac.fan_speed",
        |   |    "value": <number>, "status": "on"|"off"}
        |   |
        | zenoh.put on:
        |   sim/cabin/temp  (float, 0..100)
        v   tcp/192.168.100.11:7461
    hvac_ecu.py (this file, VM2)
        |    ^
        |    | kuksa subscribe_current_values()
        v    | (ACT log + dashboard forward)
    VM2 ev-range-cabin Kuksa Databroker (127.0.0.1:55555)
        - Vehicle.Cabin.HVAC.AmbientAirTemperature = float
        |
        | (kuksa-bridge bridges over Zenoh to VM1)
        v
    VM1 ev-range Kuksa Databroker  <-- EV Range Extender app writes here
        |
        v
    range_ai.py (recomputes Range)

Wire format (host -> ECU):
    Each Zenoh sample on `sim/cabin/temp` is a tiny JSON document:
        {"value": <number>, "source": "<host>", "ts": "<iso>"}

Wire format (ECU -> dashboard, NEW):
    Each Zenoh sample on `dash/status/hvac` is a key/value envelope:
        {"key": "hvac.fan_speed",
         "value": <number>,
         "status": "on" | "off",
         "source": "vm2",
         "ts": "<iso>"}
    The dashboard drives the on-screen red/green indicator from
    `status` alone; `value` is shown alongside for context.

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
from datetime import datetime, timezone
from typing import Any

import zenoh
from kuksa_client.grpc import Datapoint
from kuksa_client.grpc.aio import VSSClient


DEFAULT_LISTEN = "tcp/0.0.0.0:7461"
DEFAULT_KUKSA_HOST = "127.0.0.1"
DEFAULT_KUKSA_PORT = 55555

# Source tag embedded in every `dash/status/hvac` envelope so the
# dashboard can tell which VM the message came from (forensic only;
# the dashboard does not filter on it today).
SOURCE_LABEL = "vm2"

# Reverse Zenoh key the dashboard subscribes to. Kept stable; pair it
# with the matching DASH_KEY_PAIR below so the dashboard can use the
# embedded `key` field rather than parsing the Zenoh key expression.
DASH_STATUS_KEY = "dash/status/hvac"

# Logical name used in the {"key": ..., "value": ...} envelope.
# Stable across releases - the dashboard uses this to pick the right
# indicator widget.
DASH_KEY_PAIR = "hvac.fan_speed"


KEY_TO_VSS = {
    "sim/cabin/temp": (
        "Vehicle.Cabin.HVAC.AmbientAirTemperature",
        float,
    ),
}

KEY_PREFIX = "sim/cabin/temp"


# VSS paths the ECU subscribes to on its local Kuksa to drive the
# dashboard indicator. Listed separately from KEY_TO_VSS because the
# dashboard-forward path is independent of the host-Zenoh ingest path.
VSS_TO_DASH = ("Vehicle.Cabin.HVAC.AmbientAirTemperature",)


def _hvac_status(value: float) -> str:
    """Map a fan-speed value (0..100) to the dashboard indicator state.

    Per the demo narrative the HVAC indicator is binary:
       fan > 0  -> "on"   (dashboard renders green)
       fan == 0 -> "off"  (dashboard renders red)
    """
    try:
        return "on" if float(value) > 0 else "off"
    except (TypeError, ValueError):
        return "off"


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


async def _consumer(
    queue: "_LatestValueQueue",
    kuksa: VSSClient,
    dash_pub: "zenoh.Publisher",
) -> None:
    """Drain the latest-value queue and push to Kuksa with dedup.

    One Kuksa RPC per asyncio loop tick that has pending data. Identical
    re-writes (same path + same coerced value as last time) are dropped
    so the broker isn't woken up needlessly during back-and-forth scrubs.
    """
    last_sent: dict[str, Any] = {}
    while True:
        pending = await queue.take()
        updates: dict[str, Datapoint] = {}
        ack_values: list[float] = []
        log_lines: list[str] = []
        for path, (raw_value, cast, src) in pending.items():
            try:
                coerced = cast(raw_value)
            except (TypeError, ValueError) as exc:
                log(f"WARN cannot cast {raw_value!r} -> {cast.__name__} for {path}: {exc}")
                continue
            if last_sent.get(path) == coerced:
                ack_values.append(float(coerced))
                continue
            updates[path] = Datapoint(coerced)
            last_sent[path] = coerced
            ack_values.append(float(coerced))
            log_lines.append(f"OK   {path} = {coerced} (from {src})")
        if updates:
            try:
                await kuksa.set_current_values(updates)
            except Exception as exc:
                log(f"ERROR writing {len(updates)} key(s) to Kuksa: {exc}")
                continue

        for v in ack_values:
            try:
                dash_pub.put(json.dumps({
                    "key": DASH_KEY_PAIR,
                    "value": v,
                    "status": _hvac_status(v),
                    "source": SOURCE_LABEL,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }).encode("utf-8"))
            except Exception as exc:
                log(f"WARN dashboard ACK publish failed: {exc}")

        for line in log_lines:
            log(line)


async def _dashboard_forwarder(
    kuksa: VSSClient,
    dash_pub: "zenoh.Publisher",
) -> None:
    """Subscribe to the HVAC VSS path on local Kuksa and forward
    each change to the host dashboard as a `{key, value, status}`
    envelope. Logs an `ACT` line per change so the actuation is
    visible in the ECU log.

    This is the path that surfaces writes made BY the EV Range
    Extender app on VM1: the app writes to VM1's Kuksa, the
    kuksa-bridge mirrors the value VM1 -> VM2, this subscriber
    fires here, the dashboard indicator updates.

    For the host-dashboard slider path the same subscriber also
    fires (since we write to Kuksa from `_consumer`), which means
    every slider movement results in a single dashboard-side echo.
    That is intentional: the indicator should reflect the current
    Kuksa state regardless of who wrote it.
    """
    last_status: dict[str, str] = {}
    async for updates in kuksa.subscribe_current_values(list(VSS_TO_DASH)):
        for path, dp in updates.items():
            if dp is None or dp.value is None:
                continue
            status = _hvac_status(dp.value)
            payload = json.dumps({
                "key": DASH_KEY_PAIR,
                "value": float(dp.value),
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
            log(f"{tag} {path} = {dp.value}  -> dashboard {DASH_KEY_PAIR} (status={status})")


async def run(listen: str, kuksa_host: str, kuksa_port: int) -> None:
    log(f"Connecting to Kuksa Databroker at {kuksa_host}:{kuksa_port}...")
    async with VSSClient(kuksa_host, kuksa_port) as kuksa:
        log("Connected to Kuksa.")
        log("Subscribed Zenoh keys -> VSS paths:")
        for k, (vss, cast) in KEY_TO_VSS.items():
            log(f"    {k}  ->  {vss}  ({cast.__name__})")

        loop = asyncio.get_running_loop()
        queue = _LatestValueQueue(loop)
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

            consumer_task = asyncio.create_task(_consumer(queue, kuksa, dash_pub))

            forwarder_task = asyncio.create_task(
                _dashboard_forwarder(kuksa, dash_pub)
            )
            log(f"Kuksa->dashboard forwarder subscribed to: "
                f"{', '.join(VSS_TO_DASH)}")

            log("HVAC ECU running. Drive values from the host PyTk dashboard. Ctrl+C to stop.")
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                pass
            finally:
                consumer_task.cancel()
                forwarder_task.cancel()


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
