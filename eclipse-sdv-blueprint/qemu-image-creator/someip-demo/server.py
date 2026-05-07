"""SOME/IP server for the VM2 -> VM1 demo.

Runs on VM2 (192.168.100.11). Offers a tiny "Hello" service over
SOME/IP-SD (multicast 224.224.224.245:30490) and emits a counter event
once per --interval seconds. VM1's client.py subscribes to the
eventgroup and prints each payload.

Service contract:

    service id     : 0x1000
    instance id    : 0x0001
    major version  : 1
    eventgroup id  : 0x0001
    event id       : 0x8001    payload = ASCII string
"""

import argparse
import asyncio
import ipaddress
import logging
import socket
from datetime import datetime

from someipy import (
    EventGroup,
    ServiceBuilder,
    TransportLayerProtocol,
    construct_server_service_instance,
)
from someipy.logging import set_someipy_log_level
from someipy.service_discovery import construct_service_discovery


SD_MULTICAST_GROUP = "224.224.224.245"
SD_PORT            = 30490

DEMO_SERVICE_ID    = 0x1000
DEMO_INSTANCE_ID   = 0x0001
DEMO_MAJOR_VERSION = 1
DEMO_EVENTGROUP_ID = 0x0001
DEMO_EVENT_ID      = 0x8001

DEFAULT_INTERFACE_IP = "192.168.100.11"   # VM2 bridge IP
DEFAULT_EVENT_PORT   = 30509


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SOME/IP server (runs on VM2). Offers the demo Hello "
                    "service and emits a counter event."
    )
    p.add_argument("--interface-ip", default=DEFAULT_INTERFACE_IP,
                   help=f"VM2 bridge interface IPv4 (default: {DEFAULT_INTERFACE_IP})")
    p.add_argument("--port", type=int, default=DEFAULT_EVENT_PORT,
                   help=f"Outbound UDP port for events (default: {DEFAULT_EVENT_PORT})")
    p.add_argument("--interval", type=float, default=1.0,
                   help="Seconds between events (default: 1.0)")
    p.add_argument("--count", type=int, default=0,
                   help="Number of events to send (0 = forever)")
    p.add_argument("--name", default=socket.gethostname(),
                   help="Sender name embedded in the payload (default: hostname)")
    p.add_argument("--debug", action="store_true",
                   help="Verbose someipy logging")
    return p.parse_args()


async def amain() -> None:
    args = parse_args()
    set_someipy_log_level(logging.DEBUG if args.debug else logging.WARNING)

    print(
        f"[SRV] Starting SOME/IP-SD on {SD_MULTICAST_GROUP}:{SD_PORT} "
        f"via {args.interface_ip}",
        flush=True,
    )
    sd = await construct_service_discovery(
        SD_MULTICAST_GROUP, SD_PORT, args.interface_ip
    )

    eventgroup = EventGroup(id=DEMO_EVENTGROUP_ID, event_ids=[DEMO_EVENT_ID])
    service = (
        ServiceBuilder()
        .with_service_id(DEMO_SERVICE_ID)
        .with_major_version(DEMO_MAJOR_VERSION)
        .with_eventgroup(eventgroup)
        .build()
    )

    server = await construct_server_service_instance(
        service,
        instance_id=DEMO_INSTANCE_ID,
        endpoint=(ipaddress.IPv4Address(args.interface_ip), args.port),
        ttl=5,
        sd_sender=sd,
        cyclic_offer_delay_ms=2000,
        protocol=TransportLayerProtocol.UDP,
    )
    sd.attach(server)

    print(
        f"[SRV] Offering service 0x{DEMO_SERVICE_ID:04x} instance "
        f"0x{DEMO_INSTANCE_ID:04x} on {args.interface_ip}:{args.port}",
        flush=True,
    )
    server.start_offer()

    try:
        index = 0
        while args.count == 0 or index < args.count:
            payload_str = (
                f"Hello from {args.name} #{index} at "
                f"{datetime.now().isoformat(timespec='milliseconds')}"
            )
            payload = payload_str.encode("utf-8")
            server.send_event(DEMO_EVENTGROUP_ID, DEMO_EVENT_ID, payload)
            print(
                f"[SRV] event 0x{DEMO_EVENT_ID:04x} ({len(payload)} B) "
                f"-> {payload_str!r}",
                flush=True,
            )
            index += 1
            await asyncio.sleep(args.interval)
    finally:
        print("[SRV] Stopping offers and shutting down...", flush=True)
        try:
            await server.stop_offer()
        except Exception:
            pass
        sd.close()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("[SRV] Stopping (KeyboardInterrupt).")


if __name__ == "__main__":
    main()
