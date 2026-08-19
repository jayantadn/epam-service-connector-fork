#!/usr/bin/env python3
"""Expose the Node-RED HTTP API on top of the shared Zenoh bus."""

from __future__ import annotations

import json
import os
import socket
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import zenoh


BRIDGE_HOST = os.getenv("BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "1881"))
ZENOH_ROUTER = os.getenv("ZENOH_ROUTER", "tcp/127.0.0.1:7447")
SOURCE = socket.gethostname()

state: dict[str, Any] = {
    "connected": {"zenoh": False},
    "signals": {},
    "statuses": {},
}
publishers: dict[str, zenoh.Publisher] = {}
state_lock = threading.Lock()
session: zenoh.Session | None = None
stop_event = threading.Event()


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def update_status(key: str, message: dict[str, Any]) -> None:
    with state_lock:
        if key == "dash/status/seat" and message.get("key"):
            state["statuses"][f"{key}#{message['key']}"] = message
        state["statuses"][key] = message


def on_status(sample: zenoh.Sample) -> None:
    try:
        message = json.loads(sample.payload.to_string())
    except (TypeError, json.JSONDecodeError):
        return
    if isinstance(message, dict):
        update_status(str(sample.key_expr), message)


def publish(key: str, value: Any) -> dict[str, Any]:
    if session is None:
        raise RuntimeError(f"Zenoh router unavailable at {ZENOH_ROUTER}")
    publisher = publishers.get(key)
    if publisher is None:
        publisher = session.declare_publisher(key)
        publishers[key] = publisher
    payload = {
        "key": key,
        "value": value,
        "source": SOURCE,
        "ts": timestamp(),
    }
    publisher.put(json.dumps(payload).encode("utf-8"))
    with state_lock:
        state["signals"][key] = value
    return payload


class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        print(f"[zenoh-bridge] {format % args}", flush=True)

    def reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/state":
            self.reply(404, {"ok": False, "error": "not found"})
            return
        with state_lock:
            self.reply(200, json.loads(json.dumps(state)))

    def do_POST(self) -> None:
        if self.path != "/publish":
            self.reply(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            message = json.loads(self.rfile.read(length) or b"{}")
            key = message.get("key")
            if not isinstance(key, str) or not key:
                raise ValueError("key is required")
            payload = publish(key, message.get("value"))
            self.reply(200, {"ok": True, "endpoint": "zenoh", "payload": payload})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.reply(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self.reply(503, {"ok": False, "endpoint": "zenoh", "error": str(exc)})


def connect_zenoh() -> None:
    global session
    config = zenoh.Config()
    config.insert_json5("mode", '"client"')
    config.insert_json5("connect/endpoints", json.dumps([ZENOH_ROUTER]))
    config.insert_json5("listen/endpoints", '["tcp/0.0.0.0:0"]')
    config.insert_json5("scouting/multicast/enabled", "false")
    config.insert_json5("scouting/gossip/enabled", "false")

    while not stop_event.is_set():
        try:
            with zenoh.open(config) as connected_session:
                session = connected_session
                subscribers = [
                    connected_session.declare_subscriber("dash/status/hvac", on_status),
                    connected_session.declare_subscriber("dash/status/seat", on_status),
                ]
                with state_lock:
                    state["connected"]["zenoh"] = True
                print(f"[zenoh-bridge] connected to {ZENOH_ROUTER}", flush=True)
                stop_event.wait()
                for subscriber in subscribers:
                    subscriber.undeclare()
        except Exception as exc:
            print(f"[zenoh-bridge] waiting for {ZENOH_ROUTER}: {exc}", flush=True)
        finally:
            session = None
            publishers.clear()
            with state_lock:
                state["connected"]["zenoh"] = False
        if not stop_event.is_set():
            stop_event.wait(2)


def main() -> None:
    server = ThreadingHTTPServer((BRIDGE_HOST, BRIDGE_PORT), BridgeHandler)
    connector = threading.Thread(target=connect_zenoh, daemon=True)
    connector.start()
    print(
        f"[zenoh-bridge] HTTP on {BRIDGE_HOST}:{BRIDGE_PORT}; "
        f"waiting for Zenoh router {ZENOH_ROUTER}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()