"""End-to-end for the app's collab relay (ferrodac.net.docs.HubDocSync).

Drives the Qt-free client relay against a real in-process hub: two HubDocSync
clients join a doc room and exchange opaque (base64) updates + awareness, proving
seed/replay/fan-out/presence/snapshot and reconnect — with NO Yjs and NO Qt. The
CRDT payloads are synthetic; the relay never parses them.

    docker compose run --rm hub python tests/docs_net_e2e.py
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile

os.environ.setdefault("GRPC_VERBOSITY", "NONE")

from hub.core import Hub
from hub.main import build_server

from ferrodac.net.docs import HubDocSync

DOC = "proj1::README.md"
SEED = "# seed\n"


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


class Sink:
    """Collects a HubDocSync's callbacks (which fire on its worker thread). Plain
    list.append is GIL-atomic; the test polls with small sleeps to let cross-thread
    results land."""

    def __init__(self):
        self.seeds: list = []
        self.updates: list = []
        self.awareness: list = []
        self.presence: list = []

    def kwargs(self) -> dict:
        return dict(
            on_seed=lambda d, s, t: self.seeds.append((d, s, t)),
            on_update=lambda d, u: self.updates.append((d, u)),
            on_awareness=lambda d, a: self.awareness.append((d, a)),
            on_presence=lambda d, a: self.presence.append((d, list(a))),
        )


async def until(pred, timeout=5.0) -> bool:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ferrodac-docs-net-")
    docs_dir = os.path.join(tmp, "proj1", "docs")
    os.makedirs(docs_dir, exist_ok=True)
    readme = os.path.join(docs_dir, "README.md")
    with open(readme, "w", encoding="utf-8") as fh:
        fh.write(SEED)

    server, _ = build_server(hub=Hub(projects_dir=tmp))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    # -- client A: cold join → told to seed from the .md text --------------------
    sa = Sink()
    a = HubDocSync(addr, agent_id="alice", **sa.kwargs())
    a.start()
    a.join(DOC, actor="alice")
    assert await until(lambda: bool(sa.seeds)), "A received no seed"
    assert sa.seeds[0][1] is True and sa.seeds[0][2] == SEED, sa.seeds
    a.send_update(DOC, b64(b"U1"))                 # alice's seed update → the baseline
    print("✓ relay: cold joiner is told to seed from the file text")

    # -- client B: late join → empty, replays the baseline -----------------------
    sb = Sink()
    b = HubDocSync(addr, agent_id="bob", **sb.kwargs())
    b.start()
    b.join(DOC, actor="bob")
    assert await until(lambda: sb.seeds and sb.seeds[0][1] is False), sb.seeds
    assert await until(lambda: (DOC, b64(b"U1")) in sb.updates), sb.updates
    print("✓ relay: late joiner replays the baseline (base64 round-trip)")

    # -- live fan-out, no self-echo ----------------------------------------------
    a.send_update(DOC, b64(b"U2"))
    assert await until(lambda: (DOC, b64(b"U2")) in sb.updates), sb.updates
    assert (DOC, b64(b"U2")) not in sa.updates, "A echoed its own update"
    print("✓ relay: live fan-out reaches the peer, never echoes the sender")

    # -- awareness + presence ----------------------------------------------------
    a.send_awareness(DOC, b64(b"AW"))
    assert await until(lambda: (DOC, b64(b"AW")) in sb.awareness), sb.awareness
    assert await until(
        lambda: sb.presence and sorted(sb.presence[-1][1]) == ["alice", "bob"]), \
        sb.presence
    print("✓ relay: awareness fans out; presence lists both actors")

    # -- leader snapshot materialises the .md ------------------------------------
    a.send_snapshot(DOC, "# done\n")
    assert await until(
        lambda: open(readme, encoding="utf-8").read() == "# done\n"), "no .md write"
    print("✓ relay: leader snapshot materialises the .md")

    # -- reconnect / late join replays full state (baseline + log) ---------------
    b.stop()
    sc = Sink()
    c = HubDocSync(addr, agent_id="carol", **sc.kwargs())
    c.start()
    c.join(DOC, actor="carol")
    assert await until(
        lambda: {u[1] for u in sc.updates} >= {b64(b"U1"), b64(b"U2")}), sc.updates
    print("✓ relay: a fresh client replays the full baseline+log")

    a.stop()
    c.stop()
    await server.stop(grace=0.2)
    print("\nDOCS NET E2E PASS — relay seed/replay/fan-out/awareness/presence/"
          "snapshot/reconnect")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
