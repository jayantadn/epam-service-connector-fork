"""SOME/IP publisher (runs on VM2).

This is the SOME/IP / Eclipse-SCore equivalent of `zenoh_publisher.py`.
Both files coexist; only one of them is run at a time during the demo.

Subscribes to VM2's local Kuksa Databroker for the cabin signals the
user drives via the Kuksa CLI on VM2, then republishes each update as
a SOME/IP notification event so VM1's `someip_client.py` can mirror it
into the ev-range Databroker that `range_ai.py` consumes.

End-to-end (no Python publisher anywhere - the Kuksa CLI is the only
source of truth for input signals):

    Kuksa CLI on VM2  --publish-->  VM2 Kuksa Databroker (127.0.0.1:55555)
                                            |
                                            | subscribe_current_values
                                            v
                                  someip_publisher.py (this file)
                                            |
                                            | SOME/IP notification event
                                            | service 0xCAB0, eg 0x0001,
                                            | event 0x800{1,2,3}
                                            v   udp/30490 (SD)  +  udp/30509 (events)
                                  VM1 someip_client.py
                                            |
                                            v
                                  VM1 ev-range Kuksa Databroker
                                            |
                                            v
                                  range_ai.py (recomputes Range)

Bridged signals (each must exist in VM2's Kuksa VSS catalog - this is
provided automatically by the `sdv-runtime` image used by VM2's
cloud-init; no JSON file or `--metadata` flag needed):

    Vehicle.Cabin.HVAC.AmbientAirTemperature        (sensor, float, celsius)
    Vehicle.Cabin.Seat.Row1.DriverSide.Heating      (actuator, int8, percent)
    Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
                                                    (actuator, int8, percent;
                                                     negative = cooling/vent,
                                                     positive = heating)

Prerequisites on VM2:
  * Kuksa Databroker running on 127.0.0.1:55555 with the EPAM/COVESA
    VSS catalog loaded (this happens automatically because cloud-init
    boots the same `sdv-runtime` image used on VM1).
  * `someipy` (>=1.0,<2.0), `kuksa-client` Python packages.
"""

import argparse
import asyncio
import ipaddress
import logging
import os
import sys
from datetime import datetime

from kuksa_client.grpc.aio import VSSClient

# someipy v1.x (pre-daemon) - flat in-process API. We import only what
# the publisher needs.
from someipy import (
    EventGroup,
    ServiceBuilder,
    TransportLayerProtocol,
    construct_server_service_instance,
)
from someipy.logging import set_someipy_log_level
from someipy.service_discovery import construct_service_discovery

# Make `from common.someip_service import ...` work regardless of how
# the file is launched (`python3 someip_publisher.py` from vm2/, or
# `python3 -m vm2.someip_publisher` from ev-range-extender/).
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
    CYCLIC_OFFER_DELAY_MS,
    DEFAULT_SERVER_EVENT_PORT,
    DEFAULT_VM2_INTERFACE_IP,
    SD_MULTICAST_GROUP,
    SD_PORT,
    SD_TTL_SECONDS,
    VSS_TO_EVENT,
    encode_event,
    unit_for_event,
)


DEFAULT_KUKSA_HOST = "127.0.0.1"
DEFAULT_KUKSA_PORT = 55555


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{ts}] [someip-pub] {msg}", flush=True)


async def run(
    kuksa_host: str,
    kuksa_port: int,
    interface_ip: str,
    event_port: int,
) -> None:
    log(f"Connecting to local Kuksa Databroker at {kuksa_host}:{kuksa_port}...")
    async with VSSClient(kuksa_host, kuksa_port) as kuksa:
        log("Connected to local Kuksa.")
        log("Subscribed Kuksa paths -> SOME/IP events:")
        for vss, eid in VSS_TO_EVENT.items():
            log(f"    {vss}")
            log(f"      -> event 0x{eid:04x}  (unit={unit_for_event(eid)})")

        log(
            f"Starting SOME/IP-SD on {SD_MULTICAST_GROUP}:{SD_PORT} "
            f"via interface {interface_ip}"
        )
        sd = await construct_service_discovery(
            SD_MULTICAST_GROUP, SD_PORT, interface_ip
        )

        eventgroup = EventGroup(
            id=CABIN_EVENTGROUP_ID, event_ids=list(ALL_EVENT_IDS)
        )
        service = (
            ServiceBuilder()
            .with_service_id(CABIN_SERVICE_ID)
            .with_major_version(CABIN_MAJOR_VERSION)
            .with_eventgroup(eventgroup)
            .build()
        )

        log(
            f"Constructing ServerServiceInstance "
            f"(service=0x{CABIN_SERVICE_ID:04x}, instance=0x{CABIN_INSTANCE_ID:04x}, "
            f"endpoint={interface_ip}:{event_port}, ttl={SD_TTL_SECONDS}s)"
        )
        server = await construct_server_service_instance(
            service,
            instance_id=CABIN_INSTANCE_ID,
            endpoint=(ipaddress.IPv4Address(interface_ip), event_port),
            ttl=SD_TTL_SECONDS,
            sd_sender=sd,
            cyclic_offer_delay_ms=CYCLIC_OFFER_DELAY_MS,
            protocol=TransportLayerProtocol.UDP,
        )
        sd.attach(server)

        log("Starting cyclic SD offers (every "
            f"{CYCLIC_OFFER_DELAY_MS} ms)...")
        server.start_offer()

        try:
            log(
                "Publisher running. Drive values from the Kuksa CLI on VM2 "
                "(e.g. `publish Vehicle.Cabin.HVAC.AmbientAirTemperature 22.0`). "
                "Ctrl+C to stop."
            )
            paths = list(VSS_TO_EVENT.keys())
            async for updates in kuksa.subscribe_current_values(paths):
                for path, dp in updates.items():
                    if dp is None or dp.value is None:
                        continue
                    eid = VSS_TO_EVENT.get(path)
                    if eid is None:
                        continue
                    try:
                        payload = encode_event(eid, dp.value)
                    except Exception as exc:
                        log(f"ERROR encoding {path}={dp.value}: {exc}")
                        continue
                    try:
                        server.send_event(CABIN_EVENTGROUP_ID, eid, payload)
                    except Exception as exc:
                        log(f"ERROR sending event 0x{eid:04x} for {path}: {exc}")
                        continue
                    log(
                        f"FWD  {path} = {dp.value}  ->  someip event "
                        f"0x{eid:04x} ({len(payload)} B)"
                    )
        finally:
            log("Stopping cyclic SD offers...")
            try:
                await server.stop_offer()
            except Exception as exc:
                log(f"WARN stop_offer raised: {exc}")
            log("Closing service discovery...")
            try:
                sd.close()
            except Exception as exc:
                log(f"WARN sd.close() raised: {exc}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="SOME/IP publisher (VM2). Subscribes to VM2's local "
                    "Kuksa Databroker and forwards updates as SOME/IP "
                    "notification events to VM1's someip_client.py."
    )
    p.add_argument("--kuksa-host", default=DEFAULT_KUKSA_HOST,
                   help=f"Local Kuksa Databroker host (default: {DEFAULT_KUKSA_HOST})")
    p.add_argument("--kuksa-port", type=int, default=DEFAULT_KUKSA_PORT,
                   help=f"Local Kuksa Databroker port (default: {DEFAULT_KUKSA_PORT})")
    p.add_argument("--interface-ip", default=DEFAULT_VM2_INTERFACE_IP,
                   help="VM2 bridge interface IPv4 address used as the "
                        f"SOME/IP-SD source and event source "
                        f"(default: {DEFAULT_VM2_INTERFACE_IP})")
    p.add_argument("--port", type=int, default=DEFAULT_SERVER_EVENT_PORT,
                   help=f"UDP port for outbound SOME/IP events "
                        f"(default: {DEFAULT_SERVER_EVENT_PORT})")
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
