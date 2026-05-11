#!/usr/bin/env python3
"""Compose cloud-init user-data with auto-deployed ev-range-extender code.

Reads the human-edited template `input/user-data-vm{1,2}` and produces a
`output/user-data-vm{1,2}.composed` file that, in addition to everything
the template already does, also:

  1. Drops the entire `ev-range-extender/` Python source tree onto the
     VM under `/home/ubuntu/ev-range-extender/` via cloud-init's
     `write_files` (with base64 encoding so quoting is safe).

  2. Drops three systemd unit files under `/etc/systemd/system/` for the
     three new ECUs (`ev-range-bms.service` on VM1; `ev-range-hvac.service`
     + `ev-range-seat.service` on VM2) so they start automatically on
     boot.

  3. Adds `runcmd` entries to chown the source tree to ubuntu:ubuntu,
     reload systemd, and enable+start the matching ECU services.

The result is a `#cloud-config` YAML document that `cloud-localds` then
embeds into the seed image. No manual `scp` is ever needed during the
demo.

Usage (called by setup.sh; can be run by hand for debugging):

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
    },
    "vm2": {
        "ev-range-extender/vm2/hvac_ecu.py":         "/home/ubuntu/ev-range-extender/vm2/hvac_ecu.py",
        "ev-range-extender/vm2/seat_ecu.py":         "/home/ubuntu/ev-range-extender/vm2/seat_ecu.py",
        "ev-range-extender/vm2/zenoh_publisher.py":  "/home/ubuntu/ev-range-extender/vm2/zenoh_publisher.py",
    },
}

# systemd unit content, keyed by (vm, unit-name). Each unit runs the
# matching ECU under user `ubuntu`, with auto-restart on failure.

def _systemd_unit(description: str, exec_cmd: str, after_kuksa_helper: str,
                  log_path: str) -> str:
    # `StandardOutput=append:` and `StandardError=append:` redirect both
    # stdout and stderr of the ExecStart process to a file. systemd opens
    # the file as PID 1 (root) before ExecStartPre runs, so the resulting
    # file is `root:root` mode 0644 - world-readable, which means a plain
    # `tail -f /tmp/ev-range-<name>.log` from the `ubuntu` user works
    # without sudo. The log lives under /tmp so it is wiped on every
    # boot - that is by design; for persistent history use
    # `journalctl -u <service>`.
    return f"""[Unit]
Description={description}
After=network-online.target {after_kuksa_helper}.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/ev-range-extender
Environment=PYTHONUNBUFFERED=1
# Wait until the local Kuksa Databroker is listening before starting.
# kuksa-client otherwise FATALs with "connection refused" on early boot.
ExecStartPre=/bin/bash -c 'for i in $(seq 1 120); do ss -ltn | grep -q ":55555 " && exit 0; sleep 1; done; exit 1'
ExecStart={exec_cmd}
StandardOutput=append:{log_path}
StandardError=append:{log_path}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


VM_UNITS = {
    "vm1": {
        "ev-range-bms.service": _systemd_unit(
            description="EV Range Extender - Battery Monitoring System",
            exec_cmd="/usr/bin/python3 /home/ubuntu/ev-range-extender/vm1/bms.py",
            after_kuksa_helper="evrange-start-runtime",
            log_path="/tmp/ev-range-bms.log",
        ),
    },
    "vm2": {
        "ev-range-hvac.service": _systemd_unit(
            description="EV Range Extender - HVAC ECU",
            exec_cmd="/usr/bin/python3 /home/ubuntu/ev-range-extender/vm2/hvac_ecu.py",
            after_kuksa_helper="evrange-start-databroker",
            log_path="/tmp/ev-range-hvac.log",
        ),
        "ev-range-seat.service": _systemd_unit(
            description="EV Range Extender - Seat Control Module",
            exec_cmd="/usr/bin/python3 /home/ubuntu/ev-range-extender/vm2/seat_ecu.py",
            after_kuksa_helper="evrange-start-databroker",
            log_path="/tmp/ev-range-seat.log",
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
