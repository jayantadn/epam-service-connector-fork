# Copyright (c) 2026 Eclipse Foundation.
#
# This program and the accompanying materials are made available under the
# terms of the MIT License which is available at
# https://opensource.org/licenses/MIT.
#
# SPDX-License-Identifier: MIT
"""Range Compute AI for the EV Range Extender (runs on VM1).

Connects to the local Kuksa Databroker (the ev-range SDV Runtime
container on 127.0.0.1:55555) and:

  1. Subscribes to six input signals - all driven by the host PyTk
     dashboard via the BMS / HVAC / Seat ECUs:

         # Battery telemetry (driven on VM1 by bms.py)
         Vehicle.Powertrain.TractionBattery.CurrentCurrent          (A)
         Vehicle.Powertrain.TractionBattery.CurrentVoltage          (V)
         Vehicle.Powertrain.TractionBattery.StateOfCharge.Current   (%)

         # Cabin signals (driven on VM2, bridged VM2->VM1 over Zenoh)
         Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed            (% 0..100)
         Vehicle.Cabin.Seat.Row1.DriverSide.Heating                 (% 0..100)
         Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling          (% -100..100;
                                                                     negative = cooling/vent,
                                                                     positive = heating)

  2. On every update recomputes the estimated remaining driving range:

         available_kWh  = (SoC / 100) * BATTERY_CAPACITY_KWH
         consumption    = NOMINAL_CONSUMPTION_KWH_PER_KM
         consumption   *= load_factor   if instantaneous power > NOMINAL_CRUISE_POWER_KW
         consumption   += cabin_load_kw / AVG_SPEED_KMH      # fan + seat heat/vent
         range_km       = available_kWh / consumption

  3. Publishes the result back to the same Databroker as:

         Vehicle.Powertrain.Range  (km, Uint32)
"""

import argparse
import asyncio
import sys
from datetime import datetime

from kuksa_client.grpc import Datapoint
from kuksa_client.grpc.aio import VSSClient


# ---- VM1 battery telemetry (driven by Kuksa CLI on VM1) ---------------
# Canonical COVESA VSS 4.x paths (what the digital.auto SDV Runtime ships
# with). Verify in the Kuksa CLI:
#   metadata Vehicle.Powertrain.TractionBattery.**
#   metadata Vehicle.Powertrain.Range
SIGNAL_CURRENT = "Vehicle.Powertrain.TractionBattery.CurrentCurrent"
SIGNAL_VOLTAGE = "Vehicle.Powertrain.TractionBattery.CurrentVoltage"
SIGNAL_SOC     = "Vehicle.Powertrain.TractionBattery.StateOfCharge.Current"

# ---- VM2 cabin signals (driven by host PyTk dashboard) ----------------
# Reach VM1 via the VM2->VM1 Zenoh bridge (zenoh_publisher.py ->
# zenoh_client.py) -> ev-range Kuksa Databroker. Verify in the Kuksa
# CLI on VM1 with:
#   metadata Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed
#   metadata Vehicle.Cabin.Seat.Row1.DriverSide.Heating
#   metadata Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling
SIGNAL_FAN_SPEED = "Vehicle.Cabin.HVAC.Station.Row1.Driver.FanSpeed"
SIGNAL_SEAT_HEAT = "Vehicle.Cabin.Seat.Row1.DriverSide.Heating"
SIGNAL_SEAT_HC   = "Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling"

BATTERY_SIGNALS    = [SIGNAL_CURRENT, SIGNAL_VOLTAGE, SIGNAL_SOC]
CABIN_SIGNALS      = [SIGNAL_FAN_SPEED, SIGNAL_SEAT_HEAT, SIGNAL_SEAT_HC]
SUBSCRIBED_SIGNALS = BATTERY_SIGNALS + CABIN_SIGNALS

RANGE_SIGNAL = "Vehicle.Powertrain.Range"

# ---- Vehicle model parameters ----------------------------------------
BATTERY_CAPACITY_KWH = 75.0
NOMINAL_CONSUMPTION_KWH_PER_KM = 0.18
NOMINAL_CRUISE_POWER_KW = 18.0

# Cabin actuator power model. We use Seat.Heating / Seat.HeatingCooling
# on Row1.DriverSide and the HVAC blower fan speed as the
# *driver-zone* control signals - i.e. they represent the aggregate
# of seat pad + footwell PTC heater + steering wheel heater + cabin
# AC compressor + blower for that zone. That's why the "max" powers
# below are 2 kW heat / 0.5 kW vent / 2 kW HVAC fan rather than the
# bare-element values. This keeps the demo visible (real EV cabin
# actuator budgets per zone).
# AVG_SPEED_KMH converts an instantaneous kW load into kWh/km so it
# can be added to NOMINAL_CONSUMPTION_KWH_PER_KM.
SEAT_HEATER_FULL_KW = 2.0
SEAT_VENT_FULL_KW   = 0.5
HVAC_FAN_FULL_KW    = 2.0
AVG_SPEED_KMH       = 60.0


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{ts}] [range-ai] {msg}", flush=True)


def _format(value) -> str:
    if value is None:
        return "<unset>"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


class VehicleState:
    """Latest values for everything range_ai cares about."""

    def __init__(self) -> None:
        self.current = None          # battery current (A)
        self.voltage = None          # battery voltage (V)
        self.state_of_charge = None  # SoC (%)
        self.fan_speed = None        # HVAC blower fan (%, 0..100) - from VM2
        self.seat_heat = None        # seat heating  (%, 0..100) - from VM2
        self.seat_hc = None          # seat HeatingCooling (%, -100..100) - from VM2

    def update(self, path: str, value) -> None:
        # Exact-path dispatch - the canonical VSS battery paths share
        # ".Current" suffixes (CurrentCurrent and StateOfCharge.Current),
        # so endswith() would collide.
        if path == SIGNAL_CURRENT:
            self.current = value
        elif path == SIGNAL_VOLTAGE:
            self.voltage = value
        elif path == SIGNAL_SOC:
            self.state_of_charge = value
        elif path == SIGNAL_FAN_SPEED:
            self.fan_speed = value
        elif path == SIGNAL_SEAT_HEAT:
            self.seat_heat = value
        elif path == SIGNAL_SEAT_HC:
            self.seat_hc = value


# Backwards-compatible alias - older tooling may import BatteryState.
BatteryState = VehicleState


def cabin_load_kw(state: "VehicleState") -> float:
    """Total cabin actuator power draw (kW). Always >= 0.

    * HVAC fan speed       : 0..100 %  -> 0..HVAC_FAN_FULL_KW
    * Seat.Heating         : 0..100 %  -> 0..SEAT_HEATER_FULL_KW
    * Seat.HeatingCooling  : -100..100 %
        positive (heating) -> SEAT_HEATER_FULL_KW * pct/100
        negative (cooling) -> SEAT_VENT_FULL_KW   * |pct|/100
    """
    total = 0.0
    if state.fan_speed is not None:
        try:
            pct = max(0.0, min(100.0, float(state.fan_speed)))
            total += HVAC_FAN_FULL_KW * (pct / 100.0)
        except (TypeError, ValueError):
            pass
    if state.seat_heat is not None:
        try:
            pct = max(0.0, min(100.0, float(state.seat_heat)))
            total += SEAT_HEATER_FULL_KW * (pct / 100.0)
        except (TypeError, ValueError):
            pass
    if state.seat_hc is not None:
        try:
            hc = max(-100.0, min(100.0, float(state.seat_hc)))
            if hc > 0:
                total += SEAT_HEATER_FULL_KW * (hc / 100.0)
            elif hc < 0:
                total += SEAT_VENT_FULL_KW * (-hc / 100.0)
        except (TypeError, ValueError):
            pass
    return total


def compute_range(state: VehicleState):
    """Return estimated remaining range in km, or None if SoC is unknown."""
    if state.state_of_charge is None:
        return None

    try:
        soc = float(state.state_of_charge)
    except (TypeError, ValueError):
        return None

    soc = max(0.0, min(100.0, soc))
    available_kwh = (soc / 100.0) * BATTERY_CAPACITY_KWH

    consumption = NOMINAL_CONSUMPTION_KWH_PER_KM

    # Hard-acceleration penalty (instantaneous traction power).
    if state.current is not None and state.voltage is not None:
        try:
            power_kw = abs(float(state.current) * float(state.voltage)) / 1000.0
            if power_kw > NOMINAL_CRUISE_POWER_KW:
                load_factor = power_kw / NOMINAL_CRUISE_POWER_KW
                consumption = NOMINAL_CONSUMPTION_KWH_PER_KM * load_factor
        except (TypeError, ValueError):
            pass

    # Cabin actuator load (additive - HVAC fan + seat heater + vent).
    consumption += cabin_load_kw(state) / AVG_SPEED_KMH

    if consumption <= 0:
        return None

    return available_kwh / consumption


async def run(host: str, port: int) -> None:
    log(f"Connecting to Kuksa Databroker at {host}:{port}...")
    async with VSSClient(host, port) as client:
        log("Connected.")
        log(f"  Subscribing to {len(SUBSCRIBED_SIGNALS)} signal(s):")
        for s in BATTERY_SIGNALS:
            log(f"    - {s}                  (battery, from Kuksa CLI on VM1)")
        for s in CABIN_SIGNALS:
            log(f"    - {s}   (cabin, from Kuksa CLI on VM2 via VM2->VM1 bridge)")
        log("  Will publish to:")
        log(f"    - {RANGE_SIGNAL}")
        log(
            f"  Model: capacity={BATTERY_CAPACITY_KWH} kWh, "
            f"consumption={NOMINAL_CONSUMPTION_KWH_PER_KM} kWh/km, "
            f"cruise={NOMINAL_CRUISE_POWER_KW} kW, "
            f"hvac-fan-max={HVAC_FAN_FULL_KW * 1000:.0f} W, "
            f"seat-heater-max={SEAT_HEATER_FULL_KW * 1000:.0f} W, "
            f"seat-vent-max={SEAT_VENT_FULL_KW * 1000:.0f} W"
        )

        state = VehicleState()
        async for updates in client.subscribe_current_values(SUBSCRIBED_SIGNALS):
            for path, dp in updates.items():
                value = dp.value if dp is not None else None
                state.update(path, value)
                log(f"input  : {path} = {_format(value)}")

            range_km = compute_range(state)
            if range_km is None:
                log("output : <waiting for StateOfCharge to be set>")
                continue

            # Vehicle.Powertrain.Range is declared as Uint32 in the
            # ev-range VSS catalog, so we must publish an int (not a
            # float) - otherwise the broker rejects the write.
            range_km_int = max(0, int(round(range_km)))
            cabin_kw = cabin_load_kw(state)

            try:
                await client.set_current_values({
                    RANGE_SIGNAL: Datapoint(range_km_int),
                })
            except Exception as exc:
                log(f"ERROR publishing {RANGE_SIGNAL}: {exc}")
                continue

            log(
                f"output : {RANGE_SIGNAL} = {range_km_int} km "
                f"(computed {range_km:.1f} km; "
                f"SoC={_format(state.state_of_charge)} %, "
                f"I={_format(state.current)} A, "
                f"U={_format(state.voltage)} V, "
                f"fan={_format(state.fan_speed)} %, "
                f"seatHeat={_format(state.seat_heat)} %, "
                f"seatHC={_format(state.seat_hc)} %, "
                f"cabin={cabin_kw * 1000:.0f} W)"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EV Range Extender - Range Compute AI (VM1)"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Kuksa Databroker host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=55555,
        help="Kuksa Databroker port (default: 55555)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args.host, args.port))
    except KeyboardInterrupt:
        log("Stopping.")
        return 0
    except Exception as exc:
        log(f"FATAL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
