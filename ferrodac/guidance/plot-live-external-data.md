---
id: plot-live-external-data
title: Plot live external data
when_to_use: Chart a value that ISN'T a lab instrument — an HTTP endpoint, a web API, or a computed quantity (e.g. a live CHF/EUR FX rate). Use a Python source.
tags: [python-source, webhook, http, chart, routing]
verbs_used: [device.list, device.add, device.config_get, device.config_set, source.list, layout.add_panel, layout.route, source.read]
---
A **Python source** is a virtual device whose configuration is a block of Python the app EXECUTES on a timer; each returned value is emitted as a Reading and routes like any real channel. This subsumes a webhook — the code can fetch an HTTP endpoint.

## Steps
1. `device.list` — the Python source appears under `available` (driver "python_source"). Note its `instance_id` and `uuid`. (A user creates one from the desktop app's "Add Python source…" action; once created it is drivable here.)
2. `device.add {instance_id}` — onboard it; it becomes active with its declared source(s).
3. `device.config_get {instance_id}` — confirm the code option's key and the current script.
4. `device.config_set {instance_id, option: "code", value: <python>}` — set the code. The script declares its channels and a `poll(ctx)` that RETURNS the number(s) to emit. Minimal CHF poller:
       SOURCES = [{"id": "rate", "name": "CHF/EUR", "unit": ""}]
       import urllib.request, json
       def poll(ctx):
           with urllib.request.urlopen(
                   "https://api.example.com/fx?base=CHF&quote=EUR", timeout=5) as r:
               return {"rate": float(json.load(r)["rate"])}
5. `source.list` — find the new source's `key` (its `origin` is the Python source's uuid; dtype float).
6. `layout.add_panel {kind: "chart"}`; `layout.route {source_key, sink_key: panel_id}`.
7. `source.read {key}` — confirm the fetched value is arriving live.

> NOTE: the exact code option key ("code") and the poll(ctx) contract come from the Python source driver — verify with `device.config_get` before assuming them.

## Verbs used
device.list, device.add, device.config_get, device.config_set, source.list, layout.add_panel, layout.route, source.read

```skeleton
avail = query("device.list")["available"]
tmpl = next(d for d in avail if d["driver"] == "python_source")
command("device.add", {"instance_id": tmpl["instance_id"]})
command("device.config_set", {"instance_id": tmpl["instance_id"], "option": "code",
    "value": ("SOURCES = [{'id':'rate','name':'CHF/EUR','unit':''}]\n"
              "import urllib.request, json\n"
              "def poll(ctx):\n"
              "    with urllib.request.urlopen('https://api.example.com/fx?"
              "base=CHF&quote=EUR', timeout=5) as r:\n"
              "        return {'rate': float(json.load(r)['rate'])}\n")})
src = next(p for p in query("source.list")
           if p["origin"] == tmpl["uuid"] and p["dtype"] == "float")
panel = command("layout.add_panel", {"kind": "chart"})["panel_id"]
command("layout.route", {"source_key": src["key"], "sink_key": panel})
```
