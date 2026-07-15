---
id: add-a-device
title: Add a device (with setup notes)
when_to_use: The user asks to add/onboard a device. Capture its setup + lab-journal notes as part of adding it — provenance frozen alongside the data.
tags: [devices, onboarding, provenance, notes, journal]
verbs_used: [device.list, device.add, device.set_meta, device.get_meta, device.get]
---
Onboarding a device is also the moment to record WHY it's on the bench — the operator's setup notes plus any calibration / asset info the hardware doesn't self-report. **ALWAYS ASK THE USER for these before (or right after) adding**, then write them with `device.set_meta`. It lands in the lab-journal AND is frozen into the data store as provenance (change-logged over time), so recorded data always knows which device — with its cal/serial/notes at that instant — produced it.

## Steps
1. `device.list` — the device is under `available` (each carries `instance_id`, `driver`). (For a computed/external channel, use `device.create` instead — see the plot-live-external-data playbook.)
2. **ASK THE USER** — do NOT invent these: *what is it / how is it mounted* (→ `notes`), and any *calibration* (`cal_date`/`cal_due`/`cal_cert`), *asset tag*, or *serial* the device doesn't report itself. If the user has nothing to add, that's fine — skip.
3. `device.add {instance_id}` — onboard it; it becomes active.
4. `device.set_meta {instance_id, notes, cal_date?, asset_tag?, …}` — record what the user told you. Fields: notes, manufacturer, model, serial, firmware, cal_date, cal_due, cal_cert, asset_tag (any subset; user values win over device-reported).
5. `device.get_meta {instance_id}` — read the merged journal back to confirm.

## Verbs used
device.list, device.add, device.set_meta, device.get_meta, device.get

```skeleton
avail = query("device.list")["available"]
dev = avail[0]                                   # the one the user wants added
# ASK THE USER FIRST — e.g.:
# "What is this device and how is it set up? Any calibration date or asset tag?"
notes = ask_user("Setup notes for this device?")
command("device.add", {"instance_id": dev["instance_id"]})
command("device.set_meta", {"instance_id": dev["instance_id"], "notes": notes})
print(query("device.get_meta", {"instance_id": dev["instance_id"]}))
```
