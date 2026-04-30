"""gRPC server for the VM1 -> VM2 demo.

Runs on VM2 (192.168.100.11). Listens on 0.0.0.0:50051 and serves
two RPCs defined in proto/demo.proto:

  - SayHello(HelloRequest) -> HelloReply       (unary)
  - StreamSignals(SignalRequest) -> stream     (server-streaming)

Generate stubs first (one-time):
    ./generate.sh
"""

import argparse
import math
import socket
import time
from concurrent import futures
from datetime import datetime

import grpc

import demo_pb2
import demo_pb2_grpc


class DemoServicer(demo_pb2_grpc.DemoServicer):
    def SayHello(self, request, context):
        peer = context.peer()
        print(f"[server] SayHello from {peer}: name={request.name!r}", flush=True)
        return demo_pb2.HelloReply(
            message=f"Hello, {request.name}! Greetings from VM2.",
            server_hostname=socket.gethostname(),
        )

    def StreamSignals(self, request, context):
        peer = context.peer()
        signal = request.signal_name or "Vehicle.Speed"
        count = request.count if request.count > 0 else 10
        interval = request.interval_seconds if request.interval_seconds > 0 else 1.0

        print(
            f"[server] StreamSignals from {peer}: "
            f"signal={signal!r} count={count} interval={interval}s",
            flush=True,
        )

        for i in range(count):
            if not context.is_active():
                print("[server] Client cancelled stream", flush=True)
                return

            value = 50.0 + 10.0 * math.sin(i / 3.0)
            update = demo_pb2.SignalUpdate(
                signal_name=signal,
                value=value,
                sequence=i,
                timestamp=datetime.now().isoformat(timespec="milliseconds"),
            )
            print(f"[server]  -> seq={i} value={value:.3f}", flush=True)
            yield update
            time.sleep(interval)

        print(f"[server] Stream complete ({count} samples sent)", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="gRPC server (runs on VM2)."
    )
    parser.add_argument(
        "--bind",
        default="0.0.0.0:50051",
        help="Address:port to listen on (default: 0.0.0.0:50051)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Thread-pool size for handling RPCs (default: 8)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=args.workers))
    demo_pb2_grpc.add_DemoServicer_to_server(DemoServicer(), server)
    server.add_insecure_port(args.bind)
    server.start()
    print(f"[server] gRPC server listening on {args.bind}", flush=True)
    print("[server] Press Ctrl+C to stop", flush=True)

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        print("[server] Shutting down...")
        server.stop(grace=2).wait()


if __name__ == "__main__":
    main()
