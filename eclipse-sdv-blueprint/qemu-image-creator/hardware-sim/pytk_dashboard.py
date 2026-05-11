"""Hardware Simulator dashboard - runs on the host (PyTk).

Tk GUI that replaces the manual Kuksa CLI workflow during the EV
Range Extender demo. Six sliders + numeric spinboxes, one Zenoh
session that dials the three ECUs:

    sim/battery/voltage   ->  bms.py  on VM1   (tcp/192.168.100.10:7460)
    sim/battery/current   ->  bms.py  on VM1   (same)
    sim/battery/soc       ->  bms.py  on VM1   (same)
    sim/cabin/temp        ->  hvac_ecu.py  on VM2   (tcp/192.168.100.11:7461)
    sim/cabin/seat/heating-> seat_ecu.py  on VM2   (tcp/192.168.100.11:7462)
    sim/cabin/seat/hc     -> seat_ecu.py  on VM2   (same)

Each Zenoh sample is a tiny JSON payload:
    {"value": <number>, "source": "<host>", "ts": "<iso>"}

The ECUs decode the JSON and write the value into their local Kuksa
Databroker. From there `range_ai.py` recomputes the range exactly as
before; the only thing that has changed is the input layer.

Requirements (host):
    - Python 3 with Tk (the `tkinter` stdlib module - usually
      preinstalled on Linux/macOS; Windows ships with it).
    - `eclipse-zenoh` (pip install eclipse-zenoh).
    - Network reachability to 192.168.100.10/11 (the QEMU bridge IP
      192.168.100.1/24 set up by `setup.sh`).

Usage:
    python3 pytk_dashboard.py
    python3 pytk_dashboard.py --vm1 192.168.100.10 --vm2 192.168.100.11
    python3 pytk_dashboard.py --bms-port 7460 --hvac-port 7461 --seat-port 7462
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from tkinter import Frame, IntVar, StringVar, Tk, ttk
from typing import Callable

import zenoh


DEFAULT_VM1_IP = "192.168.100.10"
DEFAULT_VM2_IP = "192.168.100.11"
DEFAULT_BMS_PORT = 7460
DEFAULT_HVAC_PORT = 7461
DEFAULT_SEAT_PORT = 7462


@dataclass
class Signal:
    """One row in the dashboard.

    `display`   user-facing label
    `key`       Zenoh key the ECU subscribes to
    `unit`      shown after the value
    `vmin/vmax` slider range
    `step`      spinbox step (also slider resolution)
    `default`   initial value
    `is_int`    True for VSS int8 signals (Heating, HeatingCooling)
    """

    display: str
    key: str
    unit: str
    vmin: float
    vmax: float
    step: float
    default: float
    is_int: bool = False


# ---------------------------------------------------------------------
# Signal catalogue. Keep this in lockstep with:
#   - vm1/bms.py        KEY_TO_VSS
#   - vm2/hvac_ecu.py   KEY_TO_VSS
#   - vm2/seat_ecu.py   KEY_TO_VSS
# ---------------------------------------------------------------------

BATTERY_SIGNALS = (
    Signal("Battery Voltage",          "sim/battery/voltage", "V",  200.0, 450.0, 1.0, 400.0),
    Signal("Battery Current",          "sim/battery/current", "A", -200.0, 300.0, 0.5,  25.5),
    Signal("Battery SoC",              "sim/battery/soc",     "%",    0.0, 100.0, 1.0,  80.0),
)
HVAC_SIGNALS = (
    Signal("Cabin Ambient Temp",       "sim/cabin/temp",      "\u00b0C", -30.0, 50.0, 0.5, 22.0),
)
SEAT_SIGNALS = (
    Signal("Seat Heating",             "sim/cabin/seat/heating", "%",  0,   100, 1, 0,   is_int=True),
    Signal("Seat Heating-Cooling",     "sim/cabin/seat/hc",      "%", -100, 100, 1, 0,   is_int=True),
)

ALL_SECTIONS = (
    ("Battery (VM1 - bms.py)", BATTERY_SIGNALS),
    ("Cabin HVAC (VM2 - hvac_ecu.py)", HVAC_SIGNALS),
    ("Cabin Seat (VM2 - seat_ecu.py)", SEAT_SIGNALS),
)


# ---------------------------------------------------------------------
# Zenoh wrapper
# ---------------------------------------------------------------------


class ZenohBus:
    """Holds the Zenoh session and one publisher per key.

    Uses peer mode dialing the three ECU TCP listeners. The session is
    opened lazily on the first `put` so an unreachable ECU does not
    block GUI startup; failed publishes are reported on the status bar.
    """

    def __init__(self, endpoints: list[str]) -> None:
        self.endpoints = endpoints
        self.source = socket.gethostname()
        self._session: zenoh.Session | None = None
        self._publishers: dict[str, zenoh.Publisher] = {}
        self._lock = threading.Lock()

    def _ensure(self) -> zenoh.Session:
        with self._lock:
            if self._session is not None:
                return self._session
            cfg = zenoh.Config()
            cfg.insert_json5("connect/endpoints", json.dumps(self.endpoints))
            cfg.insert_json5("listen/endpoints", '["tcp/0.0.0.0:0"]')
            self._session = zenoh.open(cfg)
            return self._session

    def put(self, key: str, value: float | int) -> None:
        session = self._ensure()
        with self._lock:
            pub = self._publishers.get(key)
            if pub is None:
                pub = session.declare_publisher(key)
                self._publishers[key] = pub
        payload = json.dumps({
            "value": value,
            "source": self.source,
            "ts": datetime.now(timezone.utc).isoformat(),
        }).encode("utf-8")
        pub.put(payload)

    def close(self) -> None:
        with self._lock:
            for pub in self._publishers.values():
                try:
                    pub.undeclare()
                except Exception:
                    pass
            self._publishers.clear()
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None


# ---------------------------------------------------------------------
# Tk widgets
# ---------------------------------------------------------------------


class SignalRow:
    """One slider + spinbox pair, wired to a publish callback."""

    def __init__(self, parent: Frame, sig: Signal, publish: Callable[[Signal, float], None]) -> None:
        self.sig = sig
        self.publish = publish
        self.var = IntVar(value=int(round(sig.default))) if sig.is_int else None
        self._float = sig.default
        self._building = True

        ttk.Label(parent, text=sig.display, width=24).grid(row=0, column=0, sticky="w", padx=(8, 4), pady=4)

        # Slider
        self.scale = ttk.Scale(
            parent,
            from_=sig.vmin,
            to=sig.vmax,
            orient="horizontal",
            command=self._on_scale,
            length=320,
        )
        self.scale.set(sig.default)
        self.scale.grid(row=0, column=1, sticky="we", padx=4, pady=4)

        # Spinbox (numeric) - shows / lets user type a precise value
        increment = sig.step if sig.step else (1 if sig.is_int else 0.1)
        if sig.is_int:
            self.spin = ttk.Spinbox(
                parent,
                from_=sig.vmin,
                to=sig.vmax,
                increment=increment,
                width=8,
                command=self._on_spin,
                textvariable=self.var,
            )
            self.var.set(int(round(sig.default)))
        else:
            self.spin = ttk.Spinbox(
                parent,
                from_=sig.vmin,
                to=sig.vmax,
                increment=increment,
                width=8,
                command=self._on_spin,
            )
            self.spin.set(f"{sig.default:.2f}")
        self.spin.bind("<Return>", lambda _e: self._on_spin())
        self.spin.bind("<FocusOut>", lambda _e: self._on_spin())
        self.spin.grid(row=0, column=2, sticky="e", padx=4, pady=4)

        ttk.Label(parent, text=sig.unit, width=4).grid(row=0, column=3, sticky="w", padx=(0, 8), pady=4)

        parent.columnconfigure(1, weight=1)
        self._building = False

    def _on_scale(self, raw: str) -> None:
        if self._building:
            return
        try:
            value = float(raw)
        except ValueError:
            return
        if self.sig.is_int:
            value = int(round(value))
            if self.var is not None and self.var.get() != value:
                self.var.set(value)
        else:
            value = round(value, 2)
            try:
                self.spin.delete(0, "end")
                self.spin.insert(0, f"{value:.2f}")
            except Exception:
                pass
        self._float = value
        self.publish(self.sig, value)

    def _on_spin(self) -> None:
        if self._building:
            return
        try:
            value = float(self.spin.get())
        except (ValueError, TypeError):
            return
        if self.sig.is_int:
            value = int(round(value))
        value = max(self.sig.vmin, min(self.sig.vmax, value))
        if self._float == value:
            return
        self._float = value
        self.scale.set(value)
        self.publish(self.sig, value)


class Dashboard:
    def __init__(self, root: Tk, bus: ZenohBus) -> None:
        self.root = root
        self.bus = bus
        self.root.title("EV Range Extender - Hardware Simulator")
        self.root.geometry("640x520")
        # WSLg ships without an X cursor theme by default, which makes the
        # pointer disappear over Tk windows. Pinning the built-in X11
        # cursor "left_ptr" forces the server to render the fallback
        # bitmap that is always available, so the pointer stays visible
        # regardless of XCURSOR_THEME / Wayland config on the host.
        try:
            self.root.config(cursor="left_ptr")
        except Exception:
            pass
        # WSLg also routinely launches new windows BEHIND the terminal
        # that spawned them, leaving the user wondering where their
        # dashboard went. Force the window to the front, grab focus,
        # then drop the topmost flag so it does not stay glued above
        # everything else once the user has acknowledged it.
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(800, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        except Exception:
            pass

        self.status_var = StringVar(value="Ready. Move a slider to publish.")

        for section_title, sigs in ALL_SECTIONS:
            frame = ttk.LabelFrame(root, text=section_title, padding=(8, 6))
            frame.pack(fill="x", expand=False, padx=10, pady=(8, 0))
            for sig in sigs:
                row = Frame(frame)
                row.pack(fill="x", expand=True)
                SignalRow(row, sig, self._publish)

        # Status bar
        status = ttk.Frame(root, padding=(8, 4))
        status.pack(fill="x", side="bottom")
        ttk.Label(status, textvariable=self.status_var, anchor="w").pack(fill="x", expand=True)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _publish(self, sig: Signal, value: float | int) -> None:
        try:
            self.bus.put(sig.key, value)
            ts = datetime.now().strftime("%H:%M:%S")
            unit = sig.unit
            if sig.is_int:
                self.status_var.set(f"[{ts}]  PUT {sig.key} = {int(value)} {unit}")
            else:
                self.status_var.set(f"[{ts}]  PUT {sig.key} = {value:.2f} {unit}")
        except Exception as exc:
            ts = datetime.now().strftime("%H:%M:%S")
            self.status_var.set(f"[{ts}]  ERROR publishing {sig.key}: {exc}")

    def _on_close(self) -> None:
        try:
            self.bus.close()
        finally:
            self.root.destroy()


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Hardware Simulator dashboard for the EV Range Extender. "
                    "Runs on the host. Publishes slider values over Zenoh to "
                    "the BMS / HVAC / Seat ECUs running on VM1 and VM2."
    )
    p.add_argument("--vm1", default=DEFAULT_VM1_IP, help=f"VM1 bridge IP (default: {DEFAULT_VM1_IP})")
    p.add_argument("--vm2", default=DEFAULT_VM2_IP, help=f"VM2 bridge IP (default: {DEFAULT_VM2_IP})")
    p.add_argument("--bms-port", type=int, default=DEFAULT_BMS_PORT,
                   help=f"VM1 BMS Zenoh port (default: {DEFAULT_BMS_PORT})")
    p.add_argument("--hvac-port", type=int, default=DEFAULT_HVAC_PORT,
                   help=f"VM2 HVAC Zenoh port (default: {DEFAULT_HVAC_PORT})")
    p.add_argument("--seat-port", type=int, default=DEFAULT_SEAT_PORT,
                   help=f"VM2 Seat Zenoh port (default: {DEFAULT_SEAT_PORT})")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    endpoints = [
        f"tcp/{args.vm1}:{args.bms_port}",
        f"tcp/{args.vm2}:{args.hvac_port}",
        f"tcp/{args.vm2}:{args.seat_port}",
    ]

    print(f"[pytk] dialing Zenoh endpoints: {endpoints}", flush=True)

    bus = ZenohBus(endpoints)
    root = Tk()
    Dashboard(root, bus)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
