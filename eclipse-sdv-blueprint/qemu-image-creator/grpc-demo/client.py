"""gRPC client for the VM1 -> VM2 demo.

Runs on VM1 (192.168.100.10). Dials the server on VM2 and exercises
both RPCs defined in proto/demo.proto:

  - SayHello       (unary)
  - StreamSignals  (server-streaming)

Generate stubs first (one-time):
    ./generate.sh
"""

import argparse
import socket
import sys

import grpc

import demo_pb2
import demo_pb2_grpc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="gRPC client (runs on VM1). Calls the server on VM2."
    )
    parser.add_argument(
        "--server",
        default="192.168.100.11:50051",
        help="Server address:port (default: 192.168.100.11:50051)",
    )
    parser.add_argument(
        "--name",
        default=socket.gethostname(),
        help="Name to send in the SayHello RPC (default: hostname)",
    )
    parser.add_argument(
        "--signal",
        default="Vehicle.Speed",
        help="Signal name to request from StreamSignals (default: Vehicle.Speed)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=10,
        help="Number of streamed samples to request (default: 10)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Server-side interval between samples in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=5.0,
        help="Channel-ready timeout in seconds (default: 5.0)",
    )
    parser.add_argument(
        "--skip-stream",
        action="store_true",
        help="Only run the unary SayHello RPC, skip StreamSignals",
    )
    return parser.parse_args()


def call_unary(stub: demo_pb2_grpc.DemoStub, name: str) -> None:
    print(f"[client] -> SayHello(name={name!r})")
    reply = stub.SayHello(demo_pb2.HelloRequest(name=name))
    print(f"[client] <- HelloReply(message={reply.message!r}, "
          f"server_hostname={reply.server_hostname!r})")


def call_stream(
    stub: demo_pb2_grpc.DemoStub,
    signal: str,
    count: int,
    interval: float,
) -> None:
    request = demo_pb2.SignalRequest(
        signal_name=signal,
        count=count,
        interval_seconds=interval,
    )
    print(f"[client] -> StreamSignals(signal={signal!r}, count={count}, interval={interval}s)")
    for update in stub.StreamSignals(request):
        print(f"[client] <- seq={update.sequence:>3} "
              f"signal={update.signal_name} "
              f"value={update.value:.3f} "
              f"ts={update.timestamp}")


def main() -> int:
    args = parse_args()

    print(f"[client] Connecting to {args.server}...")
    channel = grpc.insecure_channel(args.server)

    try:
        grpc.channel_ready_future(channel).result(timeout=args.connect_timeout)
    except grpc.FutureTimeoutError:
        print(f"[client] ERROR: could not reach {args.server} "
              f"within {args.connect_timeout}s", file=sys.stderr)
        return 2

    stub = demo_pb2_grpc.DemoStub(channel)

    try:
        call_unary(stub, args.name)
        if not args.skip_stream:
            call_stream(stub, args.signal, args.count, args.interval)
    except grpc.RpcError as exc:
        status = exc.code() if hasattr(exc, "code") else "?"
        detail = exc.details() if hasattr(exc, "details") else str(exc)
        print(f"[client] gRPC call failed: {status} - {detail}", file=sys.stderr)
        return 3
    finally:
        channel.close()

    print("[client] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
