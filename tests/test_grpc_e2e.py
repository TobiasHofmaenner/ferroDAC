"""Real-gRPC end-to-end against an in-process hub.

Wraps the server's e2e scripts (each `async def main() -> int`). They stand up a
real grpc.aio server in-process — no Docker — covering the contract both ways:
store-and-forward sync + the hub-as-resolver-tier read path (scalars + traces).
"""

import asyncio
import importlib

import pytest

grpc = pytest.importorskip("grpc")

# server/tests scripts on the path (see conftest); each has async def main()→int.
E2E = [
    "sync_e2e",     # sync mirror + live tail + read tier (ListSources/Coverage/Query/ReadRaw/Trace)
    "e2e",          # agent → hub → viewer: transparent remote device, subscribe, retire
    "net_e2e",      # app net layer round-trip (convert + agent/viewer), incl. Trace
]


@pytest.mark.integration
@pytest.mark.parametrize("module", E2E)
def test_grpc_e2e(module):
    pytest.importorskip("ferrodac_contract.v1.data_plane_pb2")
    mod = importlib.import_module(module)
    assert asyncio.run(mod.main()) == 0, f"{module}.main() reported failure"
