# Eclipse SDV Blueprint — Hybrid Cloud–Edge Application Lifecycle Management

This blueprint demonstrates an end-to-end workflow for developing, validating and orchestrating Mixed-Critical Software-Defined Vehicle (SDV) applications across cloud and HPC edge device.

It showcases how SDV applications are developed in the Playground Digital Auto Portal using the C++ and Python platforms, pushed to AosEdge registry, and deployed onto an in-vehicle HPC running AosCore software package. In the updated architecture, the AutoWorx Runtime is replaced by the Syncer, KUKSA Bridge, and Zenoh protocol, while Eclipse KUKSA remains a core component. Vehicle signals are exchanged across heterogeneous compute domains with HPCs, Zonal and End ECUs — through Eclipse SCore and Eclipse Zenoh.

---

## Demonstrated Use Case – EV Range Extender

Software defined Application  **EV Range Extender**.

The EV Range Extender application continuously monitors the vehicle's battery State of Charge (SoC). When the SoC drops below a predefined threshold, the application initiates a power-saving mode by identifying and reducing or disabling non-essential functions, such as HVAC climate control and seat heating, while maintaining all critical driving and safety functions.

### Use Case Flow

The table below shows how the EV Range Extender application monitors the battery State of Charge (SoC), evaluates vehicle functions, and automatically activates power-saving measures when required.

| | Step 1 | Step 2 | Step 3 |
| :--- | :--- | :--- | :--- |
| **Who** | EV Range Extender | EV Range Extender | Driver |
| **What** | Continuosly Monitors the Vehicle battery (State of Charge), and it drops below the predefined critical threshold. | EV Range Extender app automatically enters power-saving mode and instantly scaling down non-essential features (HVAC climate control, seat heating). | The EV Range Extender optimises energy consumption to extend the vehicle's driving range, while notifying the driver of the actions performed. |
| **Customer TouchPoints** | None | Cabin environment (HVAC eases off, seat heater turns off) | "Power Saving Mode" activated and Driving Range extended |


> **Why this matters for OEMs:** Unlike a manual "Eco Mode" button, the EV Range Extender showcases how SDV applications can continuously monitor vehicle conditions, make intelligent decisions, and automatically optimise energy usage to extend driving range while maintaining safety and enhancing the driver experience

###  VSS signal used in the EV Range Extender sdv application

| Infrastructure Layer | VSS Signal | Vehicle Functions Layer | Functionality of the signal |
|----|---|---|---|
| VM1      | `Vehicle.Powertrain.TractionBattery.StateOfCharge.Current` |  BMS | Triggers power-saving mode when charge is low |
| VM1	   |  `Vehicle.Powertrain.TractionBattery.CurrentVoltage` |  BMS | Battery voltage monitored by the Battery Monitoring System |
| VM1	   |  `Vehicle.Powertrain.TractionBattery.CurrentCurrent` |  BMS | Battery current monitored by the Battery Monitoring System |
| VM2      | `Vehicle.Cabin.HVAC.AmbientAirTemperature` |  HVAC ECU | Adjusted to save power and bridged to VM1 |
| VM2      | `Vehicle.Cabin.Seat.Row1.DriverSide.Heating` |  Seat ECU | Disabled to save power and bridged to VM1 |

| VM2      | `Vehicle.Cabin.Seat.Row1.DriverSide.HeatingCooling` |  Seat ECU | Disabled to save power and bridged to VM2 |

---

## SDV Application on digital.auto playground portal

We can develop and test the SDV application directly in the digital.auto Playground portal without requiring any hardware-specific knowledge. The dashboard provides a visual interface to demonstrate and validate the application’s functionality, enabling rapid development and evaluation in a simulated environment.

 **[Open Application on digital.auto Playground](https://playground.digital.auto/model/67f76c0d8c609a0027662a69/library/prototype/69ce30f438bb8e98f0af5ac8/code)**

The digital.auto Playground enables you to simulate vehicle signals and observe the application's behaviour in a virtual environment before deploying it to real hardware. This allows OEMs to validate business logic, verify signal interactions, and refine the end-to-end customer experience through rapid testing and iteration.

---

## Architecture Overview

The blueprint is implemented in two phases. 

**Phase 1** Leverages virtual machines, enabling teams to develop, test, and validate functionality without relying on physical hardware. 

**Phase 2** Transitions the solution to production-grade automotive hardware, enabling validation under real-world conditions and ensuring readiness for deployment. While the edge hardware is replaced with automotive-grade devices, the cloud infrastructure, application logic, and interfaces remain unchanged across both phases, providing a seamless path from development to production

---

## Phase 1 — Virtual Machines

![Architecture Phase 1](./images/architecture_phase1.svg)

Phase 1 is designed for rapid application development and validation. By running the full stack in QEMU-based virtual machines, developers can build, test, and refine functionality on standard computing platforms before moving to automotive hardware. This software-defined environment accelerates innovation while reducing dependency on physical devices.

### End-to-End EV Range Extender Application Workflow

```
1. Develop and configure EV Range Extender Application in digital.auto Playground portal
        ↓
2. App is published to the AosCloud App Registry once it is successfully build
        ↓
3. AosCore fetches the latest app and deploy it to the qemu VM
        ↓
4. AosCore on the qemu VM configure the app and systemd is running this app as service
        ↓
5. The app(service) reads/writes vehicle signals via eclipse-kuksa
        ↓
6. App(service) functionality will be updated in the digital.auto Playground dash board

```

### Component Distribution by Layer

| Layer | What It Is | What It Does |
|---|---|---|
|  **AosCloud** | Fleet Management + App Registry | Manages the lifecycle, versioning, and fleet-wide deployment of vehicle applications |
|  **QEMU-VM-1** (Linux) | AosCore + sdv app | The brain — runs the vehicle app, handles signal logic |
|  **QEMU-VM-2** (Linux) | Services running Seat Control Module, HVAC ECU, Range Compute AI, Battery Monitoring System | Simulates the end-ECU layer that controls physical components |
|  **Communication stack** | Eclipse Zenoh | Connects QEMU-VM-1 and QEMU-VM-2 — lightweight pub/sub messaging |

### Eclipse components inside the blueprint phase 1

| Component | Role |
|---|---|
| `syncer `       | Communication manager send the signal to dash board  |
| `eclipse-kuksa` | Vehicle signal broker — reads and writes VSS signals |
| `eclipse-zenoh` | Modern pub/sub communication protocol between HPC-VM and End-VM |

### System Setup Workflow

- [QEMU VM's setup](#QEMU-VM's-setup)
- [Install software layer](#Install-sofware-layer)
- [Install necessory services(demo services) to QEMU-VM-1 and QEMU-VM-2](#Install-necessory-services(demo-services)-to-QEMU-VM-1-and-QEMU-VM-2)
- [AOS cloud service provoder portal setup](#AOS-cloud-servce-provider-portal-setup)
- [AOS cloud OEM provoder portal setup](#AOS-cloud-OEM-provoder-portal-setup)
- [Configure the Aos Cloud OEM Target Systems](#Configure-the-Aos-Cloud-OEM-Target-Systems)
- [Software Layer Verification and Unit Set Configuration](#Software-Layer-Verification-and-Unit-Set-Configuration)
- [Bind the service with subject in OEM portal to download to VM](#Bind-the-service-with-subject-in-OEM-portal-to-download-to-VM)
- [SDV Application Compilation and Configuration](#SDV-Application-Compilation-and-Configuration)  
- [Start the hardware simulator](#start-the-hardware-simulator)  
- [Steps to demo](#steps-to-demo)

⚠️ Disclaimer: Please follow the above steps for clear setup and demo.

### QEMU-VM's-setup
- Open [AOS edge meta-aos-vm releases](https://github.com/aosedge/meta-aos-vm/releases/)
- Download [aos-vm-image-genericx86-64-6.1.0.bosch.2.tar.xz]( https://github.com/aosedge/meta-aos-vm/releases/download/v6.1.0-bosch.2/aos-vm-image-genericx86-64-6.1.0-bosch.2.tar.gz)
- Download [aos_vm.sh](https://github.com/aosedge/meta-aos-vm/releases/download/v6.1.0-bosch.2/aos_vm.sh)
- Extract the VM image package.
	- tar -xvf aos-vm-image-genericx86-64-6.1.0-bosch.2.tar.xz
- Start the QEMU VMs by executing the script from the same directory.
	- ./aos_vm.sh run -f .
- Log in to the main node.
	- ssh root@10.0.0.100	
- Log in to the secondary node.
	- ssh root@10.0.0.x where x is assigned dynamically and can be determined using:*ip neigh*
- After logging in to both VMs, monitor the system logs:
	-journalctl -f
- Provision the VMs to the AOS Cloud infrastructure by running the following command on the primary node:
	- aos-prov provision -u 10.0.0.100

### Install-sofware-layer
- Open [AOS edge meta-aos-vm releases](https://github.com/aosedge/meta-aos-vm/releases/)
- Download [aos-vm-layers-genericx86-64-6.1.0-bosch.2.tar.gz]( https://github.com/aosedge/meta-aos-vm/releases/download/v6.1.0-bosch.2/aos-vm-layers-genericx86-64-6.1.0-bosch.2.tar.gz)

- Extract the VM image package.
	- tar -xvf aos-vm-layers-genericx86-64-6.1.0-bosch.2.tar.gz
- After extracting the package, navigate to the layers directory and send packages to cloud
	- aos-signer go
- Once upload is sucessfull, it will be pushing to respective VM's based on config.yaml file.

### Install necessory services(demo-services) to QEMU-VM-1 and QEMU-VM-2 
- Checkout [https://github.com/aosedge/demo-services.git] in the VM.
- Go to the path [repo/demo-services/ev-range-extender] and send the packages to the cloud by giving command aos-signer go
- Follow the step 
	- "Verify software layer is downloaded"
	- "Bind the service with subject in OEM portal to download to VM"

### AOS-cloud-OEM-provoder-portal-setup
- Open [AOS cloud docs web protal](https://docs.aosedge.tech/docs/quick-start/) and install the certificate in the local VM.
- Open [AOS Service provider(sp) web portal](https://api.aoscloud.io/account/start), click on OEM and while login it will ask p12 certificate and provide oem.p12(aos-user-oem.p12) certificate.

### Configure the Aos Cloud OEM Target Systems 
- Open [AOS edge meta-aos-vm releases](https://github.com/aosedge/meta-aos-vm/releases/)
- Download [unitconfig.json]( https://github.com/aosedge/meta-aos-vm/releases/download/v6.1.0-bosch.2/unitconfig.json)
- In the AosEdge Dashboard, navigate to Unit Config.
	- AosEdge Dashboard->UNIT CONFIG
- Click the "+" (Add New Unit Configuration) button.
- Open the downloaded unitconfig.json file, copy its contents, and paste them into the configuration editor.
- Click Update to save the new unit configuration.

### Software Layer Verification and Unit Set Configuration
- Navigate to AosEdge Dashboard → SOTA/FOTA → Layers and verify that the software layer packages have been uploaded successfully.
- In AosEdge Dashboard → SOTA/FOTA → Layers, verify that the software layer packages have been downloaded to the target VM.
- Create a Unitset_Bosch unit set to bypass verification:
	- Navigate to AosEdge Dashboard → Unit → Unit Sets.
	- Click the "+" icon to create a new unit set.
	- Configure the following settings:
		- Title: Unitset_Bosch
		- Description: Optional
		- Update Strategy: Minimize Unit Restart
		- Is Verification Set: Enable (set to True)
	- Save the unit set.
- Assign Unitset_Bosch to the provisioned VM:
	- Navigate to AosEdge Dashboard → Unit → Units.
	- Select the target VM by clicking its System ID.
	- In the Unit Details page, click Manage Unit Sets.
	- Add Unitset_Bosch to the unit and save the changes.
- Verify that the assigned unit set is reflected in the VM configuration before proceeding with software deployment.

> Note: If verification is enabled through Unitset_Bosch, software layers can be deployed without additional verification checks during the update process.

### Bind the service with subject in OEM portal to download to VM

- Open [AOS cloud docs web protal](https://docs.aosedge.tech/docs/quick-start/) and install the certificate in the WSL.
- Open [AOS Service provider(sp) web portal](https://api.aoscloud.io/account/start) click on SP and while login it will ask p12 certificate and provide sp.p12(aos-user-sp.p12) certificate.
- Navigate to AosEdge Dashboard → SOTA/FOTA → Verification Batches.
- Select the required service package to open the Package Details page.
- Click Update Approval.
- Review and validate the package details.
- Click Update Approval again to approve and validate the package.
- Navigate to AosEdge Dashboard → SOTA/FOTA → Deployment Bundles.
- Verify that the software service package has been successfully validated and is available for deployment
- Confirm that the package status is updated to Validated or Approved before proceeding with deployment.
- Verify that the deployment bundle contains the correct software service version.

- Open [AOS cloud docs web protal](https://docs.aosedge.tech/docs/quick-start/) and install the certificate in the local VM.
- Open [AOS Service provider(sp) web portal](https://api.aoscloud.io/account/start), click on OEM and while login it will ask p12 certificate and provide oem.p12(aos-user-oem.p12) certificate.
- Subject Creation and Service Assignment
	- Navigate to AosEdge Dashboard → Units → Subjects.
	- Click the "+" button to create a new subject.
	- In AosEdge Dashboard → Units → Subjects, select the newly created subject.
	- Add the target Unit (Primary VM) to the subject.
	- Add the required software service(s) to the subject and save the configuration.
	- Once the subject is configured, the assigned service is automatically fetched by the AOS Core running on QEMU-VM-1.
	- The service is then downloaded, configured, and started automatically on the VM.
- Monitor the service deployment logs on QEMU-VM-1:
	- journalctl -f
- Verify that the service is running successfully and that no deployment errors are reported in the logs.

### SDV-Application-Compilation-and-Configuration

- Go to playground.digital.auto and sign in.[link](https://playground.digital.auto)
- Navigate to Vehicle Models.
- Select the EPAM Integration vehicle model.[link](https://playground.digital.auto/model/67f76c0d8c609a0027662a69)
- Open PrototypeLibrary and choose the EV Range Extender application.
- Click AOS Cloud Deployment to open the SDV application deployment page.
- From the dropdown list of available SDV applications, select the desired application (for example, EV Range Extender).
- Upload the aos-user-sp.p12 certificate.
- Click Build and Deploy to start the deployment.
- Complete the post-deployment steps:
	- Verify that the software layer has been downloaded successfully.
	- Bind the service with the subject in the OEM Portal and download it to the VM.

### Start the hardware simulator

With both VMs running and the EV Range Extender application executed from the digital.auto playground against your registered runtime, launch the host-side hardware simulator (the Tk simulator) so you can drive the inputs manually:

```bash
python3 -m pip install -r hardware-sim/requirements.txt
./hardware-sim/setup.sh
python hardware-sim/pytk_hwsim.py
```
⚠️ Disclaimer: Please follow the above steps , if pytek-simulator does not display the values, hwsim should be re-launched.

See the [hardware simulator README](hardware-sim/README.md) for the full control and status map.

For nodered hardware simulator there is helper script to launch [here](hardware-sim/node-red/README.md)

### Steps to demo
1. Complete the "SDV-Application-Compilation-and-Configuration" steps
1. Start the hardware simulator.
2. Once the hardware simulator is running, launch the Playground application (SDV application).
3. Click the Start button in the hardware simulator to begin battery discharge simulation.
### Observe the threshold-based behaviour:
1. When the battery level reaches 50%, the HVAC fan is automatically turned off.
2. When the battery level reaches 30%, additional power-saving measures are applied, and the seat heating/cooling functions are turned off.
3. When the HVAC fan is turned off, a slight increase in the estimated driving range can be observed.
4. When the seat heating/cooling functions are also disabled, the estimated driving range increases furthe
5. Log in to QEMU-VM-1 using SSH:
	ssh root@10.0.0.100
6. Monitor the application logs by running:
	journalctl -f | grep "range-ext"
7. Check the application level logs

### Signal Flow and Internals

The demo runs as a closed loop across host, virtual machines, and the playground runtime.

1. **Hardware simulator (host side)** publishes battery and cabin control values.
2. **QEMU-VM-1 runtime stack** receives battery values and updates the vehicle signal broker.
3. **Vehicle signal broker (Kuksa)** stores and distributes current vehicle values used by the application and runtime services.
4. **Bridge layer** transfers cabin-related signal and  updates between QEMU-VM-1 and QEMU-VM-2 so both compute domains stay synchronized.
5. **VM2 ECU services** apply HVAC and seat actions and publish actuator status back to the dashboard.

---

## Phase 2 — Physical Hardware

![Architecture Phase 2](./images/architecture_phase2.svg)

Phase 2 replaces the virtual machines with **automotive hardware**. The cloud layer and application logic stay identical — this phase validates that the same software runs correctly on the hardware an OEM would actually put in a vehicle.

A key addition in Phase 2 is the **End ECU layer** (STM32), which represents the deepest level of the vehicle's electrical architecture — the microcontrollers directly attached to physical sensors and actuators like motors, lights, and HVAC.

### How the flow works

```
1. Same cloud flow as Phase 1 (AosCloud → App Registry → App Fetching)
        ↓
2. App runs on a NXP S32G2 board (automotive-grade processor)
        ↓
3. HPC communicates with Zonal Raspberry Pi over eclipse-score / SOME-IP
        ↓
4. Zonal Pi communicates with End STM32 microcontroller over eclipse-zenoh
        ↓
5. STM32 directly controls HVAC, infotainment display, seat ventilation
```

### What's running in each layer

| Layer | Hardware | Software | What It Does |
|---|---|---|---|
|  **AosCloud** | — | Fleet Management + App Registry | Same as Phase 1 |
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

### Additional Eclipse components inside the blueprint phase 2

| Component | Role |
|---|---|
| `eclipse-autosd` | Automotive grade Linux OS from RedHat |
| `eclipse-zenoh` | Modern communication protocol designed for SDV |
| `eclipse-threadx` | Safe RTOS for microcontrollers |

---

## Phase Comparison

| | Phase 1 | Phase 2 |
|---|---|---|
| **Goal** | Develop & validate logic | Validate on real hardware |
| **HPC** | Linux VM (QEMU) | NXP S32G2 |
| **Zonal** | Not present | Raspberry Pi |
| **End ECU** | Linux VM (QEMU) | STM32 |
| **HPC ↔ End comms** | Eclipse Zenoh | — |
| **HPC ↔ Zonal comms** | — | Eclipse SCore / SOME-IP |
| **Zonal ↔ End comms** | — | Eclipse Zenoh |
| **Setup complexity** | Low — just Docker | Requires hardware |
| **Best for** | App development, signal testing, OEM demos | Pre-production validation |

---

## Project Resources

| Resource | Link |
|---|---|
| digital.auto Playground | [playground.digital.auto](https://playground.digital.auto) |
| Development Repository | [eclipse-autowrx/epam-service-connector](https://github.com/eclipse-autowrx/epam-service-connector) |
| digital.auto Website | [www.digital.auto](https://www.digital.auto) |
| Aos Cloud | https://api.aoscloud.io/account/start |
---
