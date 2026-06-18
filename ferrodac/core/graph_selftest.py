"""Self-test for the core DataflowGraph (DESIGN §4.1).
Run: python3 -m ferrodac.core.graph_selftest
"""

from __future__ import annotations

from .graph import DataflowGraph, Node, PROCESSOR, SINK, SOURCE


def main() -> int:
    g = DataflowGraph()
    fired = [0]
    g.subscribe(lambda: fired.__setitem__(0, fired[0] + 1))

    g.add_node(Node("dev/p", SOURCE, "Pressure", dtype="float", origin="device"))
    g.add_node(Node("dev/spec", SOURCE, "Spectrum", dtype="trace", origin="device"))
    g.add_node(Node("chart-1", SINK, "Chart", accepts=frozenset({"float"})))
    g.add_node(Node("gas1", PROCESSOR, "Gas fit", ptype="gas", input_key="dev/spec"))
    g.add_node(Node("gas1/Acetone", SOURCE, "Acetone", dtype="float", origin="virtual"))
    g.connect("dev/p", "chart-1")
    g.connect("dev/spec", "gas1")
    g.connect("gas1/Acetone", "chart-1")

    assert len(g.nodes()) == 5 and len(g.edges()) == 3
    assert g.targets("dev/p") == {"chart-1"} and g.routed("dev/spec")
    assert sorted(n.id for n in g.inputs_of("chart-1")) == ["dev/p", "gas1/Acetone"]
    assert [n.id for n in g.inputs_of("gas1")] == ["dev/spec"]
    assert [n.id for n in g.downstream_of("dev/spec")] == ["gas1"]
    print("✓ nodes/edges, targets, inputs_of, downstream_of")

    g.add_node(Node("dev/idle", SOURCE, dtype="float"))
    assert not g.routed("dev/idle")
    print("✓ routed(): unrouted source is False (capture-set query)")

    g.remove_node("chart-1")
    assert g.edges() == [("dev/spec", "gas1")] and g.targets("dev/p") == set()
    print("✓ remove_node cleans edges both directions")

    d = g.to_dict()
    g2 = DataflowGraph()
    for n in g.nodes():
        g2.add_node(n)
    g2.load_routes(d["routes"])
    assert sorted(g2.edges()) == sorted(g.edges())
    print("✓ routes serialize + reload")

    assert fired[0] > 0
    print("✓ change observer fires (UI bridges this to its Qt signal)")

    print("\nGRAPH SELFTEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
