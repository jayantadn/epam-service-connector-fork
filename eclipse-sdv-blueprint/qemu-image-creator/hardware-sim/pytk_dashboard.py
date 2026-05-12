"""Hardware Simulator dashboard - runs on the host (PyTk).

Tk GUI that replaces the manual Kuksa CLI workflow during the EV
Range Extender demo. Sliders for the battery + fan speed and on/off
toggles for seat heating/cooling, all driven by one Zenoh session
that dials the three ECUs:

    sim/battery/voltage     ->  bms.py       VM1   (tcp/192.168.100.10:7460)
    sim/battery/current     ->  bms.py       VM1   (same)
    sim/battery/soc         ->  bms.py       VM1   (same)
    sim/cabin/fan-speed     ->  hvac_ecu.py  VM2   (tcp/192.168.100.11:7461)
    sim/cabin/seat/heating  ->  seat_ecu.py  VM2   (tcp/192.168.100.11:7462)
    sim/cabin/seat/hc       ->  seat_ecu.py  VM2   (same)

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
from tkinter import BooleanVar, Frame, IntVar, StringVar, Tk, ttk
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
    `vmin/vmax` slider range (ignored when `is_toggle`)
    `step`      spinbox step / slider resolution (ignored when `is_toggle`)
    `default`   initial value
    `is_int`    True for VSS int8/uint8 signals
    `is_toggle` True to render as a checkbox instead of a slider+spinbox.
                When set, the row publishes `on_value` when the checkbox
                is ticked and `off_value` when it is unticked.
    `on_value`  value sent on toggle ON  (only when `is_toggle`)
    `off_value` value sent on toggle OFF (only when `is_toggle`)
    `group`     non-empty string for mutually-exclusive toggle groups.
                Turning any toggle ON in a group automatically turns OFF
                every other toggle that shares the same `group` value
                (and publishes their `off_value`, so the receiving ECU
                also sees the change). Ignored for sliders.
    """

    display: str
    key: str
    unit: str
    vmin: float
    vmax: float
    step: float
    default: float
    is_int: bool = False
    is_toggle: bool = False
    on_value: float = 0
    off_value: float = 0
    group: str = ""


# ---------------------------------------------------------------------
# Signal catalogue. Keep this in lockstep with:
#   - vm1/bms.py        KEY_TO_VSS
#   - vm2/hvac_ecu.py   KEY_TO_VSS
#   - vm2/seat_ecu.py   KEY_TO_VSS
# ---------------------------------------------------------------------
#
# Battery ranges reflect a typical passenger EV high-voltage pack
# (~400 V Li-ion, ~75 kWh): nominal voltage band 320..420 V; current
# 0..200 A on traction discharge only (regen / charging is intentionally
# not exposed on the dashboard - all dashboard inputs are non-negative).

BATTERY_SIGNALS = (
    Signal("Battery Voltage",      "sim/battery/voltage", "V",  320.0, 420.0, 1.0, 400.0),
    Signal("Battery Current",      "sim/battery/current", "A",    0.0, 200.0, 1.0,  25.0),
    Signal("Battery %",            "sim/battery/soc",     "%",    0.0, 100.0, 1.0,  80.0),
)
# HVAC: a single Fan Speed slider (0..100 %). Drives the AC blower load
# in the Range AI consumption model. Replaces the old "Cabin Ambient
# Temp" slider so the dashboard maps 1:1 to controls a real driver
# would touch.
HVAC_SIGNALS = (
    Signal("Fan Speed",            "sim/cabin/fan-speed", "%",    0.0, 100.0, 1.0,   0.0,
           is_int=True),
)
# Seat: simple on/off toggles. Heating ON publishes 100 %, Cooling ON
# publishes -100 % (the Heating/Cooling VSS path is bidirectional;
# negative = ventilation/cooling). Both belong to the same `group`,
# making them mutually exclusive on the dashboard - turning Heating ON
# auto-clears Cooling and vice versa, and the cleared toggle's
# off_value is published so the ECU on VM2 also stops the action.
SEAT_SIGNALS = (
    Signal("Seat Heating",         "sim/cabin/seat/heating", "",  0,   100, 1, 0,
           is_int=True, is_toggle=True, on_value=100,  off_value=0,
           group="seat-thermal"),
    Signal("Seat Cooling",         "sim/cabin/seat/hc",      "", -100, 100, 1, 0,
           is_int=True, is_toggle=True, on_value=-100, off_value=0,
           group="seat-thermal"),
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
    """One row in the dashboard - a slider+spinbox or a checkbox toggle.

    Which widget set is rendered is decided by `Signal.is_toggle`:
      * False (default) -> slider + numeric spinbox + unit label.
      * True            -> single checkbox; ticking publishes
                           `Signal.on_value`, unticking publishes
                           `Signal.off_value`.
    """

    def __init__(self, parent: Frame, sig: Signal, publish: Callable[[Signal, float], None]) -> None:
        self.sig = sig
        self.publish = publish
        self._building = True

        ttk.Label(parent, text=sig.display, width=24).grid(row=0, column=0, sticky="w", padx=(8, 4), pady=4)

        if sig.is_toggle:
            self._build_toggle(parent)
        else:
            self._build_slider(parent)

        parent.columnconfigure(1, weight=1)
        self._building = False

    # -- toggle (checkbox) ------------------------------------------------
    def _build_toggle(self, parent: Frame) -> None:
        self.bool_var = BooleanVar(value=bool(self.sig.default))
        self.check = ttk.Checkbutton(
            parent,
            text="ON",
            variable=self.bool_var,
            command=self._on_toggle,
        )
        # Span the slider + spinbox + unit columns so the layout stays
        # aligned with the slider rows above.
        self.check.grid(row=0, column=1, columnspan=3, sticky="w", padx=4, pady=4)

    def _on_toggle(self) -> None:
        if self._building:
            return
        value = self.sig.on_value if self.bool_var.get() else self.sig.off_value
        if self.sig.is_int:
            value = int(round(value))
        self.publish(self.sig, value)

    def force_toggle_off(self) -> bool:
        """Programmatically clear the checkbox without firing _on_toggle.

        Returns True if the toggle was actually ON before the call (so
        the caller can decide whether to publish the off_value), False
        if it was already off / not a toggle.
        """
        if not self.sig.is_toggle:
            return False
        was_on = bool(self.bool_var.get())
        if was_on:
            # BooleanVar.set() does NOT trigger ttk.Checkbutton.command,
            # so we are free to mutate state without recursion.
            self.bool_var.set(False)
        return was_on

    # -- slider + spinbox -------------------------------------------------
    def _build_slider(self, parent: Frame) -> None:
        sig = self.sig
        self.var = IntVar(value=int(round(sig.default))) if sig.is_int else None
        self._float = sig.default

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

        self.status_var = StringVar(value="Ready. Move a slider or click a toggle to publish.")

        # Track every SignalRow we create so _publish() can implement
        # toggle-group mutual exclusion (see _enforce_toggle_group).
        self._rows: list[tuple[Signal, SignalRow]] = []

        for section_title, sigs in ALL_SECTIONS:
            frame = ttk.LabelFrame(root, text=section_title, padding=(8, 6))
            frame.pack(fill="x", expand=False, padx=10, pady=(8, 0))
            for sig in sigs:
                row = Frame(frame)
                row.pack(fill="x", expand=True)
                self._rows.append((sig, SignalRow(row, sig, self._publish)))

        # Status bar
        status = ttk.Frame(root, padding=(8, 4))
        status.pack(fill="x", side="bottom")
        ttk.Label(status, textvariable=self.status_var, anchor="w").pack(fill="x", expand=True)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Publish + UI side-effects
    # ------------------------------------------------------------------
    def _emit(self, sig: Signal, value: float | int) -> None:
        """Send to the Zenoh bus only - no UI side effects, no recursion."""
        self.bus.put(sig.key, value)

    def _enforce_toggle_group(self, sig: Signal, value: float | int) -> list[str]:
        """If `sig` is a toggle going ON in a non-empty group, switch
        every other ON toggle in the same group to OFF and publish their
        off_value to the bus. Returns the human-readable display names of
        the toggles that were auto-cleared (for the status bar)."""
        cleared: list[str] = []
        if not (sig.is_toggle and sig.group and value == sig.on_value):
            return cleared
        for other_sig, other_row in self._rows:
            if other_sig is sig:
                continue
            if not other_sig.is_toggle or other_sig.group != sig.group:
                continue
            if other_row.force_toggle_off():
                # Was ON, now we cleared the box - tell the ECU too.
                self._emit(other_sig, other_sig.off_value)
                cleared.append(other_sig.display)
        return cleared

    def _publish(self, sig: Signal, value: float | int) -> None:
        try:
            cleared = self._enforce_toggle_group(sig, value)
            self._emit(sig, value)

            ts = datetime.now().strftime("%H:%M:%S")
            unit = sig.unit
            if sig.is_toggle:
                state = "ON" if value == sig.on_value else "OFF"
                msg = f"[{ts}]  PUT {sig.key} = {state} ({int(value)})"
                if cleared:
                    msg += "  | auto-OFF: " + ", ".join(cleared)
            elif sig.is_int:
                msg = f"[{ts}]  PUT {sig.key} = {int(value)} {unit}"
            else:
                msg = f"[{ts}]  PUT {sig.key} = {value:.2f} {unit}"
            self.status_var.set(msg)
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
