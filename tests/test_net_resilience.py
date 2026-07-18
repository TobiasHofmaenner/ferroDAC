"""The hub link must survive failures without dying silently.

Regressions for the lab outage where the hub host rebooted and every client's
grpc.aio loop died permanently while the UI stayed green and the store sync
kept reporting "synced N samples" — so the agent LOOKED like it was streaming
but no viewer ever saw live data again until an app restart.

Three pinned behaviors:
 1. A CancelledError surfaced by the CALL (grpc.aio raises it when a dying
    stream is locally cancelled, e.g. the hub vanishing mid-write) must NOT end
    the reconnect loop — only stop() may.
 2. stop()/announce()/feed() must tolerate a dead worker (closed loop): a wedged
    agent must never abort HubController.disconnect()/reconnect with
    "Event loop is closed".
 3. Restart e2e: the hub goes away mid-stream and comes back → the agent
    reconnects and RE-ANNOUNCES, so viewers' catalogs self-heal.
"""
import asyncio
import socket
import threading
import time
import types

import pytest

grpc = pytest.importorskip("grpc")
pytest.importorskip("ferrodac_contract.v1.data_plane_pb2")

from ferrodac.core.reading import Reading           # noqa: E402
from ferrodac.net.agent import HubAgent             # noqa: E402


def _desc(uuid="fake-uuid-1", name="Fake Gauge"):
    # convert.descriptor_to_proto only reads attributes
    return types.SimpleNamespace(
        uuid=uuid, instance_id="fake-1", name=name, driver="fake",
        hardware_id="", firmware="",
        sources=[types.SimpleNamespace(id="ch1", name="ch1", unit="mbar",
                                       dtype="float")],
        sinks=[])


def test_session_loop_survives_a_cancelled_call(monkeypatch):
    """A CancelledError from the channel/call while stop() was NOT requested is a
    disconnect → the loop must retry, not silently end (the wedged-agent bug)."""
    import ferrodac.net.session as session_mod   # the reconnect FSM opens the channel

    attempts = {"n": 0}

    class CancelledChannel:
        def __init__(self, *a, **k):
            attempts["n"] += 1

        async def __aenter__(self):
            raise asyncio.CancelledError()      # what a dying call surfaces as

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(session_mod.grpc.aio, "insecure_channel", CancelledChannel)
    states = []
    ag = HubAgent("127.0.0.1:1", agent_id="t",
                  on_state=lambda c, d: states.append((c, d)))
    ag.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and attempts["n"] < 2:
            time.sleep(0.05)
        assert attempts["n"] >= 2, \
            f"loop died after the first CancelledError ({attempts['n']} attempt(s))"
        assert any("cancelled" in d for _, d in states)   # surfaced as a disconnect
    finally:
        ag.stop()
    assert not ag._thread, "stop() did not tear the worker down"


def test_agent_api_tolerates_a_dead_worker():
    """What a crashed/finished worker leaves behind is a CLOSED loop. The GUI
    thread's stop() (via disconnect→reconnect) and the engine thread's feed()
    must both shrug, not raise 'Event loop is closed'."""
    ag = HubAgent("127.0.0.1:1", agent_id="t")
    loop = asyncio.new_event_loop()
    loop.close()
    ag._loop = loop
    ag.announce(_desc())                                   # GUI-thread path
    ag.feed([Reading(device="fake-uuid-1", source="ch1",
                     t=time.time(), value=1.0)])           # engine-thread path
    ag.stop()                                              # disconnect path


@pytest.mark.integration
def test_agent_reannounces_after_hub_restart():
    """Hub dies mid-stream and comes back on the same port → the agent must
    reconnect and re-announce its devices (viewers' catalogs self-heal)."""
    from ferrodac_contract.v1 import data_plane_pb2 as pb
    from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc
    from hub.main import build_server

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    addr = f"127.0.0.1:{port}"

    agent = HubAgent(addr, agent_id="e2e-restart")
    feeding = threading.Event()
    feeding.set()

    def feeder():
        while feeding.is_set():
            agent.feed([Reading(device="fake-uuid-1", source="ch1",
                                t=time.time(), value=1.23)])
            time.sleep(0.1)

    async def has_device() -> bool:
        async with grpc.aio.insecure_channel(addr) as ch:
            cat = await rpc.ViewerStub(ch).GetCatalog(pb.CatalogRequest())
            return any(d.name == "Fake Gauge" and d.online for d in cat.devices)

    async def wait_for_device(timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await has_device():
                    return True
            except Exception:
                pass                                # hub (re)starting
            await asyncio.sleep(0.25)
        return False

    async def scenario() -> tuple:
        server1, _ = build_server()
        server1.add_insecure_port(addr)
        await server1.start()

        agent.start()
        agent.announce(_desc())
        threading.Thread(target=feeder, daemon=True).start()
        before = await wait_for_device(10)

        await server1.stop(grace=None)              # hub vanishes mid-stream
        await asyncio.sleep(1.0)                    # agent is now in its retry loop

        server2, _ = build_server()
        server2.add_insecure_port(addr)
        await server2.start()
        after = await wait_for_device(15)           # reconnect + re-announce
        await server2.stop(grace=None)
        return before, after

    try:
        before, after = asyncio.run(scenario())
    finally:
        feeding.clear()
        agent.stop()
    assert before, "device never appeared in the catalog on first connect"
    assert after, "agent did not re-announce after the hub restart"


def test_outgen_replays_the_open_prompt_mirror():
    """§7.3 reconnect: every (re)connected session must RE-PUBLISH the agent's
    still-open prompts (the hub closes them when a session drops, leaving viewers
    blind after a blip/restart) — and must NOT resurrect one closed while offline."""
    from ferrodac_contract.v1 import data_plane_pb2 as pb

    ag = HubAgent("127.0.0.1:1", agent_id="t")           # never started: offline
    ag.publish_prompt(pb.WirePrompt(id="p-1", device_uuid="d", question="A?"))
    ag.publish_prompt(pb.WirePrompt(id="p-2", device_uuid="d", question="B?"))
    ag.close_prompt("p-2", by="device")                  # resolved while offline

    async def session_opening():
        # what _run_session does before draining the queue
        ag._stop = asyncio.Event()
        ag._outq = asyncio.Queue()
        ag._outq.put_nowait(None)                        # end the generator after the replay
        return [m async for m in ag._outgen()]

    msgs = asyncio.run(session_opening())
    assert msgs[0].WhichOneof("msg") == "hello"
    opened = [m.prompt.opened.id for m in msgs
              if m.WhichOneof("msg") == "prompt" and m.prompt.WhichOneof("ev") == "opened"]
    assert opened == ["p-1"], f"mirror replay wrong: {opened}"


def test_promptwatch_respond_reports_the_ack():
    """§7.3 answer relay: the viewer must HEAR what became of its answer — a delivered
    Ack, an authoritative hub rejection (ok=False: prompt gone), or NO Ack at all
    (None: channel down / transport failure) — never a silent void (the old behavior
    swallowed everything and the card just vanished)."""
    from ferrodac_contract.v1 import data_plane_pb2 as pb

    from ferrodac.net.prompts import HubPromptWatch

    results = []
    w = HubPromptWatch("127.0.0.1:1")

    # not started (no loop): the caller hears the failure immediately
    w.respond("p-1", True, on_result=lambda ok, d: results.append((ok, d)))
    assert results == [(None, "prompt channel not running")]

    class _Stub:
        def __init__(self, ack=None, boom=None):
            self.ack, self.boom = ack, boom

        async def RespondPrompt(self, req):              # noqa: N802
            if self.boom is not None:
                raise self.boom
            return self.ack

    def roundtrip(stub):
        w._stub = stub
        req = pb.RespondPromptRequest(id="p", boolean=True)
        asyncio.run(w._respond(req, lambda ok, d: results.append((ok, d))))
        return results[-1]

    assert roundtrip(_Stub(ack=pb.Ack(ok=True))) == (True, "")
    assert roundtrip(_Stub(ack=pb.Ack(ok=False, detail="no such open prompt"))) == \
        (False, "no such open prompt")
    ok, detail = roundtrip(_Stub(boom=RuntimeError("hub unreachable")))
    assert ok is None and "unreachable" in detail
    # between sessions (stub gone): also a no-Ack, not silence
    assert roundtrip(None) == (None, "prompt channel disconnected")
    # and a broken observer must never propagate out of the relay
    w._stub = _Stub(ack=pb.Ack(ok=True))
    asyncio.run(w._respond(pb.RespondPromptRequest(id="p", boolean=True),
                           lambda ok, d: 1 / 0))
