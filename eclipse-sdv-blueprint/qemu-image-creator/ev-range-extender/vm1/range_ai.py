"""Range Compute AI for the EV Range Extender.

Runs on VM1 alongside the BMS. Connects to the local Kuksa Databroker
(the ev-range SDV Runtime container on 127.0.0.1:55555) and:

  1. Subscribes to the three battery telemetry signals that the BMS
     observes (and the Kuksa CLI / a real sensor publishes):

         Vehicle.Powertrain.Battery.Current        (A)
         Vehicle.Powertrain.Battery.Voltage        (V)
         Vehicle.Powertrain.Battery.StateOfCharge  (%)

  2. On every input update, computes the estimated remaining driving
     range using a simple physical model (see compute_range below).

  3. Publishes the result back to the same Databroker as:

         Vehicle.Powertrain.Range  (km)

Range model (deliberately small; tweak the constants at the top of
this file to match a different vehicle):

    available_kWh = (SoC / 100) * BATTERY_CAPACITY_KWH
    consumption   = NOMINAL_CONSUMPTION_KWH_PER_KM
                    scaled up when instantaneous power > NOMINAL_CRUISE_POWER_KW
    range_km      = available_kWh / consumption

Examples (with the defaults below):

    SoC=100% idle/cruise -> ~417 km
    SoC=50%  idle/cruise -> ~208 km
    SoC=12%  idle/cruise -> ~50  km
    SoC=50%  hard accel  -> proportionally less
"""

import argparse
import asyncio
import sys
from datetime import datetime

from kuksa_client.grpc import Datapoint
from kuksa_client.grpc.aio import VSSClient


# Canonical COVESA VSS 4.x paths (what the digital.auto SDV Runtime
# ships with). To verify in the runtime, run in the Kuksa CLI:
#   metadata Vehicle.Powertrain.TractionBattery.**
#   metadata Vehicle.Powertrain.Range
SIGNAL_CURRENT = "Vehicle.Powertrain.TractionBattery.CurrentCurrent"
SIGNAL_VOLTAGE = "Vehicle.Powertrain.TractionBattery.CurrentVoltage"
SIGNAL_SOC = "Vehicle.Powertrain.TractionBattery.StateOfCharge.Current"

BATTERY_SIGNALS = [SIGNAL_CURRENT, SIGNAL_VOLTAGE, SIGNAL_SOC]

RANGE_SIGNAL = "Vehicle.Powertrain.Range"

# Vehicle model parameters (adjust to taste)
BATTERY_CAPACITY_KWH = 75.0
NOMINAL_CONSUMPTION_KWH_PER_KM = 0.18
NOMINAL_CRUISE_POWER_KW = 18.0


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{ts}] [range-ai] {msg}", flush=True)


def _format(value) -> str:
    if value is None:
        return "<unset>"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


class BatteryState:
    def __init__(self) -> None:
        self.current = None
        self.voltage = None
        self.state_of_charge = None

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


def compute_range(state: BatteryState):
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
    if state.current is not None and state.voltage is not None:
        try:
            power_kw = abs(float(state.current) * float(state.voltage)) / 1000.0
            if power_kw > NOMINAL_CRUISE_POWER_KW:
                load_factor = power_kw / NOMINAL_CRUISE_POWER_KW
                consumption = NOMINAL_CONSUMPTION_KWH_PER_KM * load_factor
        except (TypeError, ValueError):
            pass

    if consumption <= 0:
        return None

    return available_kwh / consumption


async def run(host: str, port: int) -> None:
    log(f"Connecting to Kuksa Databroker at {host}:{port}...")
    async with VSSClient(host, port) as client:
        log("Connected.")
        log(f"  Subscribing to {len(BATTERY_SIGNALS)} battery signal(s):")
        for s in BATTERY_SIGNALS:
            log(f"    - {s}")
        log("  Will publish to:")
        log(f"    - {RANGE_SIGNAL}")
        log(
            f"  Model: capacity={BATTERY_CAPACITY_KWH} kWh, "
            f"consumption={NOMINAL_CONSUMPTION_KWH_PER_KM} kWh/km, "
            f"cruise={NOMINAL_CRUISE_POWER_KW} kW"
        )

        state = BatteryState()
        async for updates in client.subscribe_current_values(BATTERY_SIGNALS):
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
                f"U={_format(state.voltage)} V)"
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
