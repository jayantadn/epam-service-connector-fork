#!/bin/bash
# Copyright (c) 2026 Eclipse Foundation.
#
# This program and the accompanying materials are made available under the
# terms of the MIT License which is available at
# https://opensource.org/licenses/MIT.
#
# SPDX-License-Identifier: MIT

# Starts the Zenoh bridge and Node-RED together, forwarding container
# signals to both so `docker stop` shuts the whole stack down cleanly.
set -e

python3 zenoh_bus_bridge.py &
BRIDGE_PID=$!

term() {
  kill -TERM "$BRIDGE_PID" 2>/dev/null || true
  kill -TERM "$NODE_RED_PID" 2>/dev/null || true
}
trap term TERM INT

./node_modules/.bin/node-red --userDir . --flowFile flows/ev-range-dashboard.json &
NODE_RED_PID=$!

wait "$NODE_RED_PID"
EXIT_CODE=$?
kill -TERM "$BRIDGE_PID" 2>/dev/null || true
exit "$EXIT_CODE"
