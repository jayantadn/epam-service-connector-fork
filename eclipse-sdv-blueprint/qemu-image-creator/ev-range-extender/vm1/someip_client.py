"""SOME/IP client / subscriber (runs on VM1).

This is the SOME/IP / Eclipse-SCore equivalent of `zenoh_client.py`.
Both files coexist; only one of them is run at a time during the demo.

The receiving half of the VM2 -> VM1 bridge. VM2's `someip_publisher.py`
forwards every Kuksa-CLI publish on VM2 as a SOME/IP notification event
(service 0xCAB0, eventgroup 0x0001, events 0x8001..0x8003); this client
subscribes to the eventgroup, decodes each binary payload using the
shared codecs in `common.someip_service`, and writes the value into
the local ev-range Kuksa Databroker on 127.0.0.1:55555 - the same
Databroker that `range_ai.py` subscribes to.

End-to-end:

    Kuksa CLI on VM2 -> VM2 Kuksa -> someip_publisher.py
        |
        v   udp/30490 (SD)  +  udp/30509 (events)
    VM1: someip_client.py (this file)
        SD: subscribe_eventgroup(0x0001) on service 0xCAB0
            -> register_callback(...) decodes payload by event_id
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
import ipaddress
import logging
import os
import sys
from datetime import datetime

from kuksa_client.grpc import Datapoint
from kuksa_client.grpc.aio import VSSClient

# someipy v1.x (pre-daemon) - flat in-process API.
from someipy import (
    EventGroup,
    ServiceBuilder,
    SomeIpMessage,
    TransportLayerProtocol,
)
from someipy.client_service_instance import construct_client_service_instance
from someipy.logging import set_someipy_log_level
from someipy.service_discovery import construct_service_discovery

# Path adjust so `from common.someip_service import ...` works regardless
# of how the file is launched.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from common.someip_service import (  # noqa: E402  (sys.path adjust above)
    ALL_EVENT_IDS,
    CABIN_EVENTGROUP_ID,
    CABIN_INSTANCE_ID,
    CABIN_MAJOR_VERSION,
    CABIN_SERVICE_ID,
    DEFAULT_CLIENT_EVENT_PORT,
    DEFAULT_VM1_INTERFACE_IP,
    SD_MULTICAST_GROUP,
    SD_PORT,
    SD_TTL_SECONDS,
    VSS_AMBIENT_TEMP,
    VSS_SEAT_HC,
    VSS_SEAT_HEAT,
    decode_event,
    vss_for_event_id,
)


DEFAULT_KUKSA_HOST = "127.0.0.1"
DEFAULT_KUKSA_PORT = 55555


# Whitelist of VSS paths the client is allowed to write into the local
# Kuksa Databroker, with the type we coerce the decoded value to before
# handing it to Kuksa. Kuksa rejects writes with a type mismatch, so
# the casts here mirror the VSS `datatype` (int8 -> int, float -> float).
# Keep this in sync with vm2/someip_publisher.py.
BRIDGED_PATHS = {
    VSS_AMBIENT_TEMP: float,   # sensor, float, celsius
    VSS_SEAT_HEAT:    int,     # actuator, int8, percent
    VSS_SEAT_HC:      int,     # actuator, int8, percent
}


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{ts}] [someip-cli] {msg}", flush=True)


async def push_to_kuksa(client: VSSClient, path: str, value, event_id: int) -> None:
    cast = BRIDGED_PATHS.get(path)
    if cast is None:
        log(f"WARN ignoring unmapped VSS path '{path}' for event 0x{event_id:04x}")
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
    log(f"OK   {path} = {coerced} (from someip event 0x{event_id:04x})")


async def run(
    kuksa_host: str,
    kuksa_port: int,
    interface_ip: str,
    event_port: int,
) -> None:
    log(f"Connecting to Kuksa Databroker at {kuksa_host}:{kuksa_port}...")
    async with VSSClient(kuksa_host, kuksa_port) as kuksa:
        log("Connected to Kuksa.")
        log("Whitelisted VSS paths (VM2 publishes -> client writes here):")
        for p in BRIDGED_PATHS:
            log(f"    - {p}")

        loop = asyncio.get_running_loop()

        log(
            f"Starting SOME/IP-SD on {SD_MULTICAST_GROUP}:{SD_PORT} "
            f"via interface {interface_ip}"
        )
        sd = await construct_service_discovery(
            SD_MULTICAST_GROUP, SD_PORT, interface_ip
        )

        # The client side does not need the eventgroup wired into the
        # Service object (the SD subscribe_eventgroup carries the id by
        # itself), but we keep the schema declaration symmetric with
        # the publisher for clarity.
        eventgroup = EventGroup(
            id=CABIN_EVENTGROUP_ID, event_ids=list(ALL_EVENT_IDS)
        )
        service = (
            ServiceBuilder()
            .with_service_id(CABIN_SERVICE_ID)
            .with_major_version(CABIN_MAJOR_VERSION)
            .build()
        )

        log(
            f"Constructing ClientServiceInstance "
            f"(service=0x{CABIN_SERVICE_ID:04x}, instance=0x{CABIN_INSTANCE_ID:04x}, "
            f"endpoint={interface_ip}:{event_port}, ttl={SD_TTL_SECONDS}s)"
        )
        client_inst = await construct_client_service_instance(
            service=service,
            instance_id=CABIN_INSTANCE_ID,
            endpoint=(ipaddress.IPv4Address(interface_ip), event_port),
            ttl=SD_TTL_SECONDS,
            sd_sender=sd,
            protocol=TransportLayerProtocol.UDP,
        )

        # someipy invokes the callback on its own asyncio task / thread.
        # We need to bounce the Kuksa write back into our running loop
        # because VSSClient is async-only.
        def on_event(message: SomeIpMessage) -> None:
            event_id = message.header.method_id
            payload = message.payload
            try:
                value = decode_event(event_id, payload)
            except KeyError:
                log(f"WARN unknown event id 0x{event_id:04x} ({len(payload)} B)")
                return
            except Exception as exc:
                log(
                    f"WARN failed to decode event 0x{event_id:04x} "
                    f"({len(payload)} B): {exc}"
                )
                return

            vss_path = vss_for_event_id(event_id)
            if vss_path is None:
                log(f"WARN no VSS path mapped for event 0x{event_id:04x}")
                return

            asyncio.run_coroutine_threadsafe(
                push_to_kuksa(kuksa, vss_path, value, event_id), loop
            )

        client_inst.register_callback(on_event)
        client_inst.subscribe_eventgroup(CABIN_EVENTGROUP_ID)
        sd.attach(client_inst)

        log("SOME/IP client running. Waiting for offers from VM2. Ctrl+C to stop.")
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pass
        finally:
            log("Closing client service instance...")
            try:
                await client_inst.close()
            except Exception as exc:
                log(f"WARN client.close() raised: {exc}")
            log("Closing service discovery...")
            try:
                sd.close()
            except Exception as exc:
                log(f"WARN sd.close() raised: {exc}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SOME/IP client (VM1) for the EV Range Extender. "
                    "Subscribes to VM2's SOME/IP cabin service and writes "
                    "values into the local ev-range Kuksa Databroker."
    )
    p.add_argument("--kuksa-host", default=DEFAULT_KUKSA_HOST,
                   help=f"Kuksa Databroker host (default: {DEFAULT_KUKSA_HOST})")
    p.add_argument("--kuksa-port", type=int, default=DEFAULT_KUKSA_PORT,
                   help=f"Kuksa Databroker port (default: {DEFAULT_KUKSA_PORT})")
    p.add_argument("--interface-ip", default=DEFAULT_VM1_INTERFACE_IP,
                   help="VM1 bridge interface IPv4 address used as the "
                        f"SOME/IP-SD source and event sink "
                        f"(default: {DEFAULT_VM1_INTERFACE_IP})")
    p.add_argument("--port", type=int, default=DEFAULT_CLIENT_EVENT_PORT,
                   help=f"UDP port to receive SOME/IP events on "
                        f"(default: {DEFAULT_CLIENT_EVENT_PORT})")
    p.add_argument("--debug", action="store_true",
                   help="Verbose someipy logging")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    set_someipy_log_level(logging.DEBUG if args.debug else logging.WARNING)
    try:
        asyncio.run(run(args.kuksa_host, args.kuksa_port,
                        args.interface_ip, args.port))
    except KeyboardInterrupt:
        log("Stopping.")
        return 0
    except Exception as exc:
        log(f"FATAL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
