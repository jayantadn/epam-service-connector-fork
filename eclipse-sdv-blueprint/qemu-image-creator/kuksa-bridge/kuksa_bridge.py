# Copyright (c) 2026 Eclipse Foundation.
#
# This program and the accompanying materials are made available under the
# terms of the MIT License which is available at
# https://opensource.org/licenses/MIT.
#
# SPDX-License-Identifier: MIT
"""kuksa-bridge: a current-value bridge between two Kuksa Databrokers over Zenoh.

This is the project's own "kuksa-bridge" component, the same role as the
arrow labelled `kuksa-bridge / eclipse-zenoh` in the Phase 1 architecture
diagram.

Why not the upstream ``zenoh-kuksa-provider``?
    The upstream provider in eclipse-kuksa/kuksa-incubation is built for
    the device-side actuator pattern (Kuksa side: subscribes to
    ``actuator_target``; Zenoh side: only consumes samples whose
    attachment is ``"currentValue"``). Two instances on two Databrokers
    therefore do NOT mirror current values to each other - which is
    exactly what our cabin-signal pipeline needs (VM2 Kuksa current ->
    VM1 Kuksa current). This module is a small, config-driven replacement
    that does mirror current values, while keeping the same JSON wire
    envelope ``zenoh_publisher.py`` / ``zenoh_client.py`` already use,
    so it can run side-by-side with the legacy pair on a different
    Zenoh port.

Roles
-----
The bridge as a whole is **bidirectional**: each VM runs one
``kuksa_bridge.py`` process that both publishes to and subscribes from
Zenoh, so cabin signals flow VM2->VM1 *and* battery / range signals
flow VM1->VM2 over the same Zenoh peer connection.

The role of an individual *signal* on a given VM is set per-signal in
the JSON config and tells the bridge whether it owns the writer side
of that signal locally:

* ``outbound``      Kuksa  -> Zenoh
                    The local Databroker is the source of truth for
                    this signal. Subscribe to its current value;
                    publish every change to Zenoh on key
                    ``<key_prefix>/<vss/path/with/slashes>``.

* ``inbound``       Zenoh  -> Kuksa
                    The local Databroker is a mirror for this signal.
                    Subscribe to ``<key_prefix>/**`` on Zenoh; for
                    each received sample whose ``path`` field matches
                    a configured ``inbound`` signal, write the value
                    into the local Databroker as a current value,
                    coerced to the signal's ``type``.

* ``bidirectional`` Both of the above. The inbound listener filters
                    out messages whose ``source`` field equals our own
                    ``source_label`` so we never echo our own writes
                    back into Kuksa.

Typical Phase 1 deployment (current scope: 3 cabin signals only)::

    VM1                                        VM2
    ----                                       ----
    Vehicle.Cabin.HVAC.                        Vehicle.Cabin.HVAC.
        AmbientAirTemperature : bidirectional      AmbientAirTemperature : bidirectional
    Vehicle.Cabin.Seat.Row1.                   Vehicle.Cabin.Seat.Row1.
        DriverSide.Heating    : bidirectional      DriverSide.Heating    : bidirectional
    Vehicle.Cabin.Seat.Row1.                   Vehicle.Cabin.Seat.Row1.
        DriverSide.HeatingCooling             :     DriverSide.HeatingCooling          :
                                bidirectional                                  bidirectional

Battery state and ``Vehicle.Powertrain.Range`` are intentionally NOT
on the bridge: nothing on VM2 currently consumes them, and adding
them only inflated the log. They can be put back at any time by
extending the two JSON configs - the bridge code itself does not
care about the specific paths.

The same wire envelope as ``zenoh_publisher.py`` is used so the legacy
``zenoh_client.py`` would also accept these messages if the key prefixes
overlapped::

    {
        "path":   "Vehicle.Cabin.HVAC.AmbientAirTemperature",
        "value":  50.0,
        "unit":   "percent",
        "timestamp": "2026-...Z",
        "source": "vm2"
    }

Configuration file
------------------
JSON. See ``bridge-config-vm1.json`` / ``bridge-config-vm2.json`` for
working examples used by the cloud-init deployment::

    {
      "kuksa": { "host": "127.0.0.1", "port": 55555 },
      "zenoh": {
        "mode": "peer",
        "listen":  ["tcp/0.0.0.0:7448"],
        "connect": []
      },
      "key_prefix": "ev-range/cabin",
      "source_label": "vm2",
      "signals": [
        {
          "path": "Vehicle.Cabin.HVAC.AmbientAirTemperature",
          "type": "float",
          "unit": "percent",
          "direction": "outbound"
        },
        ...
      ]
    }

Manual run (when the systemd unit is stopped)::

    sudo systemctl stop ev-range-kuksa-bridge
    cd /home/ubuntu/kuksa-bridge
    python3 kuksa_bridge.py --config /etc/kuksa-bridge/config.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import zenoh
from kuksa_client.grpc import Datapoint
from kuksa_client.grpc.aio import VSSClient


# ---------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------


VALID_DIRECTIONS = ("outbound", "inbound", "bidirectional")
VALID_TYPES = ("float", "int", "bool", "str")


def _coerce(raw: Any, vss_type: str) -> Any:
    """Coerce a JSON value to the configured VSS scalar type.

    Raises ValueError on any failure; the caller turns that into a WARN
    log line and drops the sample (same policy as the legacy bridge).
    """
    if vss_type == "float":
        return float(raw)
    if vss_type == "int":
        return int(round(float(raw)))
    if vss_type == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        raise ValueError(f"cannot coerce {raw!r} to bool")
    if vss_type == "str":
        return str(raw)
    raise ValueError(f"unsupported VSS type {vss_type!r}; expected one of {VALID_TYPES}")


@dataclass(frozen=True)
class SignalSpec:
    path: str            # canonical VSS path, e.g. "Vehicle.Cabin.HVAC.AmbientAirTemperature"
    vss_type: str        # one of VALID_TYPES
    unit: str            # free-text unit string, only used in the wire envelope
    direction: str       # one of VALID_DIRECTIONS

    @property
    def is_outbound(self) -> bool:
        return self.direction in ("outbound", "bidirectional")

    @property
    def is_inbound(self) -> bool:
        return self.direction in ("inbound", "bidirectional")


@dataclass(frozen=True)
class BridgeConfig:
    kuksa_host: str
    kuksa_port: int
    zenoh_mode: str            # "peer" or "client" (forwarded as-is to Zenoh)
    zenoh_listen: tuple[str, ...]
    zenoh_connect: tuple[str, ...]
    key_prefix: str            # Zenoh key prefix; we publish on f"{prefix}/<vss/path>"
    source_label: str          # embedded in outbound payloads ("vm1" / "vm2" / hostname)
    signals: tuple[SignalSpec, ...]

    @property
    def has_outbound(self) -> bool:
        return any(s.is_outbound for s in self.signals)

    @property
    def has_inbound(self) -> bool:
        return any(s.is_inbound for s in self.signals)


# ---------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------


def _vss_to_zenoh_key(prefix: str, vss_path: str) -> str:
    """Turn a VSS path into the Zenoh key the bridge uses for it."""
    return f"{prefix}/{vss_path.replace('.', '/')}"


def load_config(path: Path) -> BridgeConfig:
    raw = json.loads(path.read_text())

    kuksa = raw.get("kuksa") or {}
    zenoh_cfg = raw.get("zenoh") or {}

    signals_raw = raw.get("signals") or []
    if not signals_raw:
        raise ValueError("config has no 'signals' (nothing to bridge)")

    signals: list[SignalSpec] = []
    seen_paths: set[str] = set()
    for entry in signals_raw:
        path = entry.get("path")
        if not path or "." not in path:
            raise ValueError(f"signal entry missing/invalid 'path': {entry!r}")
        if path in seen_paths:
            raise ValueError(f"duplicate signal path {path!r}")
        seen_paths.add(path)

        vss_type = entry.get("type")
        if vss_type not in VALID_TYPES:
            raise ValueError(
                f"signal {path!r} has invalid 'type' {vss_type!r} "
                f"(expected one of {VALID_TYPES})"
            )

        direction = entry.get("direction", "bidirectional")
        if direction not in VALID_DIRECTIONS:
            raise ValueError(
                f"signal {path!r} has invalid 'direction' {direction!r} "
                f"(expected one of {VALID_DIRECTIONS})"
            )

        signals.append(SignalSpec(
            path=path,
            vss_type=vss_type,
            unit=entry.get("unit", ""),
            direction=direction,
        ))

    return BridgeConfig(
        kuksa_host=kuksa.get("host", "127.0.0.1"),
        kuksa_port=int(kuksa.get("port", 55555)),
        zenoh_mode=zenoh_cfg.get("mode", "peer"),
        zenoh_listen=tuple(zenoh_cfg.get("listen") or ()),
        zenoh_connect=tuple(zenoh_cfg.get("connect") or ()),
        key_prefix=raw.get("key_prefix", "ev-range/cabin"),
        source_label=raw.get("source_label", socket.gethostname()),
        signals=tuple(signals),
    )


# ---------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{ts}] [kuksa-bridge] {msg}", flush=True)


# ---------------------------------------------------------------------
# Zenoh session helper
# ---------------------------------------------------------------------


def build_zenoh_config(cfg: BridgeConfig) -> zenoh.Config:
    """Build a Zenoh config from BridgeConfig.

    ``listen`` / ``connect`` are passed through untouched - both can be
    empty (then Zenoh falls back to mDNS scouting), which is fine for
    tests but not used by the cloud-init deployment.
    """
    z = zenoh.Config()
    z.insert_json5("mode", json.dumps(cfg.zenoh_mode))
    if cfg.zenoh_listen:
        z.insert_json5("listen/endpoints", json.dumps(list(cfg.zenoh_listen)))
    if cfg.zenoh_connect:
        z.insert_json5("connect/endpoints", json.dumps(list(cfg.zenoh_connect)))
    return z


# ---------------------------------------------------------------------
# Inbound side (Zenoh -> Kuksa)
# ---------------------------------------------------------------------


class _InboundQueue:
    """Coalescing latest-value queue, identical in shape to the helper
    used by ``vm2/hvac_ecu.py`` / ``vm2/seat_ecu.py``.

    Producers (Zenoh worker thread) call ``offer(spec, raw_value, src)``;
    a single asyncio consumer drains the snapshot per loop tick and
    writes once per (path, distinct value) to Kuksa. Identical re-writes
    are dropped so the broker is not woken up unnecessarily.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[SignalSpec, Any, str]] = {}
        self._evt = asyncio.Event()

    def offer(self, spec: SignalSpec, raw_value: Any, src: str) -> None:
        with self._lock:
            self._pending[spec.path] = (spec, raw_value, src)
        self._loop.call_soon_threadsafe(self._evt.set)

    async def take(self) -> dict[str, tuple[SignalSpec, Any, str]]:
        while True:
            await self._evt.wait()
            with self._lock:
                if self._pending:
                    snapshot = self._pending
                    self._pending = {}
                    self._evt.clear()
                    return snapshot
                self._evt.clear()


async def _inbound_consumer(queue: _InboundQueue, kuksa: VSSClient) -> None:
    last_sent: dict[str, Any] = {}
    while True:
        pending = await queue.take()
        updates: dict[str, Datapoint] = {}
        log_lines: list[str] = []
        for path, (spec, raw_value, src) in pending.items():
            try:
                coerced = _coerce(raw_value, spec.vss_type)
            except (TypeError, ValueError) as exc:
                log(f"WARN cannot cast {raw_value!r} -> {spec.vss_type} for {path}: {exc}")
                continue
            if last_sent.get(path) == coerced:
                continue
            updates[path] = Datapoint(coerced)
            last_sent[path] = coerced
            log_lines.append(f"IN   {path} = {coerced} (from {src})")
        if not updates:
            continue
        try:
            await kuksa.set_current_values(updates)
        except Exception as exc:
            log(f"ERROR writing {len(updates)} key(s) to Kuksa: {exc}")
            continue
        for line in log_lines:
            log(line)


def _make_zenoh_listener(
    cfg: BridgeConfig,
    inbound_specs: dict[str, SignalSpec],
    queue: _InboundQueue,
):
    """Closure that the Zenoh subscriber will invoke per sample."""

    def listener(sample: zenoh.Sample) -> None:
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
            log(f"WARN payload missing 'path'/'value' on '{sample.key_expr}': {msg}")
            return
        # Loop-prevention for bidirectional setups. Without this, two
        # peers each running an outbound+inbound role on the same VSS
        # path would ping-pong: peer A writes X to its Kuksa, outbound
        # publishes X tagged source=A, peer B's inbound writes X to its
        # Kuksa, peer B's outbound sees the change and republishes X
        # tagged source=B, peer A's inbound writes X back into A's
        # Kuksa - one wasted round trip per change. The Kuksa
        # subscribe_current_values stream only fires on actual changes
        # so it does NOT escalate to an infinite loop, but the extra
        # writes are pointless. Drop self-tagged messages here so the
        # ping-pong never starts.
        if src == cfg.source_label:
            return
        spec = inbound_specs.get(path)
        if spec is None:
            # Not in this bridge's inbound whitelist - silently ignore.
            # (Could be a signal we only publish outbound, or noise.)
            return
        queue.offer(spec, value, src)

    return listener


# ---------------------------------------------------------------------
# Outbound side (Kuksa -> Zenoh)
# ---------------------------------------------------------------------


def _make_payload(spec: SignalSpec, value: Any, source: str) -> bytes:
    payload = {
        "path": spec.path,
        "value": float(value) if isinstance(value, (int, float)) else value,
        "unit": spec.unit,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    return json.dumps(payload).encode("utf-8")


async def _outbound_loop(
    cfg: BridgeConfig,
    outbound_specs: dict[str, SignalSpec],
    kuksa: VSSClient,
    publishers: dict[str, "zenoh.Publisher"],
) -> None:
    log(
        f"Outbound: subscribed to {len(outbound_specs)} VSS path(s) on local Kuksa - "
        f"forwarding current values to Zenoh under key prefix '{cfg.key_prefix}/...'"
    )
    last_fwd: dict[str, Any] = {}
    paths = list(outbound_specs.keys())
    async for updates in kuksa.subscribe_current_values(paths):
        forwarded: list[tuple[str, Any, int]] = []
        for path, dp in updates.items():
            if dp is None or dp.value is None:
                continue
            spec = outbound_specs.get(path)
            if spec is None:
                continue
            if last_fwd.get(path) == dp.value:
                continue
            try:
                payload = _make_payload(spec, dp.value, cfg.source_label)
                publishers[path].put(payload)
            except Exception as exc:
                log(f"ERROR forwarding {path}: {exc}")
                continue
            last_fwd[path] = dp.value
            forwarded.append((path, dp.value, len(payload)))
        if forwarded:
            summary = ", ".join(f"{p}={v} ({n}B)" for p, v, n in forwarded)
            log(f"OUT  {len(forwarded)} key(s) -> zenoh: {summary}")


# ---------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------


def _format_endpoints(prefix: str, eps: Iterable[str]) -> str:
    eps = list(eps)
    return f"{prefix}=[{', '.join(eps) if eps else '<none>'}]"


async def run(cfg: BridgeConfig) -> int:
    log(f"Connecting to local Kuksa Databroker at {cfg.kuksa_host}:{cfg.kuksa_port}...")
    log(f"Zenoh mode={cfg.zenoh_mode}, "
        f"{_format_endpoints('listen', cfg.zenoh_listen)}, "
        f"{_format_endpoints('connect', cfg.zenoh_connect)}")
    log(f"Key prefix : {cfg.key_prefix}")
    log(f"Source     : {cfg.source_label}")
    log(f"Signals    : {len(cfg.signals)} total "
        f"({sum(s.is_outbound for s in cfg.signals)} outbound, "
        f"{sum(s.is_inbound for s in cfg.signals)} inbound)")
    for spec in cfg.signals:
        zk = _vss_to_zenoh_key(cfg.key_prefix, spec.path)
        log(f"   - {spec.path}  ({spec.vss_type}, {spec.direction}) <-> {zk}")

    outbound_specs = {s.path: s for s in cfg.signals if s.is_outbound}
    inbound_specs = {s.path: s for s in cfg.signals if s.is_inbound}

    async with VSSClient(cfg.kuksa_host, cfg.kuksa_port) as kuksa:
        log("Connected to Kuksa.")

        loop = asyncio.get_running_loop()
        with zenoh.open(build_zenoh_config(cfg)) as session:
            log("Zenoh session open.")

            tasks: list[asyncio.Task] = []
            consumer_task: asyncio.Task | None = None
            subscriber = None  # keep the handle alive for the session's lifetime

            # ---- Inbound (Zenoh -> Kuksa) ------------------------------
            if inbound_specs:
                queue = _InboundQueue(loop)
                consumer_task = asyncio.create_task(_inbound_consumer(queue, kuksa))
                tasks.append(consumer_task)
                key_expr = f"{cfg.key_prefix}/**"
                listener = _make_zenoh_listener(cfg, inbound_specs, queue)
                subscriber = session.declare_subscriber(key_expr, listener)
                log(f"Inbound : subscribed to Zenoh '{key_expr}' "
                    f"({len(inbound_specs)} VSS path(s) whitelisted).")

            # ---- Outbound (Kuksa -> Zenoh) -----------------------------
            publishers: dict[str, zenoh.Publisher] = {}
            if outbound_specs:
                for path in outbound_specs:
                    zk = _vss_to_zenoh_key(cfg.key_prefix, path)
                    publishers[path] = session.declare_publisher(zk)
                tasks.append(asyncio.create_task(
                    _outbound_loop(cfg, outbound_specs, kuksa, publishers)
                ))

            if not tasks:
                log("Config has no signals to bridge - nothing to do, exiting.")
                return 0

            log("Bridge running. Ctrl+C to stop.")
            try:
                # Block on whichever finishes first; if any of them raises
                # we bubble it up rather than silently letting the bridge
                # half-die. ``stop_event`` would also work, but waiting on
                # the tasks gives us automatic propagation of the actual
                # error to the FATAL log path in main().
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                for t in done:
                    if t.exception() is not None:
                        raise t.exception()  # type: ignore[misc]
            finally:
                # zenoh.Session is closed by the `with` block; subscriber
                # / publishers are scoped to it and torn down cleanly.
                _ = subscriber
                _ = publishers
    return 0


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="kuksa-bridge: bridges current values between two Kuksa "
                    "Databrokers over Eclipse Zenoh. Single config-driven "
                    "service that supersedes the project's legacy "
                    "zenoh_publisher.py + zenoh_client.py pair."
    )
    p.add_argument("--config", "-c", required=True, type=Path,
                   help="Path to the JSON config file")
    p.add_argument("--validate-config", action="store_true",
                   help="Parse the config, print the parsed view and exit "
                        "(no Kuksa / Zenoh connection attempted).")
    return p.parse_args(argv)


def _print_validation_summary(cfg: BridgeConfig) -> None:
    print(f"Kuksa     : {cfg.kuksa_host}:{cfg.kuksa_port}")
    print(f"Zenoh     : mode={cfg.zenoh_mode}, "
          f"listen={list(cfg.zenoh_listen)}, "
          f"connect={list(cfg.zenoh_connect)}")
    print(f"KeyPrefix : {cfg.key_prefix}")
    print(f"Source    : {cfg.source_label}")
    print(f"Signals   : {len(cfg.signals)} total "
          f"({sum(s.is_outbound for s in cfg.signals)} outbound, "
          f"{sum(s.is_inbound for s in cfg.signals)} inbound)")
    for spec in cfg.signals:
        zk = _vss_to_zenoh_key(cfg.key_prefix, spec.path)
        print(f"  - [{spec.direction:>13}] {spec.path}  "
              f"({spec.vss_type})  <->  {zk}")


def main() -> int:
    args = parse_args()
    try:
        cfg = load_config(args.config)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"[kuksa-bridge] FATAL: invalid config {args.config}: {exc}",
              file=sys.stderr, flush=True)
        return 2

    if args.validate_config:
        _print_validation_summary(cfg)
        return 0

    try:
        return asyncio.run(run(cfg))
    except KeyboardInterrupt:
        log("Stopping.")
        return 0
    except Exception as exc:
        log(f"FATAL: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
