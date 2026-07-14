---
id: set-up-a-live-readout
title: Set up a live readout
when_to_use: The user wants a source's current value shown live on the dashboard — a chart line or a big 7-segment numeric panel.
tags: [dashboard, routing, live, numeric]
verbs_used: [source.list, layout.add_panel, layout.route, source.read]
---
Put a channel on screen in three moves: find the source, make a panel, wire them.

## Steps
1. `source.list` — find the channel. Each entry has `key` ("<uuid>/<source_id>"), `name`, `dtype` (float/bool/trace/image), `unit`, `online`. Pick a scalar (float/bool) for a numeric readout; a trace for a chart line.
2. `layout.add_panel {kind: "numeric"}` for a single-value 7-segment readout, or `{kind: "chart"}` to plot over time. Keep the returned `panel_id` — that is the sink.
3. `layout.route {source_key: <key>, sink_key: <panel_id>}`. Check `attached` is true. A chart REFUSES a dimensionally-incompatible source (then attached=false and `routed` omits the panel) — pick a matching source/unit.
4. `source.read {key: <key>}` — confirm a live value (non-null `value`, recent `t`).

## Verbs used
source.list, layout.add_panel, layout.route, source.read

```skeleton
ports = query("source.list")
src = next(p for p in ports if p["dtype"] == "float" and p["online"])
panel = command("layout.add_panel", {"kind": "numeric"})["panel_id"]
r = command("layout.route", {"source_key": src["key"], "sink_key": panel})
assert r["attached"], "panel refused the source (dimension mismatch)"
print(query("source.read", {"key": src["key"]}))
```
