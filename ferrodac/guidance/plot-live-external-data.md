---
id: plot-live-external-data
title: Plot live external data
when_to_use: Chart a value that ISN'T a lab instrument — an HTTP endpoint, a web API, or a computed quantity (e.g. a live CHF/EUR FX rate). Use a Python device.
tags: [python-source, webhook, http, chart, routing]
verbs_used: [device.create, device.config_set, device.config_get, source.list, layout.add_panel, layout.route, source.read]
---
A **Python device** is a virtual device whose configuration is a block of Python the app EXECUTES on a timer; each returned value is emitted as a Reading and routes like any real channel. This subsumes a webhook — the code can fetch an HTTP endpoint. You can create and drive one **entirely over the API** with `device.create`.

## Steps
1. `device.create {kind: "python_device", code: <python>}` — create + activate a Python device with your `poll(ctx)` script in ONE call. It returns `{instance_id, uuid, sources, last_error}`. If `last_error` is non-null the code didn't compile — fix it and `device.config_set` the "code" option. A minimal CHF poller (declare the channel, then `poll(ctx)` returns the number):
       SOURCES = [{"id": "rate", "name": "CHF/EUR", "unit": ""}]
       import urllib.request, json
       def poll(ctx):
           with urllib.request.urlopen(
                   "https://api.example.com/fx?base=CHF&quote=EUR", timeout=5) as r:
               return {"rate": float(json.load(r)["rate"])}
2. Build the source key directly from the descriptor: `"<uuid>/<source_id>"` (e.g. `dev["uuid"] + "/rate"`). If you instead scan `source.list`, match on the key PREFIX being the uuid — note `source.list`'s `origin` is the device NAME, not the uuid.
3. `layout.add_panel {kind: "chart"}`; `layout.route {source_key, sink_key: panel_id}` (check `attached`).
4. `source.read {key}` — confirm the fetched value is arriving live.
5. To change the script later: `device.config_set {instance_id, option: "code", value: <python>}` — it hot-reloads on the running device.

> NOTE: `poll(ctx)` runs in-process with full trust (no sandbox). ctx gives `state` (persists across polls), `t` (wall time), `log`. Put a timeout on any HTTP call.

## Verbs used
device.create, device.config_set, device.config_get, source.list, layout.add_panel, layout.route, source.read

```skeleton
dev = command("device.create", {"kind": "python_device", "code":
    ("SOURCES=[{'id':'rate','name':'CHF/EUR','unit':''}]\n"
     "import urllib.request, json\n"
     "def poll(ctx):\n"
     "    with urllib.request.urlopen('https://api.example.com/fx?"
     "base=CHF&quote=EUR', timeout=5) as r:\n"
     "        return {'rate': float(json.load(r)['rate'])}\n")})
assert not dev["last_error"], dev["last_error"]      # code compiled
key = dev["uuid"] + "/rate"
panel = command("layout.add_panel", {"kind": "chart"})["panel_id"]
command("layout.route", {"source_key": key, "sink_key": panel})
print(query("source.read", {"key": key}))            # the fetched value, live
```
