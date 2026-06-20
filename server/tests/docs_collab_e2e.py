"""End-to-end test for the collaborative-editing channel: Docs.Session.

Spins up a real hub in-process and drives the Docs service the way several editors
would (DESIGN §10.x). The hub is a DUMB relay of opaque Yjs bytes, so this test
uses SYNTHETIC byte payloads — no Yjs, no Qt, no browser. Asserts the seeding rule
(first joiner of a cold room seeds; later joiners start empty and replay), live
fan-out (no self-echo), leader-only compaction (a fresh joiner gets only the new
baseline), .md materialisation from a Snapshot, persistence of the baseline across
a hub restart, and seeder re-designation when the seeder drops before seeding.

    docker compose run --rm hub python tests/docs_collab_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("GRPC_VERBOSITY", "NONE")

import grpc
from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from hub.core import Hub
from hub.main import build_server

DOC = "proj1::README.md"
SEED_TEXT = "# seed\n"


class Client:
    """A Docs.Session: a write half (call.write) + a background collector draining
    everything the hub sends into typed buckets."""

    def __init__(self, ch):
        self.ch = ch
        self.call = rpc.DocsStub(ch).Session()
        self.seeds: list = []
        self.updates: list = []
        self.presence: list = []
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            async for msg in self.call:
                k = msg.WhichOneof("msg")
                if k == "seed":
                    self.seeds.append(msg.seed)
                elif k == "update":
                    self.updates.append(msg.update)
                elif k == "presence":
                    self.presence.append(msg.presence)
        except (asyncio.CancelledError, grpc.aio.AioRpcError):
            pass

    async def join(self, doc_id=DOC, actor="a"):
        await self.call.write(pb.DocClientMsg(
            join=pb.DocJoin(doc_id=doc_id, actor=actor)))

    async def update(self, payload: bytes, doc_id=DOC, compaction=False):
        await self.call.write(pb.DocClientMsg(
            update=pb.DocUpdate(doc_id=doc_id, update=payload, compaction=compaction)))

    async def snapshot(self, text: str, doc_id=DOC):
        await self.call.write(pb.DocClientMsg(
            snapshot=pb.DocSnapshot(doc_id=doc_id, text=text)))

    async def close(self):
        self.task.cancel()
        await self.ch.close()


async def settle(t=0.15):
    await asyncio.sleep(t)


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ferrodac-docs-")
    docs_dir = os.path.join(tmp, "proj1", "docs")
    os.makedirs(docs_dir, exist_ok=True)
    with open(os.path.join(docs_dir, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(SEED_TEXT)

    hub = Hub(projects_dir=tmp)
    server, _ = build_server(hub=hub)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    # -- 1. cold seed: the first joiner is told to seed from the file text --------
    a = Client(grpc.aio.insecure_channel(addr))
    await a.join(actor="alice")
    await settle()
    assert a.seeds and a.seeds[0].should_seed, a.seeds
    assert a.seeds[0].text == SEED_TEXT, a.seeds[0].text
    await a.update(b"U1")                          # alice's seed update → the baseline
    await settle()
    print("✓ cold room: first joiner seeds from the .md text")

    # -- 2. late join: starts EMPTY, replays the baseline ------------------------
    b = Client(grpc.aio.insecure_channel(addr))
    await b.join(actor="bob")
    await settle()
    assert b.seeds and not b.seeds[0].should_seed, b.seeds
    assert [u.update for u in b.updates] == [b"U1"], b.updates
    print("✓ late joiner starts empty and replays the baseline (no duplicate seed)")

    # -- 3. live fan-out: bob sees alice's edit; alice never sees her own ---------
    await a.update(b"U2")
    await settle()
    assert b"U2" in [u.update for u in b.updates], b.updates
    assert b"U2" not in [u.update for u in a.updates], "sender echoed its own update"
    print("✓ live fan-out reaches peers, never echoes the sender")

    # -- 4. compaction (leader=alice): a NEW joiner gets only the new baseline ----
    await a.update(b"BASE", compaction=True)
    await settle()
    c = Client(grpc.aio.insecure_channel(addr))
    await c.join(actor="carol")
    await settle()
    got = [u.update for u in c.updates]
    assert got == [b"BASE"], f"expected only the compacted baseline, got {got}"
    print("✓ leader compaction: a fresh joiner replays only the new baseline")

    # -- 5. snapshot materialises the human-readable .md (leader only) ------------
    await a.snapshot("# collaborated\n")
    await settle()
    with open(os.path.join(docs_dir, "README.md"), encoding="utf-8") as fh:
        assert fh.read() == "# collaborated\n", "snapshot did not materialise the .md"
    print("✓ leader Snapshot atomically materialises the .md on the server")

    # -- 6. persistence across a hub restart: baseline reloads from .ycrdt --------
    await a.close()
    await b.close()
    await c.close()
    await server.stop(grace=0.2)

    hub2 = Hub(projects_dir=tmp)                   # fresh hub, same folder
    server2, _ = build_server(hub=hub2)
    port2 = server2.add_insecure_port("127.0.0.1:0")
    await server2.start()
    addr2 = f"127.0.0.1:{port2}"
    d = Client(grpc.aio.insecure_channel(addr2))
    await d.join(actor="dave")
    await settle()
    assert d.seeds and not d.seeds[0].should_seed, d.seeds
    assert [u.update for u in d.updates] == [b"BASE"], d.updates
    print("✓ persistence: a cold restart replays the .ycrdt baseline")
    await d.close()

    # -- 7. seeder drops BEFORE seeding → the next member is re-designated --------
    NOTES = "proj1::NOTES.md"
    e = Client(grpc.aio.insecure_channel(addr2))
    await e.join(doc_id=NOTES, actor="erin")       # cold → erin is the seeder
    await settle()
    assert e.seeds and e.seeds[0].should_seed, e.seeds
    f = Client(grpc.aio.insecure_channel(addr2))
    await f.join(doc_id=NOTES, actor="finn")       # waiting (should_seed=False)
    await settle()
    assert f.seeds and not f.seeds[0].should_seed, f.seeds
    await e.close()                                # erin leaves WITHOUT seeding
    await settle()
    assert any(s.should_seed for s in f.seeds), \
        "finn was not re-designated as seeder after the seeder dropped"
    print("✓ seeder drop-before-seed: the next member is re-designated to seed")

    await f.close()
    await server2.stop(grace=0.2)
    print("\nDOCS COLLAB E2E PASS — seeding, fan-out, compaction, snapshot, "
          "persistence, seeder recovery")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
