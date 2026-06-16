"""End-to-end test for the tag channel: PublishTag / DeleteTag / WatchTags.

Spins up a real hub in-process and drives the Tags service the way two ferroDAC
instances would (DESIGN §7.3): one client watches, another publishes/edits/
deletes — no agent Session involved (tags are role-independent). Asserts live
fan-out, last-write-wins, tombstone propagation, and that a LATE watcher
converges from the snapshot. Run inside the hub image:

    docker compose run --rm hub python tests/tags_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("GRPC_VERBOSITY", "NONE")

import grpc
from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from hub.main import build_server

TID = "tag-uuid-0001"


async def watch(addr, events, ready, want, done):
    """Collect TagEvents until `want` of them have arrived."""
    async with grpc.aio.insecure_channel(addr) as ch:
        t = rpc.TagsStub(ch)
        call = t.WatchTags(pb.WatchTagsRequest())
        ready.set()
        async for ev in call:
            events.append(ev)
            if len(events) >= want:
                done.set()
                return


async def main() -> int:
    server, _hub = build_server()
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    events: list = []
    ready, done = asyncio.Event(), asyncio.Event()
    # watcher A wants 3 live events: ADDED, UPDATED, REMOVED
    watcher = asyncio.create_task(watch(addr, events, ready, 3, done))
    await asyncio.wait_for(ready.wait(), 5)
    await asyncio.sleep(0.1)                     # let the stream register

    async with grpc.aio.insecure_channel(addr) as ch:
        t = rpc.TagsStub(ch)

        # --- publish (any client; no agent session) -------------------------
        ack = await t.PublishTag(pb.PublishTagRequest(tag=pb.Tag(
            id=TID, t=100.0, kind="tag", label="Close GV",
            origin_kind=pb.TAG_ORIGIN_USER, severity=pb.TAG_INFO, version=1)))
        assert ack.ok, ack
        print("✓ PublishTag accepted (role-independent — no agent session)")

        # --- last-write-wins: a STALE (lower version) write is rejected ------
        stale = await t.PublishTag(pb.PublishTagRequest(tag=pb.Tag(
            id=TID, t=100.0, label="stale", version=1)))
        assert stale.detail == "stale/duplicate", stale
        print("✓ LWW: stale/duplicate version rejected")

        # --- edit (higher version) → UPDATED --------------------------------
        await t.PublishTag(pb.PublishTagRequest(tag=pb.Tag(
            id=TID, t=100.0, label="Close gate valve", version=2)))

        # --- delete → tombstone REMOVED -------------------------------------
        await t.DeleteTag(pb.DeleteTagRequest(id=TID, version=3,
                                              origin_id="user-b"))

    await asyncio.wait_for(done.wait(), 5)
    watcher.cancel()

    kinds = [pb.TagEvent.Type.Name(e.type) for e in events]
    assert kinds == ["ADDED", "UPDATED", "REMOVED"], kinds
    assert events[0].tag.label == "Close GV"
    assert events[1].tag.label == "Close gate valve" and events[1].tag.version == 2
    assert events[2].tag.deleted and events[2].tag.version >= 3
    print(f"✓ live watcher saw {kinds} (edit + tombstone propagated)")

    # --- a LATE watcher converges from the snapshot ------------------------
    late: list = []
    lready, ldone = asyncio.Event(), asyncio.Event()
    late_task = asyncio.create_task(watch(addr, late, lready, 1, ldone))
    await asyncio.wait_for(ldone.wait(), 5)
    late_task.cancel()
    assert late[0].tag.id == TID and late[0].tag.deleted, late
    print("✓ late watcher converges: receives the tombstone in its snapshot")

    await server.stop(grace=0.5)
    print("\nTAGS E2E PASS — reliable fan-out, LWW, tombstones, late-join convergence")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
