# Eclipse SDV Blueprint — Hybrid Cloud–Edge Application Lifecycle Management


This blueprint demonstrates an end-to-end workflow for developing, validating and orchestrating Mixed-Critical Software-Defined Vehicle (SDV) applications across cloud and HPC edge device.

It showcases how SDV applications are built in the cloud, pushed to AosEdge registry, and deployed onto an in-vehicle HPC running AosCore and Eclipse AutoWorx runtime component which includes Eclipse Velocitas, Eclipse Kuksa, and a Signal Gateway. Vehicle signals are exchanged across heterogeneous compute domains with HPCs, Zonal and End ECUs — through Eclipse SCore and Eclipse Zenoh.

---

## What Is This?

This project is a reference blueprint for building and deploying **Software-Defined Vehicle (SDV)** applications across a hybrid cloud and edge setup. Think of it as a proven recipe that shows:

- How a developer writes and tests a vehicle app in the cloud
- How that app gets packaged and stored in a registry
- How it gets deployed — over the air — into the vehicle's onboard computer
- How the vehicle then acts on it in real time

The stack uses open, Eclipse-based components throughout, making it transparent and extensible.

---

## The Use Case — EV Range Extender

The demo application is an **EV Range Extender**.

When the vehicle's battery state of charge drops below a defined threshold, the system automatically enters a power-saving mode. It identifies non-essential features and turns them off — things like ambient lighting, reading lights, and seat heating — while keeping all core driving and safety functions fully intact.

### Customer Journey

The journey below shows how the system interacts across three steps — from driver action to automatic system response:

### Customer Journey

The journey below shows how the system interacts across three steps — from the initial trigger to the automated system response:

| | Step 1 | Step 2 | Step 3 |
| :--- | :--- | :--- | :--- |
| **Who** | System | System | Driver |
| **What** | Vehicle battery (State of Charge) drops below the predefined critical threshold. | System automatically enters power-saving mode, instantly disabling non-essential features (ambient lights, seat heating). | Driver continues driving safely with extended range and is notified of the system's actions. |
| **Customer TouchPoints** | Battery monitoring sensors | Cabin environment (lights dim, seat heater turns off) | Notification on the digital dashboard/infotainment screen ("Power Saving Mode Activated") |

> **Why this matters for OEMs:** Unlike a manual "Eco Mode" button, this journey highlights the **automated orchestration** of the Software-Defined Vehicle. The system constantly monitors the powertrain (Step 1), instantly communicates with the zonal controllers to shut down cabin comforts (Step 2), and keeps the driver informed without requiring them to take their hands off the wheel (Step 3).

| Signal | Layer | Purpose |
|---|---|---|
| `Vehicle.Powertrain.Battery.StateOfCharge` | Zonal | Triggers power-saving mode when charge is low |
| `Vehicle.Cabin.Lights.AmbientLight.Intensity` | Zonal | Reduced to save power |
| `Vehicle.Cabin.Lights.ReadingLight.Status` | Zonal | Turned off |
| `Vehicle.Cabin.Seat.Heating` | Zonal | Disabled |
| `Vehicle.Powertrain.Range` | HPC | Monitored at the compute level |

---

## Try the Prototype

You can explore and run the prototype directly in the digital.auto playground — no hardware needed:

 **[Open Prototype on digital.auto Playground](https://playground.digital.auto)**

The playground lets you simulate vehicle signals and see the app's logic in action before touching any real device. OEMs can use this to validate business logic, test signal flows, and iterate on the customer journey end-to-end.

---

## Architecture Overview

The blueprint is implemented in two phases. **Phase 1** uses virtual machines so teams can develop and test without physical hardware. **Phase 2** moves to real automotive-grade hardware for production-readiness validation. Both phases share the same cloud layer and application logic — only the edge hardware changes.

---

## Phase 1 — Virtual Machines (Start Here)

![Architecture Phase 1](./images/architecture_phase1.png)

Phase 1 is designed for **rapid development and validation**. Everything runs inside virtual machines (QEMU), so any developer can spin up the full stack on a standard laptop or cloud server. All the components that will eventually run on real car hardware are running here as software — making it fast and safe to iterate.

### How the flow works

```
① EV Range Extender Prototype 
        ↓
② App is published to the AosCloud App Registry
        ↓
③ AosCloud fetches the app and pushes it to the HPC-VM (App Fetching)
        ↓
④ AosCore on the HPC-VM executes the app via the digital.auto runtime
        ↓
⑤ The app reads/writes vehicle signals via eclipse-kuksa
        ↓
⑥ Signals flow between HPC-VM and Zonal-VM over eclipse-score / SOME-IP
```

### What's running in each layer

| Layer | What It Is | What It Does |
|---|---|---|
|  **AosCloud** | Fleet Management + App Registry | Stores, versions and distributes vehicle apps to the fleet |
|  **HPC-VM** (Linux) | AosCore + digital.auto + Eclipse AutoWorx stack | The brain — runs the vehicle app, handles signal logic |
|  **Zonal-VM** (Linux) | Sensor/Actuator Controllers | Simulates the lower-level ECU that controls physical components |
|  **Comms** | Eclipse SCore / SOME-IP | Connects HPC and Zonal layers — same protocol used in real cars |

### Eclipse components inside the HPC-VM (highlighted in purple)

| Component | Role |
|---|---|
| `eclipse-autoworx` | Automates app lifecycle management on the vehicle |
| `eclipse-kuksa` | Vehicle signal broker — reads and writes VSS signals |
| `eclipse-velocitas` | Framework for building vehicle apps in Python/C++ |

> **All components shown with a checkmark in the diagram are fully completed and working in Phase 1.**

---

## Phase 2 — Real Hardware (Production Validation)

![Architecture Phase 2](./images/architecture_phase2.png)

Phase 2 replaces the virtual machines with **real automotive hardware**. The cloud layer and application logic stay identical — this phase validates that the same software runs correctly on the hardware an OEM would actually put in a vehicle.

A key addition in Phase 2 is the **End ECU layer** (STM32), which represents the deepest level of the vehicle's electrical architecture — the microcontrollers directly attached to physical sensors and actuators like motors, lights, and HVAC.

### How the flow works

```
① Same cloud flow as Phase 1 (AosCloud → App Registry → App Fetching)
        ↓
② App runs on a real NXP S32G2 board (automotive-grade processor)
        ↓
③ HPC communicates with Zonal Raspberry Pi over eclipse-score / SOME-IP
        ↓
④ Zonal Pi communicates with End STM32 microcontroller over eclipse-zenoh
        ↓
⑤ STM32 directly controls HVAC, infotainment display, seat ventilation
```

### What's running in each layer

| Layer | Hardware | Software | What It Does |
|---|---|---|---|
|  **AosCloud** | Cloud | Fleet Management + App Registry | Same as Phase 1 |
|  **HPC** | NXP S32G2 | AosCore + digital.auto + `eclipse-autosd` | Automotive-grade compute, runs the main app logic |
|  **Zonal** | Raspberry Pi | Linux + Eclipse AutoWorx stack | Bridges HPC signals to physical ECU layer |
|  **End ECU** | STM32 | `eclipse-threadX` | Directly controls physical actuators (HVAC, lights, display) |
|  **HPC ↔ Zonal** | — | Eclipse SCore / SOME-IP | Standard automotive bus protocol |
|  **Zonal ↔ End** | — | Eclipse Zenoh | Lightweight pub/sub messaging for constrained devices |

### Additional signals unlocked in Phase 2

| Signal | Layer | Purpose |
|---|---|---|
| `Vehicle.Cabin.HVAC.TargetTemperature` | End ECU | Adjust climate control for power saving |
| `Vehicle.Infotainment.Display.Brightness` | End ECU | Dim screen to reduce power draw |
| `Vehicle.Cabin.Seat.Ventilation.Level` | End ECU | Disable seat ventilation |

> **For OEMs:** Phase 2 is where you validate that the app behaviour confirmed in the Playground and Phase 1 translates faithfully onto your target hardware. The signal list above represents exactly the vehicle capabilities your app will control in a real car.

---

## Phase Comparison

| | Phase 1 | Phase 2 |
|---|---|---|
| **Goal** | Develop & validate logic | Validate on real hardware |
| **HPC** | Linux VM (QEMU) | NXP S32G2 |
| **Zonal** | Linux VM | Raspberry Pi |
| **End ECU** | Not present | STM32 |
| **Zonal ↔ End comms** | Not present | Eclipse Zenoh |
| **HPC ↔ Zonal comms** | Eclipse SCore / SOME-IP | Eclipse SCore / SOME-IP |
| **Setup complexity** | Low — just Docker | Requires hardware |
| **Best for** | App development, signal testing, OEM demos | Pre-production validation |

---

## Getting Started

### Prerequisites

- Docker installed on your machine (or inside a QEMU VM)
- Access to [playground.digital.auto](https://playground.digital.auto)

### Run the SDV Runtime (Phase 1)

**1. Pull the runtime image**
```bash
docker pull ghcr.io/eclipse-autowrx/sdv-runtime:latest
```

**2. Start the runtime**
```bash
docker run -d \
  -e RUNTIME_NAME="MyRuntimeName" \
  ghcr.io/eclipse-autowrx/sdv-runtime:latest
```

> `RUNTIME_NAME` is the identifier you'll use to register this runtime on the playground.

**3. Register your runtime on the Playground**

1. Go to [playground.digital.auto](https://playground.digital.auto) and log in
2. Navigate to **Profile → My Assets → Runtimes**
3. Click **Add Asset**
4. Enter the same `RUNTIME_NAME` from step 2
5. Set **Type = Runtime** and click **Save**

**4. Open and run the prototype**

1. Open the [EV Range Extender prototype](https://playground.digital.auto)
2. Select the runtime you just registered
3. Click **Execute** — vehicle signals will start flowing in real time

---

## Project Resources

| Resource | Link |
|---|---|
| digital.auto Playground (Prototype) | [playground.digital.auto](https://playground.digital.auto) |
| Development Repository | [eclipse-autowrx/epam-service-connector](https://github.com/eclipse-autowrx/epam-service-connector) |
| digital.auto Website | [www.digital.auto](https://www.digital.auto) |

---
