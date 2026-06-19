"""Hub entrypoint: an async gRPC server exposing Ingest + Viewer.

Milestone 1: insecure (plaintext h2c) on :50051, no auth, no storage. TLS lands
at the cluster ingress; the auth seam is already in the contract.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

import grpc
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from .core import HUB_VERSION, Hub
from .service import IngestServicer, StoreServicer, TagsServicer, ViewerServicer

log = logging.getLogger("hub")


def _open_store(store_dir):
    """The hub's durable Zarr store (same engine as the app). None disables the
    Store service's persistence (it then reports empty / accepts nothing)."""
    if not store_dir:
        return None
    try:
        from ferrodac.store import ZarrStore
        return ZarrStore(store_dir)
    except Exception as exc:                          # noqa: BLE001
        log.warning("hub store disabled (%s): %s", store_dir, exc)
        return None


def build_server(hub: "Hub | None" = None, store=None
                 ) -> "tuple[grpc.aio.Server, Hub]":
    """Wire a gRPC server around a Hub (shared by main and the e2e test). `store`
    is the hub's durable ZarrStore (sync target + read tier); may be None."""
    hub = hub or Hub()
    server = grpc.aio.server()
    rpc.add_IngestServicer_to_server(IngestServicer(hub), server)
    rpc.add_ViewerServicer_to_server(ViewerServicer(hub), server)
    rpc.add_TagsServicer_to_server(TagsServicer(hub), server)
    rpc.add_StoreServicer_to_server(StoreServicer(store), server)
    return server, hub


def _tags_path():
    """Where the hub persists tags (JSON). Beside the Zarr store by default."""
    p = os.environ.get("HUB_TAGS_PATH")
    if p:
        return p
    store_dir = os.environ.get("HUB_STORE_DIR")
    return os.path.join(os.path.dirname(store_dir.rstrip("/")), "tags.json") \
        if store_dir else None


async def serve() -> None:
    hub = Hub(tags_path=_tags_path())
    server, _ = build_server(hub=hub, store=_open_store(os.environ.get("HUB_STORE_DIR")))
    addr = os.environ.get("HUB_GRPC_ADDR", "0.0.0.0:50051")
    server.add_insecure_port(addr)
    await server.start()
    log.info("ferroDAC hub %s listening on %s (gRPC, insecure)", HUB_VERSION, addr)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:        # e.g. Windows
            pass
    await stop.wait()
    log.info("shutting down…")
    await server.stop(grace=2.0)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve())


if __name__ == "__main__":
    main()
