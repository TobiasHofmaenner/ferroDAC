"""The dataflow graph — the core, Qt-free routing model (DESIGN §4.1).

Lifts the routing that grew up inside the UI Dashboard (`ui/workspace.py`) into a
single, headless, queryable model: **nodes** (sources / sinks / processors /
devices) and **edges** (a source routed to a sink/processor). This is the one
substrate the patch-bay edits, dataflow *introspection* draws, and the replay /
distributed-compute layers consult.

Qt-free on purpose: the UI registers a change callback (bridging to its Qt
signal); headless consumers (hub, replay, compute nodes) use the graph directly.
Node carries the distribution seams (`placement`, `parallel`) even though
everything is `local` today (DESIGN §4.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCE, SINK, PROCESSOR, DEVICE = "source", "sink", "processor", "device"


@dataclass
class Node:
    id: str
    kind: str                                  # SOURCE | SINK | PROCESSOR | DEVICE
    name: str = ""
    dtype: str = ""                            # sources: float|bool|trace|…
    unit: str = ""
    origin: str = ""                           # sources: device|virtual|input|CV·…
    accepts: frozenset = frozenset()           # sinks: dtypes they take
    single_bind: bool = False                  # sinks: one input only
    ptype: str = ""                            # processors: registered kind
    input_key: str = ""                        # processors: bound input source id
    placement: str = "local"                   # distribution seam: local|peer|hub
    parallel: str = "map"                       # distribution seam: map|windowed|reduce
    meta: dict = field(default_factory=dict)


class DataflowGraph:
    def __init__(self):
        self._nodes: dict[str, Node] = {}
        self._out: dict[str, set] = {}         # src_id -> {dst_id}
        self._in: dict[str, set] = {}          # dst_id -> {src_id}  (reverse index)
        self._subs: list = []

    # -- nodes ---------------------------------------------------------------
    def add_node(self, node: Node) -> Node:
        self._nodes[node.id] = node
        self._out.setdefault(node.id, set())
        self._in.setdefault(node.id, set())
        self._notify()
        return node

    def remove_node(self, nid: str) -> None:
        if nid not in self._nodes:
            return
        for dst in self._out.pop(nid, set()):
            self._in[dst].discard(nid)
        for src in self._in.pop(nid, set()):
            self._out[src].discard(nid)
        del self._nodes[nid]
        self._notify()

    def get(self, nid: str): return self._nodes.get(nid)
    def has(self, nid: str) -> bool: return nid in self._nodes

    def nodes(self, kind: str = None) -> list:
        return [n for n in self._nodes.values() if kind is None or n.kind == kind]

    def sources(self): return self.nodes(SOURCE)
    def sinks(self): return self.nodes(SINK)
    def processors(self): return self.nodes(PROCESSOR)

    # -- edges ---------------------------------------------------------------
    def connect(self, src_id: str, dst_id: str) -> None:
        if src_id not in self._nodes or dst_id not in self._nodes:
            return
        if dst_id in self._out[src_id]:
            return
        self._out[src_id].add(dst_id)
        self._in[dst_id].add(src_id)
        self._notify()

    def disconnect(self, src_id: str, dst_id: str) -> None:
        if dst_id in self._out.get(src_id, ()):
            self._out[src_id].discard(dst_id)
            self._in[dst_id].discard(src_id)
            self._notify()

    def is_connected(self, src_id: str, dst_id: str) -> bool:
        return dst_id in self._out.get(src_id, ())

    def edges(self) -> list:
        return [(s, d) for s, ds in self._out.items() for d in ds]

    def targets(self, src_id: str) -> set:
        """Node ids `src_id` feeds (the source's routes)."""
        return set(self._out.get(src_id, ()))

    def routed(self, src_id: str) -> bool:
        return bool(self._out.get(src_id))

    def inputs_of(self, nid: str) -> list:
        """Source nodes feeding `nid` (for a sink or processor)."""
        return [self._nodes[s] for s in self._in.get(nid, ()) if s in self._nodes]

    def downstream_of(self, nid: str) -> list:
        return [self._nodes[d] for d in self._out.get(nid, ()) if d in self._nodes]

    def replace(self, other: "DataflowGraph") -> None:
        """Swap in another graph's nodes + edges and fire one change. Lets a UI
        rebuild a fresh snapshot and atomically publish it to observers."""
        self._nodes = dict(other._nodes)
        self._out = {k: set(v) for k, v in other._out.items()}
        self._in = {k: set(v) for k, v in other._in.items()}
        self._notify()

    # -- change notification (Qt-free observer; UI bridges to its signal) ----
    def subscribe(self, cb):
        self._subs.append(cb)
        return lambda: self._subs.remove(cb) if cb in self._subs else None

    def _notify(self):
        for cb in list(self._subs):
            try:
                cb()
            except Exception:
                pass

    # -- serialization (edges + the persistable node fields) -----------------
    def to_dict(self) -> dict:
        return {"routes": {s: sorted(ds) for s, ds in self._out.items() if ds}}

    def load_routes(self, routes: dict) -> None:
        for src, dsts in (routes or {}).items():
            for dst in dsts:
                self.connect(src, dst)
