# EV Range Dashboard — Node-RED Runtime

Local Node-RED dashboard for the EV range extender simulation.
Communicates with VM ECUs over raw TCP JSON-line transport via the built-in bridge.

## Prerequisites

- Node.js 18 or later
- VMs running (or QEMU images started with `vm1_launch.sh` / `vm2_launch.sh`)
	- VM1 `192.168.100.10:7460` — BMS service
	- VM2 `192.168.100.11:7461` — HVAC ECU
	- VM2 `192.168.100.11:7462` — Seat ECU

## Quick Start

```bash
cd hardware-sim/node-red
npm install          # first time only
npm run dashboard
```

Dashboard URLs:
- **UI:** http://127.0.0.1:1880/ev-range/page/control
- **Editor:** http://127.0.0.1:1880
- **Bridge API:** http://127.0.0.1:1881/state

## Architecture

```
Browser → Node-RED (port 1880)
							│
							▼
			 tcp_bus_bridge.js  (HTTP API on port 1881)
				 ├── VM1 BMS   192.168.100.10:7460
				 ├── VM2 HVAC  192.168.100.11:7461
				 └── VM2 Seat  192.168.100.11:7462
```

The bridge exposes:
- `GET  /state`    — returns last-known signals + ECU reverse-status
- `POST /publish`  — routes a `{ key, value }` message to the correct VM TCP endpoint

## Files

| File | Purpose |
|---|---|
| `package.json` | npm project + `npm run dashboard` script |
| `settings.js` | Node-RED runtime config (port, flow file) |
| `tcp_bus_bridge.js` | TCP bridge — connects to VMs, exposes HTTP API |
| `flows/ev-range-dashboard.json` | Node-RED flow (all widgets + logic) |

## Environment Overrides

| Variable | Default | Description |
|---|---|---|
| `PORT` | `1880` | Node-RED listen port |
| `BRIDGE_PORT` | `1881` | Bridge HTTP API port |
| `VM1_HOST` | `192.168.100.10` | VM1 host |
| `VM1_PORT` | `7460` | VM1 BMS port |
| `VM2_HVAC_HOST` | `192.168.100.11` | VM2 HVAC host |
| `VM2_HVAC_PORT` | `7461` | VM2 HVAC port |
| `VM2_SEAT_HOST` | `192.168.100.11` | VM2 Seat host |
| `VM2_SEAT_PORT` | `7462` | VM2 Seat port |
