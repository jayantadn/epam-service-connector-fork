# QEMU Multi-VM Network

This script provides an automated, zero-touch deployment of a multi-node virtual network using QEMU, KVM and cloud-init. It provisions two isolated Ubuntu VMs that communicate over a private Layer 2 bridge while seamlessly maintaining full outbound internet access. The main aim is to create a runtime named ev-range on the digital.auto playground that runs on VM1. The host, VM1, and VM2 can communicate with each other. Prototype for the ev-range runtime: [Playground Prototype](https://playground.digital.auto/model/67f76c0d8c609a0027662a69/library/prototype/69ce30f438bb8e98f0af5ac8/dashboard)

### Script Capabilities
This script automatically provisions two VMs with the following capabilities:
1. **Inter-VM Communication:** Connected via a private Layer 2 virtual bridge (`br0`) and TAP interfaces.
2. **Host Isolation:** The host operating system's routing table and primary network interfaces remain uncompromised.

---
## IP Addresses

* **Host:** 192.168.100.1
* **VM1:** 192.168.100.10
* **VM2:** 192.168.100.11

## Core Components

* **`setup.sh`:** Downloads the base Ubuntu Cloud Image, allocates the `.qcow2` virtual disks, and generates the `cloud-init` seed images and establishes network connectivity between the VMs. VM1 is launched automatically.
* **`input/` directory:** Contains declarative `cloud-init` YAML files (`user-data`, `meta-data`, and network configs) to automatically inject hostnames (`vm1`, `vm2`) and static IP addresses (`192.168.100.10/24`, `192.168.100.11/24`) during the initial boot sequence.
* **`vm1_launch.sh` & `vm2_launch.sh`:** The QEMU execution scripts that initialize the KVM guests, define system resources, and bind the virtual NICs to the correct network backends.

## How to Run the Script

Follow these steps to provision the infrastructure and initialize the virtual network. 

**Pre-requisite: Grant Execution Permissions**

Before running the deployment, ensure all bash scripts have the correct execution permissions:

```bash
chmod +x *.sh
```

**Step 1: Provision the VMs (QEMU & Cloud-Init)**

Execute the setup script to download the base Ubuntu cloud image, allocate the `qcow2` virtual disks, and generate the `cloud-init` seed images containing the network configurations and launches VM1 automatically. The runtime is created and verified during the execution of setup.sh.

```bash
./setup.sh
```
**Step 2: Boot VM 2**

Open a **new, separate terminal window or tab** (to maintain parallel console sessions), and initialize the second QEMU instance.

```bash
./vm2_launch.sh
```
**Step 3: Access VM 1**

```bash
ssh ubuntu@192.168.100.10
```

**Step 4: Access VM 2**

```bash
ssh ubuntu@192.168.100.11
```
### Verify the SDV Runtime

Log into VM1 and check if the Eclipse SDV Docker container is actively running:

```bash
docker ps
```
---

## How to Test It

Once both VMs have finished booting, you will see a login prompt. 
* **Username:** `ubuntu`
* **Password:** `ubuntu`

To prove they are connected, From VM1, ping VM2:

```bash
ping 192.168.100.11
```

To prove they are connected, From VM2, ping VM1:

```bash
ping 192.168.100.10
```
If packets are successfully transmitted between VM1 and VM2, it confirms that the two VMs can communicate with each other.

---

## Troubleshooting

WSL's internal Netfilter firewall is actively stopping the communication by dropping all packets trying to cross your virtual bridge. 

Running the following command overrides this restriction, forcing WSL to allow traffic between the VMs (this must be executed on the WSL host, not inside the VMs).

```bash
sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT
```

**How to shut down the VMs**

When you are done playing with the virtual computers, you can safely shut them down by typing this inside their terminals:
```bash
sudo poweroff
```