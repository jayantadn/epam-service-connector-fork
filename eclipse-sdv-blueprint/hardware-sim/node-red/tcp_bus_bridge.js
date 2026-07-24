#!/usr/bin/env node
// Copyright (c) 2026 Eclipse Foundation.
//
// This program and the accompanying materials are made available under the
// terms of the MIT License which is available at
// https://opensource.org/licenses/MIT.
//
// SPDX-License-Identifier: MIT
"use strict";

const http = require("http");
const net = require("net");

const BRIDGE_PORT = Number(process.env.BRIDGE_PORT || 1881);

const endpoints = {
  "vm1-bms": {
    host: process.env.VM1_HOST || "192.168.100.10",
    port: Number(process.env.VM1_PORT || 7460)
  },
  "vm2-hvac": {
    host: process.env.VM2_HVAC_HOST || "192.168.100.11",
    port: Number(process.env.VM2_HVAC_PORT || 7461)
  },
  "vm2-seat": {
    host: process.env.VM2_SEAT_HOST || "192.168.100.11",
    port: Number(process.env.VM2_SEAT_PORT || 7462)
  }
};

const state = {
  connected: {
    "vm1-bms": false,
    "vm2-hvac": false,
    "vm2-seat": false
  },
  signals: {},
  statuses: {}
};

const clients = new Map();
const reconnectTimers = new Map();

function ts() {
  return new Date().toISOString();
}

function log(level, msg, extra) {
  if (extra) {
    console.log(`[${ts()}] [tcp-bridge] [${level}] ${msg} ${JSON.stringify(extra)}`);
  } else {
    console.log(`[${ts()}] [tcp-bridge] [${level}] ${msg}`);
  }
}

function routeForKey(key) {
  if (!key) return null;
  if (key.startsWith("sim/battery/")) return "vm1-bms";
  if (key.startsWith("sim/cabin/seat/")) return "vm2-seat";
  if (key === "sim/cabin/temp" || key === "sim/cabin/fan-speed") return "vm2-hvac";
  return null;
}

function ingestInbound(name, obj) {
  if (obj && typeof obj === "object") {
    if (obj.topic) {
      const topic = obj.topic;
      if (topic === "dash/status/seat" && obj.key) {
        const seatKey = `${topic}#${obj.key}`;
        state.statuses[seatKey] = {
          ...obj,
          _endpoint: name,
          _ts: Date.now()
        };
      }
      state.statuses[topic] = {
        ...obj,
        _endpoint: name,
        _ts: Date.now()
      };
    }
    if (obj.key && obj.value !== undefined) {
      state.signals[obj.key] = obj.value;
    }
  }
}

function scheduleReconnect(name) {
  if (reconnectTimers.has(name)) return;
  const timer = setTimeout(() => {
    reconnectTimers.delete(name);
    connectEndpoint(name);
  }, 1000);
  reconnectTimers.set(name, timer);
}

function connectEndpoint(name) {
  const target = endpoints[name];
  const socket = new net.Socket();
  socket.setEncoding("utf8");

  let buf = "";

  socket.on("connect", () => {
    state.connected[name] = true;
    log("INFO", `connected to ${name}`, target);
  });

  socket.on("data", (chunk) => {
    buf += chunk;
    while (true) {
      const nl = buf.indexOf("\n");
      if (nl < 0) break;
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      try {
        const obj = JSON.parse(line);
        ingestInbound(name, obj);
      } catch (err) {
        log("WARN", "failed to parse inbound line", { endpoint: name, line, err: err.message });
      }
    }
  });

  socket.on("error", (err) => {
    state.connected[name] = false;
    log("WARN", `socket error from ${name}`, { error: err.message });
  });

  socket.on("close", () => {
    state.connected[name] = false;
    log("WARN", `disconnected from ${name}`);
    scheduleReconnect(name);
  });

  clients.set(name, socket);
  socket.connect(target.port, target.host);
}

function sendJsonLine(name, payload) {
  const sock = clients.get(name);
  if (!sock || !state.connected[name]) return false;
  try {
    sock.write(`${JSON.stringify(payload)}\n`);
    return true;
  } catch {
    return false;
  }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 1024 * 1024) {
        reject(new Error("request too large"));
      }
    });
    req.on("end", () => resolve(raw));
    req.on("error", reject);
  });
}

function replyJson(res, statusCode, data) {
  res.writeHead(statusCode, { "content-type": "application/json" });
  res.end(JSON.stringify(data));
}

const server = http.createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/state") {
    return replyJson(res, 200, {
      connected: state.connected,
      statuses: state.statuses,
      signals: state.signals
    });
  }

  if (req.method === "POST" && req.url === "/publish") {
    try {
      const raw = await readBody(req);
      const msg = JSON.parse(raw || "{}");
      const key = msg.key;
      if (!key) return replyJson(res, 400, { ok: false, error: "key is required" });

      const endpoint = routeForKey(key);
      if (!endpoint) return replyJson(res, 400, { ok: false, error: `no route for key ${key}` });

      const payload = {
        key,
        value: msg.value,
        source: msg.source || "node-red",
        ts: msg.ts || ts()
      };

      state.signals[key] = payload.value;
      const ok = sendJsonLine(endpoint, payload);

      if (!ok) {
        return replyJson(res, 503, { ok: false, endpoint, error: "endpoint not connected" });
      }

      return replyJson(res, 200, { ok: true, endpoint, payload });
    } catch (err) {
      return replyJson(res, 400, { ok: false, error: err.message });
    }
  }

  return replyJson(res, 404, { ok: false, error: "not found" });
});

for (const name of Object.keys(endpoints)) {
  connectEndpoint(name);
}

server.listen(BRIDGE_PORT, "127.0.0.1", () => {
  log("INFO", "bridge HTTP API listening", {
    listenPort: BRIDGE_PORT,
    vm1: `${endpoints["vm1-bms"].host}:${endpoints["vm1-bms"].port}`,
    vm2Hvac: `${endpoints["vm2-hvac"].host}:${endpoints["vm2-hvac"].port}`,
    vm2Seat: `${endpoints["vm2-seat"].host}:${endpoints["vm2-seat"].port}`
  });
});

function shutdown() {
  for (const t of reconnectTimers.values()) clearTimeout(t);
  reconnectTimers.clear();
  for (const sock of clients.values()) {
    try { sock.destroy(); } catch {}
  }
  clients.clear();
  try { server.close(); } catch {}
  process.exit(0);
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
