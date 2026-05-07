"""Shared SOME/IP service contract for the EV Range Extender Cabin service.

This module is the single source of truth for the SOME/IP wire model
shared between VM2 (publisher / server) and VM1 (subscriber / client).
Both `vm2/someip_publisher.py` and `vm1/someip_client.py` import their
service IDs, event IDs and payload codecs from here so they cannot
drift out of sync.

The blueprint at `eclipse-sdv-blueprint/README.md` calls the HPC <->
Zonal link "Eclipse SCore / SOME-IP". This module implements the
SOME/IP half of that link using `someipy` (a pure-Python SOME/IP +
SOME/IP-SD implementation). Wire output is real SOME/IP that a
Wireshark `someip` dissector decodes correctly.

SOME/IP-SD discovery defaults:

    multicast group : 224.224.224.245   (someipy default; matches the
                                         AUTOSAR R22-11 SOME/IP-SD examples)
    sd port         : udp/30490         (standard SOME/IP-SD port)

Service contract:

    service id      : 0xCAB0    (Cabin)
    instance id     : 0x0001
    major version   : 1
    eventgroup id   : 0x0001
    events          :
        0x8001  Vehicle.Cabin.HVAC.AmbientAirTemperature
                    payload: 4-byte big-endian IEEE-754 float (degC)
        0x8002  Vehicle.Cabin.Seat.Row1.DriverSide.Heating
                    payload: 1 signed byte (int8, percent  0..100)
        0x8003  Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
                    payload: 1 signed byte (int8, percent -100..100;
                                            negative = ventilation,
                                            positive = heating)

The event IDs are >= 0x8000 because AUTOSAR reserves the high bit for
notification (event) IDs vs RPC method IDs.
"""

from __future__ import annotations

import struct
from typing import Any, Callable


# --- SOME/IP-SD wire defaults -----------------------------------------

SD_MULTICAST_GROUP = "224.224.224.245"
SD_PORT = 30490

# Default UDP ports each side listens on for the *event* traffic
# itself (i.e. not the SD multicast). These are arbitrary; they just
# need to differ between the server and client when both run on the
# same loopback (which is fine on the QEMU bridge - VM1 and VM2 have
# distinct IPs, so any pair works).
DEFAULT_SERVER_EVENT_PORT = 30509   # VM2 (publisher) src port for events
DEFAULT_CLIENT_EVENT_PORT = 30510   # VM1 (subscriber) listen port for events

# Default interface IPs for the bridged QEMU lab (br0/192.168.100.0/24).
# Override on the command line if you re-IP your bridge.
DEFAULT_VM1_INTERFACE_IP = "192.168.100.10"   # subscriber / HPC
DEFAULT_VM2_INTERFACE_IP = "192.168.100.11"   # publisher  / Zonal


# --- Service / instance / eventgroup IDs ------------------------------

CABIN_SERVICE_ID    = 0xCAB0
CABIN_INSTANCE_ID   = 0x0001
CABIN_MAJOR_VERSION = 1
CABIN_EVENTGROUP_ID = 0x0001

# Cyclic offer interval for the publisher (ms). The default in the
# someipy examples is 2000 ms; matching that keeps subscription latency
# under a couple of seconds on a fresh start.
CYCLIC_OFFER_DELAY_MS = 2000

# TTL (s) attached to SD entries (offer / subscribe). 5 s is the
# someipy example default and is plenty short for a demo.
SD_TTL_SECONDS = 5


# --- Event IDs --------------------------------------------------------

EVENT_AMBIENT_TEMP   = 0x8001   # Vehicle.Cabin.HVAC.AmbientAirTemperature
EVENT_SEAT_HEAT      = 0x8002   # Vehicle.Cabin.Seat.Row1.DriverSide.Heating
EVENT_SEAT_HC        = 0x8003   # Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling

ALL_EVENT_IDS = (EVENT_AMBIENT_TEMP, EVENT_SEAT_HEAT, EVENT_SEAT_HC)


# --- VSS path <-> event ID mapping ------------------------------------
#
# The publisher subscribes to these VSS paths on the *local* (VM2)
# Kuksa Databroker and turns each update into a SOME/IP event. The
# client decodes the inbound events and writes the same VSS paths
# into the *local* (VM1) Kuksa Databroker. So the SOME/IP wire is
# strictly addressed by event-id; the VSS path is only used at the
# Kuksa edges of the bridge.

VSS_AMBIENT_TEMP = "Vehicle.Cabin.HVAC.AmbientAirTemperature"
VSS_SEAT_HEAT    = "Vehicle.Cabin.Seat.Row1.DriverSide.Heating"
VSS_SEAT_HC      = "Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling"

VSS_TO_EVENT = {
    VSS_AMBIENT_TEMP: EVENT_AMBIENT_TEMP,
    VSS_SEAT_HEAT:    EVENT_SEAT_HEAT,
    VSS_SEAT_HC:      EVENT_SEAT_HC,
}

EVENT_TO_VSS = {v: k for k, v in VSS_TO_EVENT.items()}


# --- Payload codecs ---------------------------------------------------
#
# SOME/IP payloads are big-endian. Each signal has a fixed encoding
# pinned by the AUTOSAR R22-11 convention and the COVESA VSS datatype:
#
#   AmbientAirTemperature  : VSS float        -> 4-byte big-endian float
#   Seat.Heating           : VSS int8 (0..100)-> 1 signed byte
#   Seat.HeatingCooling    : VSS int8 (-100..100) -> 1 signed byte
#
# The decoder returns Python-native types that drop straight into
# `kuksa_client.grpc.Datapoint(value)` without further coercion.

_FLOAT_STRUCT = struct.Struct(">f")   # 4 bytes
_INT8_STRUCT  = struct.Struct(">b")   # 1 byte signed


def _encode_temperature(value: Any) -> bytes:
    return _FLOAT_STRUCT.pack(float(value))


def _decode_temperature(payload: bytes) -> float:
    if len(payload) < _FLOAT_STRUCT.size:
        raise ValueError(
            f"AmbientAirTemperature payload too short: "
            f"got {len(payload)} bytes, need {_FLOAT_STRUCT.size}"
        )
    return float(_FLOAT_STRUCT.unpack_from(payload, 0)[0])


def _encode_int8(value: Any) -> bytes:
    iv = int(value)
    if iv < -128 or iv > 127:
        raise ValueError(f"int8 value out of range: {iv}")
    return _INT8_STRUCT.pack(iv)


def _decode_int8(payload: bytes) -> int:
    if len(payload) < _INT8_STRUCT.size:
        raise ValueError(
            f"int8 payload too short: got {len(payload)} bytes"
        )
    return int(_INT8_STRUCT.unpack_from(payload, 0)[0])


# Per-event codec table. Each entry: (encode_fn, decode_fn, unit_str).
_EVENT_CODECS = {
    EVENT_AMBIENT_TEMP: (_encode_temperature, _decode_temperature, "celsius"),
    EVENT_SEAT_HEAT:    (_encode_int8,        _decode_int8,        "percent"),
    EVENT_SEAT_HC:      (_encode_int8,        _decode_int8,        "percent"),
}


def encode_event(event_id: int, value: Any) -> bytes:
    """Encode a Python value into the SOME/IP payload for `event_id`."""
    try:
        encoder, _decoder, _unit = _EVENT_CODECS[event_id]
    except KeyError:
        raise KeyError(f"Unknown event id 0x{event_id:04x}")
    return encoder(value)


def decode_event(event_id: int, payload: bytes) -> Any:
    """Decode a SOME/IP payload for `event_id` back to a Python value."""
    try:
        _encoder, decoder, _unit = _EVENT_CODECS[event_id]
    except KeyError:
        raise KeyError(f"Unknown event id 0x{event_id:04x}")
    return decoder(payload)


def unit_for_event(event_id: int) -> str:
    return _EVENT_CODECS[event_id][2]


# --- Convenience -------------------------------------------------------

def event_id_for_vss(vss_path: str) -> int | None:
    """Return the SOME/IP event id for a VSS path, or None if unmapped."""
    return VSS_TO_EVENT.get(vss_path)


def vss_for_event_id(event_id: int) -> str | None:
    """Return the VSS path for a SOME/IP event id, or None if unmapped."""
    return EVENT_TO_VSS.get(event_id)


__all__ = [
    "SD_MULTICAST_GROUP",
    "SD_PORT",
    "DEFAULT_SERVER_EVENT_PORT",
    "DEFAULT_CLIENT_EVENT_PORT",
    "DEFAULT_VM1_INTERFACE_IP",
    "DEFAULT_VM2_INTERFACE_IP",
    "CABIN_SERVICE_ID",
    "CABIN_INSTANCE_ID",
    "CABIN_MAJOR_VERSION",
    "CABIN_EVENTGROUP_ID",
    "CYCLIC_OFFER_DELAY_MS",
    "SD_TTL_SECONDS",
    "EVENT_AMBIENT_TEMP",
    "EVENT_SEAT_HEAT",
    "EVENT_SEAT_HC",
    "ALL_EVENT_IDS",
    "VSS_AMBIENT_TEMP",
    "VSS_SEAT_HEAT",
    "VSS_SEAT_HC",
    "VSS_TO_EVENT",
    "EVENT_TO_VSS",
    "encode_event",
    "decode_event",
    "unit_for_event",
    "event_id_for_vss",
    "vss_for_event_id",
]
