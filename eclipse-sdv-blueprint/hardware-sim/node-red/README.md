# EV Range Dashboard — Node-RED Runtime

Local Node-RED dashboard for the EV range extender simulation.Communicates with VM ECUs over Eclipse Zenoh via the built-in HTTP bridge.

## Prerequisites

- Node.js 18 or later
- Python 3
- The Python package `eclipse-zenoh`
- A running AOS Zenoh router on port `7447`
- The AOS ECU services running in the QEMU VMs

## Run the Dashboard

Run these commands from the repository root:

```bash
cd /home/goutham/epam-service-connector-fork/eclipse-sdv-blueprint
```

### 1. Install dependencies

Create or activate the Python environment used by the hardware simulator, then
install the Zenoh package:

```bash
cd hardware-sim
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
l
```

Run `npm install` only once, unless `package.json` changes.

### 2. Select the Zenoh router

Node-RED must use the same Zenoh router that `hardware-sim/pytk_hwsim.py` uses.
For the AOS QEMU main VM created by `aos_vm.sh`, the usual host address is
`10.0.0.100`:

```bash
export ZENOH_ROUTER=tcp/10.0.0.100:7447
```

If the router is exposed on the local machine instead, use:

```bash
export ZENOH_ROUTER=tcp/127.0.0.1:7447
```cd node-red
npm install

Do not type `<router-host>` literally. Replace it with a real hostname or IP
address, for example:

```bash
export ZENOH_ROUTER=tcp/10.0.0.100:7447
```

### 3. Start Node-RED

Make sure the current directory is `hardware-sim/node-red` before running the
command:

```bash
pwd
# Expected suffix: /eclipse-sdv-blueprint/hardware-sim/node-red

npm run dashboard
```

This starts both processes:

- `zenoh_bus_bridge.py`, the HTTP-to-Zenoh bridge on port `1881`
- Node-RED and the dashboard on port `1880`

The bridge retries the Zenoh connection if the router is not ready yet. The
dashboard can open during that period, but controls return `503` until the
router becomes reachable.

### 4. Open the UI

Open this URL in a browser:

```text
http://127.0.0.1:1880/ev-range/control
```

Other useful URLs:

```text
Node-RED editor: http://127.0.0.1:1880
Bridge state:     http://127.0.0.1:1881/state
```

Keep the terminal running while using the dashboard. Press `Ctrl+C` in that
terminal to stop Node-RED and the bridge together.

## Architecture

```
Browser → Node-RED (port 1880)
							│
							▼
			    zenoh_bus_bridge.py (HTTP API on port 1881)
				    └── same Zenoh router as pytk_hwsim.py
```

The bridge exposes:
- `GET  /state`    — returns last-known signals + ECU reverse-status
- `POST /publish`  — publishes a `{ key, value }` message on the matching Zenoh key

## Files

| File | Purpose |
|---|---|
| `package.json` | npm project + `npm run dashboard` script |
| `settings.js` | Node-RED runtime config (port, flow file) |
| `zenoh_bus_bridge.py` | Zenoh bridge — exposes the Node-RED HTTP API |
| `flows/ev-range-dashboard.json` | Node-RED flow (all widgets + logic) |

## Environment Overrides

| Variable | Default | Description |
|---|---|---|
| `PORT` | `1880` | Node-RED listen port |
| `BRIDGE_PORT` | `1881` | Bridge HTTP API port |
| `ZENOH_ROUTER` | `tcp/127.0.0.1:7447` | Same Zenoh router endpoint used by `pytk_hwsim.py` |

## Troubleshooting

**The dashboard page is empty**

Use this route, including `/control`:

```text
http://127.0.0.1:1880/ev-range/control
```

The `/ev-range/page/control` route does not load the dashboard assets correctly.

**The dashboard loads but status LEDs stay on "awaiting ECU..."**

Check that the bridge is running:

```bash
curl -s http://127.0.0.1:1881/state
```

The response should contain `"connected": {"zenoh": true}`. If it contains
`false`, check that the AOS router is reachable:

```bash
nc -vz 10.0.0.100 7447
```

Restart the dashboard after correcting `ZENOH_ROUTER`:

```bash
pkill -f 'zenoh_bus_bridge.py|node-red.*ev-range-dashboard' || true
export ZENOH_ROUTER=tcp/10.0.0.100:7447
npm run dashboard
```

Bridge logs: stdout from `zenoh_bus_bridge.py`
Node-RED logs: stdout from `npm run dashboard`

**The command says `cd: hardware-sim/node-red: No such file or directory`**

You are already inside `hardware-sim/node-red`. Do not run the `cd` command
again. Run:

```bash
pwd
npm run dashboard
```

**Controls return HTTP `503`**

The Node-RED UI is running, but the Zenoh router is unavailable. Start the AOS
router and ECU services, or set `ZENOH_ROUTER` to the router's reachable IP and
restart the dashboard.
