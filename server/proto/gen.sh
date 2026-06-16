#!/usr/bin/env sh
# Regenerate the Python gRPC stubs from the data-plane contract.
#
# Runs protoc in a throwaway container so the host needs no protoc/grpcio-tools
# (matches the dockerised hub, and the Kali dev box keeps Python locked down).
# Output: server/gen/ferrodac/v1/{data_plane_pb2,data_plane_pb2_grpc}.py
# — committed to the repo so the app and hub import them without a toolchain.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"            # server/

docker run --rm -e HU="$(id -u)" -e HG="$(id -g)" -v "$ROOT":/work -w /work \
  python:3.12-slim sh -c '
    pip install -q grpcio-tools
    mkdir -p gen
    python -m grpc_tools.protoc -I proto --python_out=gen --grpc_python_out=gen \
      proto/ferrodac/v1/data_plane.proto
    touch gen/ferrodac/__init__.py gen/ferrodac/v1/__init__.py
    chown -R "$HU:$HG" gen
  '
echo "stubs regenerated → server/gen/ferrodac/v1/"
