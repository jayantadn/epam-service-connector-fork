***

# QEMU Multi-VM Network

This script provides an automated, zero-touch deployment of a multi-node virtual network using QEMU, KVM and cloud-init. It provisions two isolated Ubuntu VMs that communicate over a private Layer 2 bridge while seamlessly maintaining full outbound internet access. It serves as a perfect lightweight, reproducible sandbox for testing distributed systems or Linux networking directly on your local machine.

***
### Script Capabilities
This script automatically provisions two VMs with the following capabilities:
1. **Inter-VM Communication:** Connected via a private Layer 2 virtual bridge (`br0`) and TAP interfaces.
2. **Host Isolation:** The host operating system's routing table and primary network interfaces remain uncompromised.

---

##  Core Components

* **`setup.sh`:** Downloads the base Ubuntu Cloud Image, allocates the `.qcow2` virtual disks, and generates the `cloud-init` seed images.
* **`network.sh`:** Configures the virtual network infrastructure by instantiating a Linux bridge, creating TAP interfaces, and applying required `iptables` forwarding rules on the host.
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

Execute the setup script to download the base Ubuntu cloud image, allocate the `qcow2` virtual disks, and generate the `cloud-init` seed images containing the network configurations.

```bash
./setup.sh
```

**Step 2: Configure the Virtual Network Infrastructure**

Run the network script with elevated privileges to instantiate the virtual bridge (`br0`), attach the TAP interfaces, and configure NAT/IP forwarding on the host.

```bash
./network.sh
```

**Step 3: Boot VM 1**

Initialize the first QEMU instance. The VM's serial console will attach directly to your current terminal session.

```bash
./vm1_launch.sh
```

**Step 4: Boot VM 2**

Open a **new, separate terminal window or tab** (to maintain parallel console sessions), and initialize the second QEMU instance.

```bash
./vm2_launch.sh
```

---

##  How to Test It

Once both VM's have finished booting up, you will see a login prompt. 
* **Username:** `ubuntu`
* **Password:** `ubuntu`

To prove they are connected, go to VM 1 and "ping" VM 2 by typing:

```bash
ping 192.168.100.11
```


To prove they are connected, go to VM 2  and "ping" VM 1 by typing:

```bash
ping 192.168.100.10
```
If the packets reached from VM 1 to 2 or vice versa ,the 2 VM 's communicate each other 

---

## Troubleshooting

WSL's internal Netfilter firewall is actively stopping the communication by dropping all packets trying to cross your virtual bridge. 

Running overrides this restriction, forcing WSL to allow the traffic between the VMs.(need to be done on the WSL terminal not on VMs)

```bash
sudo iptables -A FORWARD -i br0 -o br0 -j ACCEPT
```

**How do I turn them off VM's**

When you are done playing with the virtual computers, you can safely shut them down by typing this inside their terminals:
```bash
sudo poweroff
```
