#!/bin/bash
set -e

echo "======================================"
echo "[INFO] MULTI-VM SETUP STARTED"
echo "======================================"

# -------- CONFIG --------
BASE_URL="https://cloud-images.ubuntu.com/noble/current/"
IMAGE_NAME="noble-server-cloudimg-amd64.img"

INPUT_DIR="input"
OUTPUT_DIR="output"
IMAGE_DIR="$OUTPUT_DIR/images"

VM1_IMG="$OUTPUT_DIR/vm1.qcow2"
VM2_IMG="$OUTPUT_DIR/vm2.qcow2"

SEED1="$OUTPUT_DIR/seed1.img"
SEED2="$OUTPUT_DIR/seed2.img"

# -------- CHECK INPUT FILES --------
if [ ! -f "$INPUT_DIR/user-data-vm1" ] || [ ! -f "$INPUT_DIR/meta-data-vm1" ]; then
    echo "[ERROR] VM1 cloud-init files missing"
    exit 1
fi

if [ ! -f "$INPUT_DIR/user-data-vm2" ] || [ ! -f "$INPUT_DIR/meta-data-vm2" ]; then
    echo "[ERROR] VM2 cloud-init files missing"
    exit 1
fi

# -------- INSTALL DEPENDENCIES --------
echo "[INFO] Installing dependencies..."
sudo apt update
sudo apt install -y qemu-system qemu-utils cloud-image-utils wget bridge-utils

# -------- KVM CHECK --------
echo "[INFO] Checking KVM..."
if [ -e /dev/kvm ] && groups | grep -q '\bkvm\b'; then
    echo "[SUCCESS] KVM enabled"
else
    echo "[WARNING] KVM not enabled (slower VM)"
fi

# -------- CREATE DIRS --------
mkdir -p "$IMAGE_DIR"

BASE_IMAGE="$IMAGE_DIR/$IMAGE_NAME"

# -------- DOWNLOAD BASE IMAGE --------
if [ ! -f "$BASE_IMAGE" ]; then
    echo "[INFO] Downloading Ubuntu image..."
    wget -O "$BASE_IMAGE" "$BASE_URL/$IMAGE_NAME"
else
    echo "[INFO] Base image already exists"
fi

# -------- CREATE VM DISKS --------
if [ ! -f "$VM1_IMG" ]; then
    echo "[INFO] Creating VM1 disk..."
    qemu-img create -f qcow2 -F qcow2 -b "images/$IMAGE_NAME" "$VM1_IMG" 30G
fi

if [ ! -f "$VM2_IMG" ]; then
    echo "[INFO] Creating VM2 disk..."
    qemu-img create -f qcow2 -F qcow2 -b "images/$IMAGE_NAME" "$VM2_IMG" 30G
fi

# -------- CREATE SEED IMAGES --------
echo "[INFO] Creating cloud-init seeds..."
cloud-localds --network-config "$INPUT_DIR/network-vm1.yaml" "$SEED1" "$INPUT_DIR/user-data-vm1" "$INPUT_DIR/meta-data-vm1"
cloud-localds --network-config "$INPUT_DIR/network-vm2.yaml" "$SEED2" "$INPUT_DIR/user-data-vm2" "$INPUT_DIR/meta-data-vm2"

echo "======================================"
echo "[SUCCESS] SETUP COMPLETED"
echo "======================================"


#!/bin/bash
set -e
echo "[INFO] Creating Bridge and TAP interfaces..."

# 1. Create and enable the bridge
sudo ip link add br0 type bridge 2>/dev/null || true
sudo ip link set br0 up

# 2. Assign IP to bridge (AFTER creation)
sudo ip addr add 192.168.100.1/24 dev br0 2>/dev/null || true

# 3. Create TAP1 and attach to bridge
sudo ip tuntap add dev tap1 mode tap user $USER 2>/dev/null || true
sudo ip link set tap1 master br0
sudo ip link set tap1 up

# 4. Create TAP2 and attach to bridge
sudo ip tuntap add dev tap2 mode tap user $USER 2>/dev/null || true
sudo ip link set tap2 master br0
sudo ip link set tap2 up

# 5. Enable forwarding (IMPORTANT)
sudo sysctl -w net.ipv4.ip_forward=1

# 6. Allow traffic (IMPORTANT)
sudo iptables -A INPUT -i br0 -j ACCEPT 2>/dev/null || true
sudo iptables -A FORWARD -i br0 -j ACCEPT 2>/dev/null || true
sudo iptables -A FORWARD -o br0 -j ACCEPT 2>/dev/null || true

echo "[SUCCESS] Bridge and TAP interfaces ready"

echo "======================================"
echo "[SUCCESS] Network setup completed"
echo "======================================"

echo ""
# Hand off control to the VM1 launch script (This will trigger your background QEMU and polling spinner!)
./vm1_launch.sh

# ==========================================
# Automated Polling: Wait for Docker Container
# ==========================================
echo ""
echo "[INFO] Waiting for SDV Runtime to download and launch..."
echo "       (This usually takes 2-3 minutes. Please wait...)"

# Silently knock on port 55555 every 5 seconds until it answers
while ! bash -c "echo > /dev/tcp/192.168.100.10/55555" 2>/dev/null; do
    echo -ne "."
    sleep 5
done

echo -e "\n"
echo "========================================================"
echo " [SUCCESS] VM1 setup completed and runtime created! "
echo "========================================================"
echo ""
echo " For launching VM2, open a NEW terminal and run:"
echo "   ./vm2_launch.sh"
echo ""
echo " To log into VM1, use:"
echo " ssh ubuntu@192.168.100.10"
echo ""