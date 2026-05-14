#!/usr/bin/env python3
"""Compose cloud-init user-data with auto-deployed ev-range-extender code.

Reads the human-edited template `input/user-data-vm{1,2}` and produces a
`output/user-data-vm{1,2}.composed` file that, in addition to everything
the template already does, also:

  1. Drops the entire `ev-range-extender/` Python source tree onto the
     VM under `/home/ubuntu/ev-range-extender/` via cloud-init's
     `write_files` (with base64 encoding so quoting is safe).

  2. Drops six systemd unit files under `/etc/systemd/system/` so every
     Python file deployed onto a VM starts automatically on boot.
     The user only ever runs `setup.py` (host) and `pytk_dashboard.py`
     (host); nothing is started by hand inside a VM.

         VM1                              VM2
         ev-range-bms.service             ev-range-hvac.service
         ev-range-range-ai.service        ev-range-seat.service
         ev-range-zenoh-client.service    ev-range-zenoh-publisher.service

  3. Adds `runcmd` entries to chown the source tree to ubuntu:ubuntu,
     reload systemd, and enable+start every unit in VM_UNITS for the
     selected VM.

The result is a `#cloud-config` YAML document that `cloud-localds` then
embeds into the seed image. No manual `scp` is ever needed during the
demo.

Usage (called by setup.py; can be run by hand for debugging):

    python3 tools/compose_userdata.py \\
        --template input/user-data-vm1 \\
        --output   output/user-data-vm1.composed \\
        --vm       vm1

    python3 tools/compose_userdata.py \\
        --template input/user-data-vm2 \\
        --output   output/user-data-vm2.composed \\
        --vm       vm2
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

import yaml


# ---------------------------------------------------------------------
# What ends up on each VM
# ---------------------------------------------------------------------

# Python files (and their target paths on the VM), keyed by VM.
# Zenoh is the only cross-VM transport in this repo, so there is no
# longer a shared `common/` package to deploy.

VM_FILES = {
    "vm1": {
        "ev-range-extender/vm1/range_ai.py":     "/home/ubuntu/ev-range-extender/vm1/range_ai.py",
        "ev-range-extender/vm1/bms.py":          "/home/ubuntu/ev-range-extender/vm1/bms.py",
        "ev-range-extender/vm1/zenoh_client.py": "/home/ubuntu/ev-range-extender/vm1/zenoh_client.py",
        # Phase A: the new project-local "kuksa-bridge" runs side-by-side with
        # the legacy zenoh_publisher.py / zenoh_client.py pair (different
        # Zenoh port, same wire envelope). See qemu-image-creator/kuksa-bridge/README.md.
        "kuksa-bridge/kuksa_bridge.py":          "/home/ubuntu/kuksa-bridge/kuksa_bridge.py",
        "kuksa-bridge/bridge-config-vm1.json":   "/home/ubuntu/kuksa-bridge/bridge-config.json",
    },
    "vm2": {
        "ev-range-extender/vm2/hvac_ecu.py":         "/home/ubuntu/ev-range-extender/vm2/hvac_ecu.py",
        "ev-range-extender/vm2/seat_ecu.py":         "/home/ubuntu/ev-range-extender/vm2/seat_ecu.py",
        "ev-range-extender/vm2/zenoh_publisher.py":  "/home/ubuntu/ev-range-extender/vm2/zenoh_publisher.py",
        "kuksa-bridge/kuksa_bridge.py":              "/home/ubuntu/kuksa-bridge/kuksa_bridge.py",
        "kuksa-bridge/bridge-config-vm2.json":       "/home/ubuntu/kuksa-bridge/bridge-config.json",
    },
}

# systemd unit content, keyed by (vm, unit-name). Each unit runs the
# matching ECU under user `ubuntu`, with auto-restart on failure.

def _systemd_unit(description: str, exec_cmd: str, after_kuksa_helper: str,
                  log_path: str) -> str:
    # First-boot vs subsequent-boot timing notes:
    #
    #   * On the very first boot, cloud-init still has to `pip3 install
    #     kuksa-client eclipse-zenoh ...` (2-3 min) AND `docker pull
    #     ghcr.io/eclipse-autowrx/sdv-runtime` (~250 MB, 3-5 min on slow
    #     links) before the ECU can possibly run.
    #   * On every later boot those are cached, so the unit starts
    #     in seconds.
    #
    # The unit therefore needs to (a) wait for cloud-init's runcmd phase
    # to finish (= pip install done), (b) wait for the Kuksa Databroker
    # to be listening on :55555 (= docker pull + start done), and (c) NOT
    # trip systemd's start-rate limit while it is patiently waiting for
    # those things. Without this, first-boot crash-loops put the unit
    # into `failed (start-limit-hit)` and the user has to start it by
    # hand - exactly the bug we are fixing here.
    #
    # `StandardOutput=append:` and `StandardError=append:` redirect both
    # stdout and stderr of the ExecStart process to a file. systemd
    # opens the file as PID 1 (root) before ExecStartPre runs, so the
    # resulting file is `root:root` mode 0644 - world-readable, which
    # means a plain `tail -f /tmp/ev-range-<name>.log` from the `ubuntu`
    # user works without sudo. The log lives under /tmp so it is wiped
    # on every boot - that is by design; for persistent history use
    # `journalctl -u <service>`.
    #
    # `after_kuksa_helper` ("evrange-start-runtime" / "evrange-start-
    # databroker") is a /usr/local/bin/ shell script, NOT a systemd
    # unit, so referencing `<helper>.service` would be a silent no-op.
    # The real gate is `cloud-final.service` (cloud-init's runcmd
    # phase) plus the two ExecStartPre wait loops below.
    _ = after_kuksa_helper  # kept in the signature for callsite clarity
    return f"""[Unit]
Description={description}
# cloud-final.service is cloud-init's runcmd stage. Ordering after it
# guarantees pip install + the runtime/databroker helper kick-off have
# completed before we try to import kuksa_client / connect to :55555.
After=network-online.target cloud-final.service
Wants=network-online.target
# Disable systemd's per-unit start-rate limit. On first boot this unit
# can legitimately need many minutes of retries while docker is still
# pulling the SDV Runtime image; the default (5 starts in 10 s) would
# flip the unit to `failed (start-limit-hit)` and stop retrying.
StartLimitIntervalSec=0

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/ev-range-extender
Environment=PYTHONUNBUFFERED=1
# 1) Wait up to 10 min for the local Kuksa Databroker to start listening
#    on TCP :55555. First-boot docker-pull of the SDV Runtime image can
#    take several minutes on slow links; on later boots this returns in
#    a fraction of a second.
ExecStartPre=/bin/bash -c 'for i in $(seq 1 600); do ss -ltn 2>/dev/null | grep -q ":55555 " && exit 0; sleep 1; done; exit 1'
# 2) Wait up to 5 min for the Python deps to actually be importable.
#    With After=cloud-final.service this is already true on entry, but
#    we keep the check as a hard safety net so the unit refuses to crash
#    with ModuleNotFoundError if pip install silently failed earlier.
ExecStartPre=/bin/bash -c 'for i in $(seq 1 300); do python3 -c "import kuksa_client, zenoh" >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'
ExecStart={exec_cmd}
StandardOutput=append:{log_path}
StandardError=append:{log_path}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


# Helper that builds the kuksa-bridge unit body. The exec line is the
# same on both VMs - the bridge's role (source / sink) is encoded in
# the JSON config that VM_FILES already drops at the canonical path
# /home/ubuntu/kuksa-bridge/bridge-config.json.
def _kuksa_bridge_unit(after_kuksa_helper: str) -> str:
    return _systemd_unit(
        description="EV Range Extender - kuksa-bridge (current-value mirror over Zenoh)",
        exec_cmd=(
            "/usr/bin/python3 /home/ubuntu/kuksa-bridge/kuksa_bridge.py "
            "--config /home/ubuntu/kuksa-bridge/bridge-config.json"
        ),
        after_kuksa_helper=after_kuksa_helper,
        log_path="/tmp/ev-range-kuksa-bridge.log",
    )


VM_UNITS = {
    "vm1": {
        # Battery Monitoring System: subscribes to host PyTk Zenoh
        # signals (sim/battery/*) and writes them into VM1's Kuksa.
        "ev-range-bms.service": _systemd_unit(
            description="EV Range Extender - Battery Monitoring System",
            exec_cmd="/usr/bin/python3 /home/ubuntu/ev-range-extender/vm1/bms.py",
            after_kuksa_helper="evrange-start-runtime",
            log_path="/tmp/ev-range-bms.log",
        ),
        # Range Compute AI: reads Kuksa signals on VM1 (battery + cabin
        # bridged from VM2) and publishes Vehicle.Powertrain.Range back
        # to the same broker.
        "ev-range-range-ai.service": _systemd_unit(
            description="EV Range Extender - Range Compute AI",
            exec_cmd="/usr/bin/python3 /home/ubuntu/ev-range-extender/vm1/range_ai.py",
            after_kuksa_helper="evrange-start-runtime",
            log_path="/tmp/ev-range-range-ai.log",
        ),
        # Legacy Zenoh -> Kuksa bridge (VM1 side): receives cabin signals
        # published by zenoh_publisher.service on VM2 and writes them
        # into VM1's Kuksa so range_ai.py can use them. Kept active
        # alongside the new kuksa-bridge for the Phase A cutover - they
        # use different Zenoh ports (7447 vs 7448) so they cannot collide.
        "ev-range-zenoh-client.service": _systemd_unit(
            description="EV Range Extender - VM2->VM1 Zenoh bridge (subscriber, legacy)",
            exec_cmd="/usr/bin/python3 /home/ubuntu/ev-range-extender/vm1/zenoh_client.py",
            after_kuksa_helper="evrange-start-runtime",
            log_path="/tmp/ev-range-zenoh-client.log",
        ),
        # New project-local kuksa-bridge (VM1 sink role). See
        # qemu-image-creator/kuksa-bridge/README.md for the rationale.
        "ev-range-kuksa-bridge.service": _kuksa_bridge_unit(
            after_kuksa_helper="evrange-start-runtime",
        ),
    },
    "vm2": {
        # HVAC ECU: subscribes to host PyTk Zenoh (sim/cabin/*) and
        # writes Vehicle.Cabin.HVAC.* into VM2's Kuksa.
        "ev-range-hvac.service": _systemd_unit(
            description="EV Range Extender - HVAC ECU",
            exec_cmd="/usr/bin/python3 /home/ubuntu/ev-range-extender/vm2/hvac_ecu.py",
            after_kuksa_helper="evrange-start-databroker",
            log_path="/tmp/ev-range-hvac.log",
        ),
        # Seat Control Module: subscribes to host PyTk Zenoh
        # (sim/seat/*) and writes Vehicle.Cabin.Seat.* into VM2's Kuksa.
        "ev-range-seat.service": _systemd_unit(
            description="EV Range Extender - Seat Control Module",
            exec_cmd="/usr/bin/python3 /home/ubuntu/ev-range-extender/vm2/seat_ecu.py",
            after_kuksa_helper="evrange-start-databroker",
            log_path="/tmp/ev-range-seat.log",
        ),
        # Legacy Kuksa -> Zenoh bridge (VM2 side): forwards VM2's cabin
        # Kuksa signals over Zenoh so zenoh_client.service on VM1 can
        # ingest them into VM1's Kuksa for the range model. Kept active
        # alongside the new kuksa-bridge during the Phase A cutover.
        "ev-range-zenoh-publisher.service": _systemd_unit(
            description="EV Range Extender - VM2->VM1 Zenoh bridge (publisher, legacy)",
            exec_cmd="/usr/bin/python3 /home/ubuntu/ev-range-extender/vm2/zenoh_publisher.py",
            after_kuksa_helper="evrange-start-databroker",
            log_path="/tmp/ev-range-zenoh-publisher.log",
        ),
        # New project-local kuksa-bridge (VM2 source role).
        "ev-range-kuksa-bridge.service": _kuksa_bridge_unit(
            after_kuksa_helper="evrange-start-databroker",
        ),
    },
}


# ---------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------


CLOUD_CONFIG_HEADER = "#cloud-config\n"


def load_template(path: Path) -> dict:
    text = path.read_text()
    # Drop the cloud-config magic header before YAML parsing
    if text.startswith("#cloud-config"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return yaml.safe_load(text) or {}


def encode_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def make_write_file_entry(host_path: Path, vm_path: str, mode: str = "0644",
                          owner: str = "ubuntu:ubuntu") -> dict:
    """Build one cloud-init `write_files` entry from a host file.

    `defer: true` is critical when `owner` references a non-root user
    that cloud-init creates later (`ubuntu:ubuntu`). Without it,
    write_files runs before cc_users_groups, the chown fails with
    "user does not exist", and cloud-init silently skips the file
    (leaving an empty parent directory behind).
    """
    raw = host_path.read_bytes()
    entry = {
        "path": vm_path,
        "permissions": mode,
        "owner": owner,
        "encoding": "b64",
        "content": encode_b64(raw),
    }
    if not owner.startswith("root:"):
        entry["defer"] = True
    return entry


def make_inline_write_file(vm_path: str, content: str, mode: str = "0644",
                           owner: str = "root:root") -> dict:
    """Build a cloud-init `write_files` entry with literal text content."""
    return {
        "path": vm_path,
        "permissions": mode,
        "owner": owner,
        "encoding": "b64",
        "content": encode_b64(content.encode("utf-8")),
    }


# ---------------------------------------------------------------------
# Main composer
# ---------------------------------------------------------------------


def compose(template: Path, output: Path, vm: str, repo_root: Path) -> None:
    if vm not in VM_FILES:
        raise SystemExit(f"unknown --vm {vm!r}; expected one of {list(VM_FILES)}")

    cfg = load_template(template)

    # --- write_files: source code + systemd units ---
    write_files = list(cfg.get("write_files") or [])

    # Per-VM application files (executable bit set so `python3 file.py` and
    # also `./file.py` work consistently)
    for src, dst in VM_FILES[vm].items():
        host_path = repo_root / src
        if not host_path.exists():
            print(f"[compose] WARN missing source file {host_path}", file=sys.stderr)
            continue
        write_files.append(make_write_file_entry(host_path, dst, mode="0755"))

    # systemd units (root:root, 0644)
    for unit_name, unit_content in VM_UNITS[vm].items():
        write_files.append(make_inline_write_file(
            f"/etc/systemd/system/{unit_name}",
            unit_content,
            mode="0644",
            owner="root:root",
        ))

    cfg["write_files"] = write_files

    # --- runcmd: chown source tree, daemon-reload, enable+start units ---
    runcmd = list(cfg.get("runcmd") or [])

    runcmd.append(["chown", "-R", "ubuntu:ubuntu", "/home/ubuntu/ev-range-extender"])
    runcmd.append(["systemctl", "daemon-reload"])

    for unit_name in VM_UNITS[vm].keys():
        runcmd.append(["systemctl", "enable", unit_name])
        # Start in background so cloud-init does not block on the ECU
        # ExecStartPre wait-for-databroker loop.
        runcmd.append(f"systemctl start --no-block {unit_name}")

    cfg["runcmd"] = runcmd

    # --- write composed file ---
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w") as f:
        f.write(CLOUD_CONFIG_HEADER)
        # default_flow_style=False keeps the YAML block-style (readable);
        # width=4096 avoids accidental line-wrapping inside long content
        # strings (we still keep them short via base64).
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False, width=4096)

    print(f"[compose] wrote {output} (write_files={len(write_files)}, "
          f"runcmd={len(runcmd)} for {vm})")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--template", required=True, type=Path,
                   help="Path to the template user-data file (e.g. input/user-data-vm1)")
    p.add_argument("--output", required=True, type=Path,
                   help="Path to write the composed user-data file")
    p.add_argument("--vm", required=True, choices=sorted(VM_FILES.keys()),
                   help="Which VM the composed file is for")
    p.add_argument("--repo-root", default=None, type=Path,
                   help="Path to qemu-image-creator/ (default: parent of this script)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    repo_root = args.repo_root or Path(__file__).resolve().parent.parent
    compose(args.template, args.output, args.vm, repo_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
