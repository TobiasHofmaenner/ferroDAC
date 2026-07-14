---
id: annotate-an-experiment
title: Annotate an experiment
when_to_use: Mark events on the timeline during or after a run — start/stop, a step change, an anomaly — and refine or remove those marks.
tags: [tags, markers, timeline, annotation]
verbs_used: [tag.add, tag.list, tag.update, tag.remove, time.seek]
---
Tags are cheap and reversible — annotate freely. Add at `now` during a live run, or pass a past `t` to mark an earlier instant.

## Steps
1. `tag.add {label, comment, t?}` — add a marker. Omit `t` to tag NOW; pass a unix `t` to mark a past event (tag.add defaults to now, NOT the replay head). Keep the returned `id`.
2. `tag.list` — review every marker (id, t, label, severity).
3. `tag.update {id, label?, comment?, severity?, color?}` — refine a marker; severity is one of the allowed set. The tag's TIME is immutable.
4. Reviewing a past event? `time.seek {t}` scrubs the replay head there to inspect context, then `tag.add {label, t}` marks that exact instant.
5. `tag.remove {id}` — tombstone a mistaken tag (destructive: needs admin scope + confirm=true; the delete propagates over hub sync).

## Verbs used
tag.add, tag.list, tag.update, tag.remove, time.seek

```skeleton
tid = command("tag.add", {"label": "run start"})["id"]
# ... run proceeds ...
command("tag.add", {"label": "anomaly", "comment": "spike ch2", "t": time.time()})
command("tag.update", {"id": tid, "severity": "info", "comment": "baseline ok"})
# undo a mistake (admin + confirm):
# command("tag.remove", {"id": bad_id}, confirm=True)
```
