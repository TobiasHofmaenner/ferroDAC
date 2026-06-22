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
from .service import (
    DocsServicer,
    IngestServicer,
    ProjectsServicer,
    StoreServicer,
    TagsServicer,
    ViewerServicer,
)

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
    # Match the clients' lifted message cap (ferrodac.net.GRPC_CHANNEL_OPTIONS): the
    # default 4 MiB is too small for a backlogged store-sync chunk or a full-res
    # ReadRawTrace response. (Clients also split sync pushes to stay well under it.)
    max_bytes = 64 * 1024 * 1024
    server = grpc.aio.server(options=[
        ("grpc.max_send_message_length", max_bytes),
        ("grpc.max_receive_message_length", max_bytes),
    ])
    rpc.add_IngestServicer_to_server(IngestServicer(hub), server)
    rpc.add_ViewerServicer_to_server(ViewerServicer(hub), server)
    rpc.add_TagsServicer_to_server(TagsServicer(hub), server)
    rpc.add_ProjectsServicer_to_server(ProjectsServicer(hub), server)
    rpc.add_DocsServicer_to_server(DocsServicer(hub), server)
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


def _projects_dir():
    """Where the hub stores project FOLDERS (mountable, same layout as local).
    Beside the Zarr store by default."""
    p = os.environ.get("HUB_PROJECTS_DIR")
    if p:
        return p
    store_dir = os.environ.get("HUB_STORE_DIR")
    return os.path.join(os.path.dirname(store_dir.rstrip("/")), "projects") \
        if store_dir else None


def _gitea():
    """Transparent dial (DESIGN §8.2): if a bundled Gitea is configured, the hub
    auto-provisions a repo per project. Off unless GITEA_URL + GITEA_TOKEN are set."""
    url = os.environ.get("GITEA_URL")
    token = os.environ.get("GITEA_TOKEN")
    if not url or not token:
        return None
    from .gitea import GiteaProvisioner
    g = GiteaProvisioner(url, token, org=os.environ.get("GITEA_ORG", "ferrodac"),
                         user=os.environ.get("GITEA_USER", "ferrodac"),
                         public_url=os.environ.get("GITEA_PUBLIC_URL"))
    log.info("transparent git: provisioning repos in Gitea at %s (org %s)", url, g.org)
    return g


async def serve() -> None:
    hub = Hub(tags_path=_tags_path(), projects_dir=_projects_dir(), gitea=_gitea())
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
