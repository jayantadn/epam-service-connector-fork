#!/usr/bin/env bash
# Regenerate Python gRPC stubs from proto/demo.proto.
#
# Outputs (next to server.py / client.py):
#   - demo_pb2.py        (message classes)
#   - demo_pb2_grpc.py   (stub + servicer base class)
#
# Requires `grpcio-tools` to be importable by the python3 you run this with:
#   pip install --user grpcio grpcio-tools
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

if ! python3 -c "import grpc_tools" >/dev/null 2>&1; then
  echo "[generate] ERROR: grpc_tools is not installed for $(command -v python3)"
  echo "[generate] Install it with:"
  echo "[generate]   pip install --user grpcio grpcio-tools"
  echo "[generate] Or on Ubuntu 23.10+:"
  echo "[generate]   sudo pip3 install --break-system-packages --ignore-installed grpcio grpcio-tools"
  exit 1
fi

cd "$HERE"

python3 -m grpc_tools.protoc \
  --proto_path=proto \
  --python_out=. \
  --grpc_python_out=. \
  proto/demo.proto

echo "[generate] Wrote:"
echo "  $HERE/demo_pb2.py"
echo "  $HERE/demo_pb2_grpc.py"