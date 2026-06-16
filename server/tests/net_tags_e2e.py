"""End-to-end test for the app's tag-sync net client against a real hub.

Two HubTagSync clients (as two ferroDAC instances) talk to an in-process hub:
A publishes/edits/deletes, B receives the merged stream, and a late C converges
from the snapshot. Exercises the Qt-free net layer + convert round-trip without
any GUI. Run with: PYTHONPATH=.:server:server/gen python3 server/tests/net_tags_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ.setdefault("GRPC_VERBOSITY", "NONE")

from hub.main import build_server                         # noqa: E402
from ferrodac.core.tag import Marker                      # noqa: E402
from ferrodac.net import convert                          # noqa: E402
from ferrodac.net.tags import HubTagSync                  # noqa: E402


def _check_convert_roundtrip() -> None:
    m = Marker(id="rt", t=12.5, t_end=20.0, kind="alarm", label="HV trip",
               comment="c", origin_kind="processor", origin_id="proc-1",
               scope="device:xyz", severity="critical",
               payload={"channel": "SEM", "value": "1500"}, version=7)
    back = convert.tag_from_proto(convert.tag_to_proto(m))
    assert back == m, (back, m)
    print("✓ convert round-trip preserves every field (incl. payload + t_end)")


async def main() -> int:
    _check_convert_roundtrip()

    server, _hub = build_server()
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    a_got, b_got = [], []
    a = HubTagSync(addr, agent_id="A", on_tag=a_got.append)
    b = HubTagSync(addr, agent_id="B", on_tag=b_got.append)
    a.start()
    b.start()
    await asyncio.sleep(0.6)                                # connect + register watch

    def b_has(pred):
        return any(pred(x) for x in b_got)

    a.publish(Marker(id="t1", t=100.0, label="Close GV", origin_id="A", version=2))
    await asyncio.sleep(0.4)
    assert b_has(lambda x: x.id == "t1" and x.label == "Close GV"), b_got
    print("✓ A's tag reaches B over the hub")

    a.publish(Marker(id="t1", t=100.0, label="Close gate valve",
                     origin_id="A", version=3))
    await asyncio.sleep(0.4)
    assert b_has(lambda x: x.label == "Close gate valve" and x.version == 3)
    print("✓ edit (higher version) propagates to B")

    a.publish(Marker(id="t1", t=100.0, label="Close gate valve",
                     origin_id="A", version=4, deleted=True))
    await asyncio.sleep(0.4)
    assert b_has(lambda x: x.id == "t1" and x.deleted)
    print("✓ delete (tombstone) propagates to B")

    # a LATE instance converges purely from the WatchTags snapshot
    c_got = []
    c = HubTagSync(addr, agent_id="C", on_tag=c_got.append)
    c.start()
    await asyncio.sleep(0.6)
    assert any(x.id == "t1" and x.deleted for x in c_got), c_got
    print("✓ late instance C converges from the snapshot")

    a.stop()
    b.stop()
    c.stop()
    await server.stop(grace=0.5)
    print("\nNET TAGS E2E PASS — cross-instance tag sync, edits, tombstones, late-join")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
