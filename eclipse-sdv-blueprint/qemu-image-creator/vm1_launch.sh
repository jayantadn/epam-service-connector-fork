# SPDX-License-Identifier: Apache-2.0
#!/bin/bash
set -e

echo "======================================"
echo "[INFO] LAUNCHING VM1"
echo "======================================"

VM_IMG="output/vm1.qcow2"
SEED_IMG="output/seed1.img"

if [ ! -f "$VM_IMG" ] || [ ! -f "$SEED_IMG" ]; then
    echo "[ERROR] VM1 files missing. Run ./setup.sh first"
    exit 1
fi

if [ -e /dev/kvm ] && groups | grep -q '\bkvm\b'; then
    KVM_FLAG="-enable-kvm"
    CPU_FLAG="-cpu host"
else
    KVM_FLAG=""
    CPU_FLAG="-cpu qemu64"
fi

echo "Starting VM1 silently in the background..."

# Launch QEMU completely silently in the background
qemu-system-x86_64 \
    $KVM_FLAG \
    $CPU_FLAG \
    -m 2048 \
    -smp 2 \
    -drive file=$VM_IMG,format=qcow2,if=virtio \
    -drive file=$SEED_IMG,format=raw,if=virtio \
    -netdev tap,id=net0,ifname=tap1,script=no,downscript=no \
    -device virtio-net-pci,netdev=net0,mac=52:54:00:12:34:51 \
    -netdev user,id=net1 \
    -device virtio-net-pci,netdev=net1,mac=52:54:00:12:34:61 \
    -display none \
    -daemonize