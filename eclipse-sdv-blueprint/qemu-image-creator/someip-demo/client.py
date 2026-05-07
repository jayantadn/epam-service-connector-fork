"""SOME/IP client for the VM2 -> VM1 demo.

Runs on VM1 (192.168.100.10). Joins the SOME/IP-SD multicast group on
the bridge and subscribes to the demo eventgroup; prints each event
payload as it arrives.

Mirror of someip-demo/server.py - see that file for the service contract.
"""

import argparse
import asyncio
import ipaddress
import logging

from someipy import (
    EventGroup,
    ServiceBuilder,
    SomeIpMessage,
    TransportLayerProtocol,
)
from someipy.client_service_instance import construct_client_service_instance
from someipy.logging import set_someipy_log_level
from someipy.service_discovery import construct_service_discovery


SD_MULTICAST_GROUP = "224.224.224.245"
SD_PORT            = 30490

DEMO_SERVICE_ID    = 0x1000
DEMO_INSTANCE_ID   = 0x0001
DEMO_MAJOR_VERSION = 1
DEMO_EVENTGROUP_ID = 0x0001
DEMO_EVENT_ID      = 0x8001

DEFAULT_INTERFACE_IP = "192.168.100.10"   # VM1 bridge IP
DEFAULT_EVENT_PORT   = 30510


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SOME/IP client (runs on VM1). Subscribes to the demo "
                    "Hello service offered by VM2."
    )
    p.add_argument("--interface-ip", default=DEFAULT_INTERFACE_IP,
                   help=f"VM1 bridge interface IPv4 (default: {DEFAULT_INTERFACE_IP})")
    p.add_argument("--port", type=int, default=DEFAULT_EVENT_PORT,
                   help=f"UDP port to receive events on (default: {DEFAULT_EVENT_PORT})")
    p.add_argument("--debug", action="store_true",
                   help="Verbose someipy logging")
    return p.parse_args()


def on_event(message: SomeIpMessage) -> None:
    event_id = message.header.method_id
    payload = message.payload
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = payload.hex()
    print(
        f"[CLI] event 0x{event_id:04x} ({len(payload)} B) -> {text!r}",
        flush=True,
    )


async def amain() -> None:
    args = parse_args()
    set_someipy_log_level(logging.DEBUG if args.debug else logging.WARNING)

    print(
        f"[CLI] Starting SOME/IP-SD on {SD_MULTICAST_GROUP}:{SD_PORT} "
        f"via {args.interface_ip}",
        flush=True,
    )
    sd = await construct_service_discovery(
        SD_MULTICAST_GROUP, SD_PORT, args.interface_ip
    )

    service = (
        ServiceBuilder()
        .with_service_id(DEMO_SERVICE_ID)
        .with_major_version(DEMO_MAJOR_VERSION)
        .build()
    )

    client = await construct_client_service_instance(
        service=service,
        instance_id=DEMO_INSTANCE_ID,
        endpoint=(ipaddress.IPv4Address(args.interface_ip), args.port),
        ttl=5,
        sd_sender=sd,
        protocol=TransportLayerProtocol.UDP,
    )

    client.register_callback(on_event)
    client.subscribe_eventgroup(DEMO_EVENTGROUP_ID)
    sd.attach(client)

    print(
        f"[CLI] Subscribed to service 0x{DEMO_SERVICE_ID:04x} eventgroup "
        f"0x{DEMO_EVENTGROUP_ID:04x} on {args.interface_ip}:{args.port}. "
        f"Waiting for events... (Ctrl+C to exit)",
        flush=True,
    )

    try:
        await asyncio.Future()
    finally:
        print("[CLI] Shutting down...", flush=True)
        try:
            await client.close()
        except Exception:
            pass
        sd.close()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("[CLI] Stopping (KeyboardInterrupt).")


if __name__ == "__main__":
    main()
