"""Real-gRPC end-to-end against an in-process hub.

Wraps the server's e2e scripts (each `async def main() -> int`). They stand up a
real grpc.aio server in-process — no Docker — covering the contract both ways:
store-and-forward sync + the hub-as-resolver-tier read path (scalars + traces).
"""

import asyncio
import importlib
import sys

import pytest

grpc = pytest.importorskip("grpc")

# server/tests scripts on the path (see conftest); each has async def main()→int.
E2E = [
    "sync_e2e",     # sync mirror + live tail + read tier (ListSources/Coverage/Query/ReadRaw/Trace)
    "e2e",          # agent → hub → viewer: transparent remote device, subscribe, retire
    "net_e2e",      # app net layer round-trip (convert + agent/viewer), incl. Trace
    "docs_collab_e2e",  # live collab rooms: seed/fan-out/compaction/snapshot/persist
    "docs_net_e2e",     # app collab relay (HubDocSync) round-trip, base64 at the seam
]


@pytest.mark.integration
@pytest.mark.parametrize("module", E2E)
def test_grpc_e2e(module):
    if sys.platform == "win32" and module.startswith("docs_"):
        pytest.skip("the collab relay e2e leaks non-daemon asyncio executor threads "
                    "that hang process exit on the Windows runner; covered on Linux")
    pytest.importorskip("ferrodac_contract.v1.data_plane_pb2")
    mod = importlib.import_module(module)
    assert asyncio.run(mod.main()) == 0, f"{module}.main() reported failure"
