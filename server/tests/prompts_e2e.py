"""End-to-end for the interaction PROMPT relay (§7.3) over real gRPC.

An AGENT raises a device prompt; a VIEWER sees it via WatchPrompts and answers it with
RespondPrompt; the hub routes the answer down to the owning agent (on_prompt_response); the
agent publishes the resolution and every viewer withdraws it. Also: a late viewer converges
from the open-prompt snapshot, an agent disconnect closes its open prompts, and an agent
RECONNECT re-publishes its still-open prompts (the mirror) — answerable again, while a
prompt resolved offline stays closed.

    docker compose run --rm hub python tests/prompts_e2e.py
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
from ferrodac.net.agent import HubAgent


async def watch(addr, events, ready, want, done):
    """Collect PromptEvents until `want` have arrived."""
    async with grpc.aio.insecure_channel(addr) as ch:
        v = rpc.ViewerStub(ch)
        call = v.WatchPrompts(pb.WatchPromptsRequest())
        ready.set()
        async for ev in call:
            events.append(ev)
            if len(events) >= want:
                done.set()
                return


async def _until(cond, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


async def main() -> int:
    server, _hub = build_server()
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    answered: list = []
    agent = HubAgent(addr, agent_id="lsc-agent",
                     on_prompt_response=lambda pid, ans, by: answered.append((pid, ans, by)))
    agent.start()
    await asyncio.sleep(0.3)                          # let the Ingest session establish

    events: list = []
    ready, done = asyncio.Event(), asyncio.Event()
    watcher = asyncio.create_task(watch(addr, events, ready, 2, done))   # opened + closed
    await asyncio.wait_for(ready.wait(), 5)
    await asyncio.sleep(0.1)                          # let the stream register

    # 1) the agent raises an OPEN prompt → the viewer sees it live
    agent.publish_prompt(pb.WirePrompt(
        id="p-1", device_uuid="lsc-uuid", question="Retract the arm?",
        kind="confirm", severity="critical", options=["Yes", "No"]))
    assert await _until(lambda: len(events) >= 1), "viewer never saw the open prompt"
    assert events[0].WhichOneof("ev") == "opened"
    assert events[0].opened.id == "p-1" and events[0].opened.question == "Retract the arm?"
    assert list(events[0].opened.options) == ["Yes", "No"]
    print("✓ agent → hub → viewer: an open prompt reaches WatchPrompts")

    # 2) the viewer ANSWERS it → routed down to the owning agent (on_prompt_response)
    async with grpc.aio.insecure_channel(addr) as ch:
        ack = await rpc.ViewerStub(ch).RespondPrompt(
            pb.RespondPromptRequest(id="p-1", by="viewer-a", boolean=True))
        assert ack.ok, ack.detail
    assert await _until(lambda: answered), "the agent never received the answer"
    assert answered == [("p-1", True, "viewer-a")], answered
    print("✓ viewer → hub → agent: RespondPrompt delivered as on_prompt_response(True)")

    # 3) the agent publishes the RESOLUTION → the viewer withdraws it
    agent.close_prompt("p-1", answer_text="Yes", by="viewer-a")
    await asyncio.wait_for(done.wait(), 5)
    watcher.cancel()
    assert events[1].WhichOneof("ev") == "closed"
    assert events[1].closed.id == "p-1" and events[1].closed.answer_text == "Yes"
    print("✓ resolution fans out as a close → every surface withdraws")

    # 4) a LATE viewer converges from the open-prompt snapshot
    agent.publish_prompt(pb.WirePrompt(id="p-2", device_uuid="lsc-uuid",
                                       question="Vent?", kind="confirm"))
    await asyncio.sleep(0.15)
    late: list = []
    lready, ldone = asyncio.Event(), asyncio.Event()
    late_task = asyncio.create_task(watch(addr, late, lready, 1, ldone))
    await asyncio.wait_for(ldone.wait(), 5)
    late_task.cancel()
    assert late[0].WhichOneof("ev") == "opened" and late[0].opened.id == "p-2", late
    print("✓ late viewer converges: open prompt is in its snapshot")

    # 5) an unanswered respond for a gone prompt is a graceful ok=false
    async with grpc.aio.insecure_channel(addr) as ch:
        bad = await rpc.ViewerStub(ch).RespondPrompt(
            pb.RespondPromptRequest(id="nope", by="v", boolean=True))
        assert not bad.ok and bad.detail, bad
    print("✓ answering a gone/unknown prompt → ok=false")

    # 6) agent disconnect closes its still-open prompts (viewers withdraw)
    closed: list = []
    cready, cdone = asyncio.Event(), asyncio.Event()
    ctask = asyncio.create_task(watch(addr, closed, cready, 2, cdone))   # snapshot p-2 + its close
    await asyncio.wait_for(cready.wait(), 5)
    await asyncio.sleep(0.1)
    agent.stop()
    await asyncio.wait_for(cdone.wait(), 5)
    ctask.cancel()
    assert closed[-1].WhichOneof("ev") == "closed" and closed[-1].closed.id == "p-2"
    print("✓ agent disconnect closes its open prompts")

    # 7) the agent comes BACK (blip / hub restart) → its still-open prompts re-publish
    #    from the open-prompt mirror, so a late viewer sees p-2 again; a prompt raised
    #    AND resolved while offline (p-3) must not resurrect.
    agent.publish_prompt(pb.WirePrompt(id="p-3", device_uuid="lsc-uuid", question="Purge?"))
    agent.close_prompt("p-3", by="device")            # resolved while offline
    agent.start()                                     # the reconnect
    back: list = []
    bready, bdone = asyncio.Event(), asyncio.Event()
    btask = asyncio.create_task(watch(addr, back, bready, 1, bdone))
    await asyncio.wait_for(bdone.wait(), 10)          # snapshot OR the live re-open
    btask.cancel()
    reopened = [e.opened.id for e in back if e.WhichOneof("ev") == "opened"]
    assert reopened == ["p-2"], back
    print("✓ agent reconnect re-publishes still-open prompts (offline-closed ones stay closed)")

    # 8) …and the re-published prompt is ANSWERABLE: the owner route was refreshed to
    #    the agent's NEW session, so RespondPrompt still lands as on_prompt_response.
    async with grpc.aio.insecure_channel(addr) as ch:
        ack2 = await rpc.ViewerStub(ch).RespondPrompt(
            pb.RespondPromptRequest(id="p-2", by="viewer-c", boolean=False))
        assert ack2.ok, ack2.detail
    assert await _until(lambda: len(answered) >= 2), \
        "an answer to a re-published prompt never reached the reconnected agent"
    assert answered[-1] == ("p-2", False, "viewer-c"), answered
    print("✓ re-published prompt routes answers down the NEW agent session")
    agent.stop()

    await server.stop(grace=0.5)
    print("\nPROMPTS E2E PASS — publish, answer-routing, resolution, snapshot, disconnect, "
          "reconnect re-publish")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
