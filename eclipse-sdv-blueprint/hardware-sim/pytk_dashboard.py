"""Hardware Simulator dashboard - runs on the host (PyTk).

Tk GUI that replaces the manual Kuksa CLI workflow during the EV
Range Extender demo. Sliders + numeric spinboxes for the analogue
signals, plus a pair of mutually-exclusive toggles for seat
heating / cooling, all driven through one Zenoh session that dials
the three ECUs on the two VMs:

    sim/battery/voltage    ->  bms.py       on VM1  (tcp/192.168.100.10:7460)
    sim/battery/current    ->  bms.py       on VM1  (same)
    sim/battery/soc        ->  bms.py       on VM1  (same)        # labelled "Battery %"
    sim/cabin/temp         ->  hvac_ecu.py  on VM2  (tcp/192.168.100.11:7461)
                                                                  # labelled "Fan Speed" (cosmetic only;
                                                                  # the underlying ECU/VSS path is
                                                                  # unchanged on the VM)
    sim/cabin/seat/heating ->  seat_ecu.py  on VM2  (tcp/192.168.100.11:7462)
    sim/cabin/seat/hc      ->  seat_ecu.py  on VM2  (same)        # negative = cooling

Each Zenoh sample is a tiny JSON payload:
    {"value": <number>, "source": "<host>", "ts": "<iso>"}

The ECUs decode the JSON and write the value into their local Kuksa
Databroker. From there `range_ai.py` recomputes the remaining range.

Reverse channel (NEW):
    The HVAC ECU and Seat Control Module each subscribe to their
    OWN local Kuksa for the VSS path they own, and on every change
    push a tiny key/value status envelope back to this dashboard
    over Zenoh on:

        dash/status/hvac   <-  hvac_ecu.py
        dash/status/seat   <-  seat_ecu.py

    Envelope (one logical signal per message):
        {"key":   "hvac.fan_speed"   |
                  "seat.heating"     |
                  "seat.heating_cooling",
         "value": <number>,
         "status": "on" | "off" | "heating" | "cooling",
         "source": "vm2",
         "ts":    "<iso>"}

    The dashboard's `IndicatorPanel` maps reverse-channel `value` /
    `status` (and the dashboard's own toggle state) to colored LEDs.
    The seat lane is split into TWO independent LEDs (heating +
    cooling) because they are physically different actuators and
    visualising them in one bulb hides cases where the EV-app on VM1
    drives the two channels in lockstep:

        HVAC          : status="on"                      -> green
                        status="off"                     -> red

        Seat Heating  : dashboard toggle on              -> red
                        seat.heating         value != 0  -> red
                        seat.heating_cooling value >  0  -> red
                                                            (VSS:
                                                             HC > 0 =
                                                             heating)
                        otherwise                        -> grey

        Seat Cooling  : dashboard toggle on              -> blue
                        seat.heating_cooling value != 0  -> blue
                        otherwise                        -> grey

    The Cooling LED's "value != 0" rule is intentionally broader than
    strict VSS (which would only treat HC<0 as cooling), so the LED
    also lights up when the EV Range Extender prototype on
    `playground.digital.auto` writes `HeatingCooling = 1` to mean
    "seat module engaged". In that flow the Heating LED is also red
    (because HC > 0 == heating per VSS), and both LEDs being on side
    by side mirrors the EV-app's intent.

    The whole point of the reverse channel is that writes made by
    the EV Range Extender app on VM1 (which travel VM1 -> VM2 via
    the kuksa-bridge) become visible on the host dashboard without
    needing to query Kuksa directly.

Plausibility / UX rules baked into the catalogue below:
    - All inputs are non-negative on the slider/spinbox side (current
      cannot be entered as a negative number).
    - Battery voltage / current ranges match a typical passenger EV
      (320-420 V pack, 0-200 A draw).
    - Seat heating and cooling are toggles, not sliders, and turning
      one on automatically turns the other off (mutual exclusion is
      enforced in the GUI before the publish).

Requirements (host):
    - Python 3 with Tk (the `tkinter` stdlib module - usually
      preinstalled on Linux/macOS; Windows ships with it).
    - `eclipse-zenoh` (pip install eclipse-zenoh).
    - Network reachability to 192.168.100.10/11 (the QEMU bridge IP
      192.168.100.1/24 set up by `setup.py` / `setup.sh`).

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
from tkinter import BooleanVar, Canvas, Frame, IntVar, StringVar, Tk, ttk
from typing import Callable, Optional

import zenoh


DEFAULT_VM1_IP = "192.168.100.10"
DEFAULT_VM2_IP = "192.168.100.11"
DEFAULT_BMS_PORT = 7460
DEFAULT_HVAC_PORT = 7461
DEFAULT_SEAT_PORT = 7462

# Reverse-channel keys the dashboard subscribes to. Kept in sync with
# DASH_STATUS_KEY on the matching ECU (vm2/hvac_ecu.py, vm2/seat_ecu.py).
STATUS_KEY_HVAC = "dash/status/hvac"
STATUS_KEY_SEAT = "dash/status/seat"

# Color palette for the indicator LEDs.
INDICATOR_COLORS = {
    "green":  "#2ecc71",
    "red":    "#e74c3c",
    "blue":   "#3498db",
    "grey":   "#7f8c8d",
}


@dataclass
class Signal:
    """One row in the dashboard.

    `display`     user-facing label
    `key`         Zenoh key the ECU subscribes to
    `unit`        shown after the value
    `vmin/vmax`   slider range (ignored for toggles)
    `step`        spinbox step (also slider resolution)
    `default`     initial value
    `is_int`      True for VSS int8 signals (Heating, HeatingCooling)
    `is_toggle`   render as on/off checkbox instead of slider+spinbox
    `on_value`    value published when a toggle is switched on
    `off_value`   value published when a toggle is switched off
    `mutex_with`  Zenoh key of a sibling toggle to force-off when this
                  toggle turns on (used to make Heating / Cooling
                  mutually exclusive on the seat row)
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
    on_value: float = 1.0
    off_value: float = 0.0
    mutex_with: Optional[str] = None


# ---------------------------------------------------------------------
# Signal catalogue. Keep this in lockstep with:
#   - vm1/bms.py        KEY_TO_VSS
#   - vm2/hvac_ecu.py   KEY_TO_VSS
#   - vm2/seat_ecu.py   KEY_TO_VSS
#
# Plausibility for a passenger EV:
#   - Voltage 320..420 V is the working range of a typical 350 V
#     class pack (Tesla Model 3 / Ioniq 5 low-voltage variant /
#     e-Golf are all in this band).
#   - Current 0..200 A covers cruise (~30 A) up to hard acceleration
#     (~150-200 A). Negative regen current is intentionally NOT
#     exposed on the input UI - the user cannot enter a negative
#     number into the spinbox.
#   - Battery % is shown verbatim (was "Battery SoC"); the underlying
#     Zenoh key (`sim/battery/soc`) and downstream VSS path are
#     unchanged so VM-side ECUs do not need to be touched.
#   - The HVAC slider is labelled "Fan Speed" (was "Cabin Ambient
#     Temp"). This is a dashboard-only relabel: the Zenoh key
#     (`sim/cabin/temp`) and HVAC ECU on VM2 are deliberately left
#     untouched, so the existing pipeline keeps working.
# ---------------------------------------------------------------------

BATTERY_SIGNALS = (
    Signal("Battery Voltage",          "sim/battery/voltage", "V", 320.0, 420.0, 1.0, 380.0),
    Signal("Battery Current",          "sim/battery/current", "A",   0.0, 200.0, 1.0,  30.0),
    Signal("Battery %",                "sim/battery/soc",     "%",   0.0, 100.0, 1.0,  80.0),
)
HVAC_SIGNALS = (
    Signal("Fan Speed",                "sim/cabin/temp",      "%",   0,   100,   1,   0, is_int=True),
)
# Seat heating and cooling are mutually-exclusive toggles. Internally
# they map to the canonical VSS signals already in use:
#   - Seat Heating ON  -> Vehicle.Cabin.Seat.Row1.DriverSide.Heating       = 100
#   - Seat Cooling ON  -> Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling = -100
# Each toggle's `mutex_with` field points at the other toggle's Zenoh
# key so the Dashboard can force the partner OFF before publishing.
SEAT_SIGNALS = (
    Signal("Seat Heating", "sim/cabin/seat/heating", "", 0,    100, 1, 0,
           is_int=True, is_toggle=True, on_value=100,  off_value=0,
           mutex_with="sim/cabin/seat/hc"),
    Signal("Seat Cooling", "sim/cabin/seat/hc",      "", -100, 0,   1, 0,
           is_int=True, is_toggle=True, on_value=-100, off_value=0,
           mutex_with="sim/cabin/seat/heating"),
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

    The same session is reused for reverse-channel subscriptions
    (`subscribe`), so ECU -> host samples ride the existing TCP peer
    connections without needing a second Zenoh process or extra ports.
    """

    def __init__(self, endpoints: list[str]) -> None:
        self.endpoints = endpoints
        self.source = socket.gethostname()
        self._session: zenoh.Session | None = None
        self._publishers: dict[str, zenoh.Publisher] = {}
        self._subscribers: list[zenoh.Subscriber] = []
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

    def subscribe(self, key_expr: str,
                  callback: Callable[[str, dict], None]) -> None:
        """Subscribe to a Zenoh key/key-expr and invoke `callback`
        from the Zenoh worker thread for each sample.

        The callback receives `(key_str, parsed_json_dict)`. JSON
        parse failures are silently dropped (a malformed message
        from an ECU should not crash the GUI thread).

        Tk is single-threaded; the callback MUST marshal any widget
        updates onto the Tk loop itself - this method does not do
        that for you. See `IndicatorPanel._on_sample` for the
        canonical `root.after_idle` shim.
        """
        session = self._ensure()

        def _listener(sample: zenoh.Sample) -> None:
            try:
                raw = sample.payload.to_string()
                msg = json.loads(raw)
            except Exception:
                return
            if not isinstance(msg, dict):
                return
            try:
                callback(str(sample.key_expr), msg)
            except Exception:
                pass

        sub = session.declare_subscriber(key_expr, _listener)
        with self._lock:
            self._subscribers.append(sub)

    def close(self) -> None:
        with self._lock:
            for sub in self._subscribers:
                try:
                    sub.undeclare()
                except Exception:
                    pass
            self._subscribers.clear()
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
    """One row in the dashboard.

    Renders one of two layouts depending on `sig.is_toggle`:
      * False (default): label + slider + numeric spinbox + unit
      * True            : label + on/off Checkbutton (no slider)

    A toggle row publishes `sig.on_value` when checked and
    `sig.off_value` when unchecked. Mutual exclusion between two
    toggle rows is handled in `Dashboard._publish` via the
    `sig.mutex_with` Zenoh key.
    """

    def __init__(self, parent: Frame, sig: Signal, publish: Callable[[Signal, float], None]) -> None:
        self.sig = sig
        self.publish = publish
        self._float = sig.default
        self._building = True

        ttk.Label(parent, text=sig.display, width=24).grid(row=0, column=0, sticky="w", padx=(8, 4), pady=4)

        # Toggle path: a single Checkbutton on the left, no slider/spinbox.
        if sig.is_toggle:
            self.var = None
            self._toggle_var = BooleanVar(value=(sig.default == sig.on_value))
            self._toggle = ttk.Checkbutton(
                parent,
                text="Off",
                variable=self._toggle_var,
                command=self._on_toggle,
            )
            self._toggle.grid(row=0, column=1, sticky="w", padx=4, pady=4)
            ttk.Label(parent, text=sig.unit, width=4).grid(row=0, column=3, sticky="w", padx=(0, 8), pady=4)
            parent.columnconfigure(1, weight=1)
            self._refresh_toggle_label()
            self._building = False
            return

        # Slider + spinbox path (everything else).
        self.var = IntVar(value=int(round(sig.default))) if sig.is_int else None
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

    # -------- toggle helpers (only used when sig.is_toggle) -----------

    def _refresh_toggle_label(self) -> None:
        try:
            self._toggle.configure(text="On" if self._toggle_var.get() else "Off")
        except Exception:
            pass

    def _on_toggle(self) -> None:
        if self._building:
            return
        on = bool(self._toggle_var.get())
        value = self.sig.on_value if on else self.sig.off_value
        self._float = value
        self._refresh_toggle_label()
        self.publish(self.sig, value)

    def is_on(self) -> bool:
        """True if this toggle is currently checked."""
        return bool(self.sig.is_toggle and self._toggle_var.get())

    def set_off_silent(self) -> None:
        """Force the toggle off without firing a publish.

        The caller (Dashboard mutex handler) is responsible for
        publishing the off-value separately so the ECU sees it.
        """
        if not self.sig.is_toggle:
            return
        self._building = True
        try:
            self._toggle_var.set(False)
            self._refresh_toggle_label()
            self._float = self.sig.off_value
        finally:
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

    def set_value_and_publish(self, value: float) -> None:
        """Programmatically move this slider to *value* and publish it.

        Called by the battery drain simulation on every tick so the GUI
        stays in sync with the simulated values.  Toggle rows are
        ignored (toggles are not part of the drain loop).
        """
        if self.sig.is_toggle:
            return
        if self.sig.is_int:
            value = int(round(value))
        value = max(self.sig.vmin, min(self.sig.vmax, value))
        if self._float == value:
            return
        self._float = value
        self.scale.set(value)
        if self.sig.is_int and self.var is not None:
            self.var.set(int(value))
        else:
            try:
                self.spin.delete(0, "end")
                self.spin.insert(0, f"{value:.2f}")
            except Exception:
                pass
        self.publish(self.sig, value)


class IndicatorPanel:
    """Reverse-channel status indicators.

    Renders three small LED-style canvases under the rest of the GUI:
      * HVAC          : green when fan is on, red when fan is off
      * Seat Heating  : red  when the seat is warming, grey otherwise
      * Seat Cooling  : blue when the seat is cooling, grey otherwise

    The seat lane uses TWO independent LEDs rather than one bulb with
    a precedence rule. That makes it obvious at a glance which seat
    actuator is engaged, and surfaces the (pathological) case where
    the EV-app on VM1 drives both channels at the same time.

    The seat LEDs are driven by **two independent inputs** and turn
    on when EITHER fires:

      1. Reverse channel from VM2's seat ECU (real Kuksa state):
         `seat.heating` status `heating` or `seat.heating_cooling`
         status `heating`  -> Heating LED red
         `seat.heating_cooling` status `cooling`
                          -> Cooling LED blue

      2. The dashboard's own toggle state (user-stated intent):
         Seat Heating toggle ON  -> Heating LED red
         Seat Cooling toggle ON  -> Cooling LED blue

    Combining both is necessary because the EV Range Extender app on
    VM1 may keep overwriting `Vehicle.Cabin.Seat.Row1.DriverSide.\\
    HeatingCooling` with a positive value (the playground prototype
    writes `1` per tick), which would otherwise drag the cooling LED
    back to grey the instant the dashboard publishes `-100`. The
    local-toggle override means the user's intent stays visible on
    the indicator even when the EV-app overrules the actual Kuksa
    value over the wire.

    Updates arrive on Zenoh callbacks from the worker thread; this
    panel marshals each one onto the Tk loop with `after_idle`
    because Tk is not thread-safe. Toggle changes arrive on the Tk
    loop directly (no marshalling needed).

    Per-key memory:
      * `_seat_keys`              : last (value, status) seen for
                                    each of the two seat VSS keys
                                    (`seat.heating`,
                                    `seat.heating_cooling`).
      * `_local_heating_on` /
        `_local_cooling_on`       : last known state of the two
                                    dashboard toggles, pushed by
                                    `Dashboard._publish`.
      * `_last_color` / `_last_text` : per-lane dedup so the
                                    indicator does not flicker when
                                    an input re-asserts the same
                                    state.
    """

    def __init__(self, parent: Frame, root: Tk) -> None:
        self._root = root

        # HVAC row -----------------------------------------------------
        hvac = ttk.LabelFrame(parent, text="HVAC Status (from VM2)", padding=(8, 6))
        hvac.pack(fill="x", expand=False, padx=10, pady=(8, 0))

        hvac_row = Frame(hvac)
        hvac_row.pack(fill="x", expand=True)
        ttk.Label(hvac_row, text="Fan", width=10).grid(row=0, column=0, sticky="w", padx=(4, 8), pady=4)
        self._hvac_canvas = Canvas(hvac_row, width=22, height=22,
                                   highlightthickness=0, bd=0)
        self._hvac_circle = self._hvac_canvas.create_oval(
            3, 3, 19, 19, fill=INDICATOR_COLORS["grey"], outline="#333"
        )
        self._hvac_canvas.grid(row=0, column=1, padx=4, pady=4)
        self._hvac_text = StringVar(value="awaiting ECU...")
        ttk.Label(hvac_row, textvariable=self._hvac_text, anchor="w").grid(
            row=0, column=2, sticky="we", padx=(8, 4), pady=4
        )
        hvac_row.columnconfigure(2, weight=1)

        # Seat lane (two LEDs: heating + cooling) ----------------------
        seat = ttk.LabelFrame(parent, text="Seat Status (from VM2)", padding=(8, 6))
        seat.pack(fill="x", expand=False, padx=10, pady=(8, 0))

        # Heating sub-row.
        seat_heat_row = Frame(seat)
        seat_heat_row.pack(fill="x", expand=True)
        ttk.Label(seat_heat_row, text="Heating", width=10).grid(
            row=0, column=0, sticky="w", padx=(4, 8), pady=4
        )
        self._seat_heat_canvas = Canvas(seat_heat_row, width=22, height=22,
                                        highlightthickness=0, bd=0)
        self._seat_heat_circle = self._seat_heat_canvas.create_oval(
            3, 3, 19, 19, fill=INDICATOR_COLORS["grey"], outline="#333"
        )
        self._seat_heat_canvas.grid(row=0, column=1, padx=4, pady=4)
        self._seat_heat_text = StringVar(value="awaiting ECU...")
        ttk.Label(seat_heat_row, textvariable=self._seat_heat_text, anchor="w").grid(
            row=0, column=2, sticky="we", padx=(8, 4), pady=4
        )
        seat_heat_row.columnconfigure(2, weight=1)

        # Cooling sub-row.
        seat_cool_row = Frame(seat)
        seat_cool_row.pack(fill="x", expand=True)
        ttk.Label(seat_cool_row, text="Cooling", width=10).grid(
            row=0, column=0, sticky="w", padx=(4, 8), pady=4
        )
        self._seat_cool_canvas = Canvas(seat_cool_row, width=22, height=22,
                                        highlightthickness=0, bd=0)
        self._seat_cool_circle = self._seat_cool_canvas.create_oval(
            3, 3, 19, 19, fill=INDICATOR_COLORS["grey"], outline="#333"
        )
        self._seat_cool_canvas.grid(row=0, column=1, padx=4, pady=4)
        self._seat_cool_text = StringVar(value="awaiting ECU...")
        ttk.Label(seat_cool_row, textvariable=self._seat_cool_text, anchor="w").grid(
            row=0, column=2, sticky="we", padx=(8, 4), pady=4
        )
        seat_cool_row.columnconfigure(2, weight=1)

        # Per-key status memory (drives BOTH seat LEDs independently).
        self._seat_keys: dict[str, tuple[int, str]] = {}
        # Last known dashboard toggle state. Pushed in from
        # Dashboard._publish so the LEDs can reflect the user's intent
        # even if the EV-app on VM1 overwrites the Kuksa value.
        self._local_heating_on: bool = False
        self._local_cooling_on: bool = False
        self._last_color: dict[str, str] = {}
        self._last_text: dict[str, str] = {}

    # -- Zenoh callbacks --------------------------------------------------

    def on_hvac_sample(self, _key: str, msg: dict) -> None:
        """Zenoh worker thread -> Tk loop trampoline (HVAC)."""
        self._root.after_idle(self._apply_hvac, msg)

    def on_seat_sample(self, _key: str, msg: dict) -> None:
        """Zenoh worker thread -> Tk loop trampoline (Seat)."""
        self._root.after_idle(self._apply_seat, msg)

    # -- Toggle-state hook (called from Dashboard._publish) --------------

    def set_seat_toggle_state(self, heating_on: bool, cooling_on: bool) -> None:
        """Push the dashboard's own seat toggle state into the panel.

        Re-renders both seat LEDs with the latest reverse-channel
        state ORed with the new local toggle state, so the LED
        immediately reflects the user's click even before the
        reverse-channel echo arrives.
        """
        self._local_heating_on = bool(heating_on)
        self._local_cooling_on = bool(cooling_on)
        self._render_seat_leds()

    # -- Tk-thread appliers (do the actual widget mutation) --------------

    def _apply_hvac(self, msg: dict) -> None:
        status = str(msg.get("status", "off")).lower()
        value = msg.get("value")
        if status == "on":
            color = "green"
        else:
            color = "red"
        text = f"value={value!s:<6}  status={status}  src={msg.get('source', '?')}"
        self._render("hvac", self._hvac_canvas, self._hvac_circle,
                     self._hvac_text, color, text)

    def _apply_seat(self, msg: dict) -> None:
        """Reverse-channel sample arrived. Update the per-key memory
        and re-render both seat LEDs (the toggle state we already
        know about is taken into account by `_render_seat_leds`)."""
        key = str(msg.get("key", ""))
        value = msg.get("value", 0)
        status = str(msg.get("status", "off")).lower()
        try:
            v_int = int(value) if isinstance(value, (int, float)) else 0
        except Exception:
            v_int = 0
        self._seat_keys[key] = (v_int, status)
        self._render_seat_leds()

    def _render_seat_leds(self) -> None:
        """Recompute both seat LEDs from `_seat_keys` (reverse-channel)
        and `_local_*_on` (dashboard toggle state).

        Heating LED  is red if any of these is true:
          - the dashboard Seat Heating toggle is on
          - `seat.heating` signal value is non-zero
          - `seat.heating_cooling` signal value is > 0
            (HeatingCooling > 0 means heating per VSS)

        Cooling LED  is blue if any of these is true:
          - the dashboard Seat Cooling toggle is on
          - `seat.heating_cooling` signal value is non-zero in EITHER
            direction. We intentionally relax this beyond strict VSS
            (which only treats HC<0 as cooling) because the EV Range
            Extender prototype on `playground.digital.auto` writes
            `HeatingCooling = 1` to mean "seat module engaged" - the
            user expects the Cooling LED to light up alongside the
            Heating LED in that case, because the EV-app drives both
            channels in lockstep.
        """
        heating_val, _heating_status = self._seat_keys.get(
            "seat.heating", (None, "off")
        )
        hc_val, _hc_status = self._seat_keys.get(
            "seat.heating_cooling", (None, "off")
        )

        # ---- Heating LED ----------------------------------------------
        heating_engaged = (
            self._local_heating_on
            or (heating_val is not None and heating_val != 0)
            or (hc_val is not None and hc_val > 0)
        )
        if heating_engaged:
            heat_color = "red"
            heat_label = "ON  (warming)"
        else:
            heat_color = "grey"
            heat_label = "off"
        heat_text = (f"toggle={'on' if self._local_heating_on else 'off':<3}"
                     f"  heating={heating_val if heating_val is not None else '?'}"
                     f"  hc={hc_val if hc_val is not None else '?'}"
                     f"  -> {heat_label}")
        self._render("seat_heat",
                     self._seat_heat_canvas, self._seat_heat_circle,
                     self._seat_heat_text, heat_color, heat_text)

        # ---- Cooling LED ----------------------------------------------
        cooling_engaged = (
            self._local_cooling_on
            or (hc_val is not None and hc_val != 0)
        )
        if cooling_engaged:
            cool_color = "blue"
            cool_label = "ON  (cooling)"
        else:
            cool_color = "grey"
            cool_label = "off"
        cool_text = (f"toggle={'on' if self._local_cooling_on else 'off':<3}"
                     f"  hc={hc_val if hc_val is not None else '?'}"
                     f"  -> {cool_label}")
        self._render("seat_cool",
                     self._seat_cool_canvas, self._seat_cool_circle,
                     self._seat_cool_text, cool_color, cool_text)

    def _render(self, lane: str, canvas: Canvas, oval_id: int,
                text_var: StringVar, color: str, text: str) -> None:
        """De-duplicating renderer. Avoids needlessly re-painting Tk."""
        if self._last_color.get(lane) != color:
            try:
                canvas.itemconfigure(oval_id, fill=INDICATOR_COLORS.get(color, "#888"))
            except Exception:
                pass
            self._last_color[lane] = color
        if self._last_text.get(lane) != text:
            text_var.set(text)
            self._last_text[lane] = text


class Dashboard:
    def __init__(self, root: Tk, bus: ZenohBus) -> None:
        self.root = root
        self.bus = bus
        self.root.title("EV Range Extender - Hardware Simulator")
        self.root.geometry("640x740")
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

        self.status_var = StringVar(value="Ready. Move a slider or toggle to publish.")

        # Index of every SignalRow by its Zenoh key, so toggle mutex
        # can locate its partner row in `_publish` below.
        self._rows_by_key: dict[str, SignalRow] = {}

        # --- Battery Drain Simulation controls --------------------------------
        self._drain_running = False
        self._drain_after_id: str | None = None

        sim_frame = ttk.LabelFrame(root, text="Drive", padding=(8, 6))
        sim_frame.pack(fill="x", expand=False, padx=10, pady=(8, 0))
        sim_inner = Frame(sim_frame)
        sim_inner.pack(fill="x", expand=True, padx=4, pady=4)
        self._sim_btn_var = StringVar(value="\u25b6  Start")
        self._sim_btn = ttk.Button(
            sim_inner,
            textvariable=self._sim_btn_var,
            command=self._toggle_simulation,
            width=18,
        )
        self._sim_btn.grid(row=0, column=0, padx=(0, 12), pady=2)
        self._sim_status_var = StringVar(
            value="Idle \u2014 press Start to begin battery drain"
        )
        ttk.Label(sim_inner, textvariable=self._sim_status_var, anchor="w").grid(
            row=0, column=1, sticky="we", padx=4
        )
        sim_inner.columnconfigure(1, weight=1)

        for section_title, sigs in ALL_SECTIONS:
            frame = ttk.LabelFrame(root, text=section_title, padding=(8, 6))
            frame.pack(fill="x", expand=False, padx=10, pady=(8, 0))
            for sig in sigs:
                row = Frame(frame)
                row.pack(fill="x", expand=True)
                self._rows_by_key[sig.key] = SignalRow(row, sig, self._publish)

        # Reverse-channel status indicators (driven by VM2 ECUs)
        self.indicators = IndicatorPanel(root, root)
        try:
            self.bus.subscribe(STATUS_KEY_HVAC, self.indicators.on_hvac_sample)
            self.bus.subscribe(STATUS_KEY_SEAT, self.indicators.on_seat_sample)
        except Exception as exc:
            ts = datetime.now().strftime("%H:%M:%S")
            # Non-fatal: the dashboard still works as a one-way emitter.
            self.status_var.set(
                f"[{ts}]  WARN reverse-channel subscribe failed: {exc}"
            )

        # Status bar
        status = ttk.Frame(root, padding=(8, 4))
        status.pack(fill="x", side="bottom")
        ttk.Label(status, textvariable=self.status_var, anchor="w").pack(fill="x", expand=True)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # Zenoh keys of the two seat toggles. Used by `_publish` to detect
    # when it should push toggle state into the IndicatorPanel.
    _SEAT_TOGGLE_KEYS = ("sim/cabin/seat/heating", "sim/cabin/seat/hc")

    def _publish(self, sig: Signal, value: float | int) -> None:
        # Toggle mutex: when a toggle turns on, force its partner toggle
        # off in the GUI and publish the partner's off-value to the
        # ECU so the local state on the VM matches what the user sees.
        if sig.is_toggle and sig.mutex_with and value == sig.on_value:
            partner = self._rows_by_key.get(sig.mutex_with)
            if partner is not None and partner.sig.is_toggle and partner.is_on():
                partner.set_off_silent()
                try:
                    self.bus.put(partner.sig.key, partner.sig.off_value)
                except Exception as exc:
                    ts = datetime.now().strftime("%H:%M:%S")
                    self.status_var.set(
                        f"[{ts}]  ERROR turning off partner {partner.sig.key}: {exc}"
                    )

        try:
            self.bus.put(sig.key, value)
            ts = datetime.now().strftime("%H:%M:%S")
            unit = sig.unit
            if sig.is_toggle:
                state = "On" if value == sig.on_value else "Off"
                self.status_var.set(f"[{ts}]  PUT {sig.key} = {int(value)} ({state})")
            elif sig.is_int:
                self.status_var.set(f"[{ts}]  PUT {sig.key} = {int(value)} {unit}")
            else:
                self.status_var.set(f"[{ts}]  PUT {sig.key} = {value:.2f} {unit}")
        except Exception as exc:
            ts = datetime.now().strftime("%H:%M:%S")
            self.status_var.set(f"[{ts}]  ERROR publishing {sig.key}: {exc}")

        # If a seat toggle just changed (this one OR its mutex partner
        # we force-off'd above), push the latest toggle state into the
        # indicator panel so the LED reflects the user's intent
        # immediately - even if the EV-app on VM1 keeps overwriting
        # the actual Kuksa value over the wire.
        if sig.key in self._SEAT_TOGGLE_KEYS:
            self._sync_seat_toggle_indicators()

    def _sync_seat_toggle_indicators(self) -> None:
        """Read the current state of the two seat toggles and forward
        it into the IndicatorPanel. Safe to call from the Tk thread
        (we're already in `_publish`, which is invoked from a Tk
        widget callback)."""
        heating_row = self._rows_by_key.get("sim/cabin/seat/heating")
        cooling_row = self._rows_by_key.get("sim/cabin/seat/hc")
        heating_on = bool(heating_row.is_on()) if heating_row else False
        cooling_on = bool(cooling_row.is_on()) if cooling_row else False
        try:
            self.indicators.set_seat_toggle_state(heating_on, cooling_on)
        except Exception:
            pass

    # ---- Battery drain simulation -------------------------------------------

    _DRAIN_TICK_MS: int = 1000        # milliseconds between ticks
    _SOC_DRAIN_PER_TICK: float = 1.0 # % SoC removed per tick (≈3.3 min full drain)

    def _toggle_simulation(self) -> None:
        """Start or stop the battery drain simulation."""
        if self._drain_running:
            # --- Stop ---
            self._drain_running = False
            if self._drain_after_id is not None:
                try:
                    self.root.after_cancel(self._drain_after_id)
                except Exception:
                    pass
                self._drain_after_id = None
            self._sim_btn_var.set("\u25b6  Start")
            self._sim_status_var.set("Stopped.")
        else:
            # --- Start ---
            self._drain_running = True
            self._sim_btn_var.set("\u25a0  Stop")
            self._sim_status_var.set("Running \u2014 battery draining\u2026")
            self._drain_after_id = self.root.after(
                self._DRAIN_TICK_MS, self._drain_tick
            )

    def _drain_tick(self) -> None:
        """One simulation tick: drain SoC by _SOC_DRAIN_PER_TICK.

        Voltage is intentionally left untouched during the auto-drain
        loop so the user can keep battery voltage fixed or adjust it
        manually as desired.
        """
        if not self._drain_running:
            return

        soc_row = self._rows_by_key.get("sim/battery/soc")

        if soc_row is None:
            # Signal catalogue changed; give up gracefully.
            self._drain_running = False
            return

        new_soc = round(
            max(0.0, soc_row._float - self._SOC_DRAIN_PER_TICK), 2
        )
        soc_row.set_value_and_publish(new_soc)

        ts = datetime.now().strftime("%H:%M:%S")
        self._sim_status_var.set(
            f"[{ts}]  Battery draining \u2014 SoC: {new_soc:.1f} %"
        )

        if new_soc <= 0.0:
            # Fully depleted — stop automatically.
            self._drain_running = False
            self._drain_after_id = None
            self._sim_btn_var.set("\u25b6  Start")
            self._sim_status_var.set(
                "Battery depleted. Reset the Battery % slider to restart."
            )
            return

        self._drain_after_id = self.root.after(
            self._DRAIN_TICK_MS, self._drain_tick
        )

    def _on_close(self) -> None:
        self._drain_running = False
        if self._drain_after_id is not None:
            try:
                self.root.after_cancel(self._drain_after_id)
            except Exception:
                pass
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
