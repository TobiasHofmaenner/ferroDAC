---
id: document-the-bench
title: Document the bench
when_to_use: Capture the physical setup — a photo of the rig plus a written note in the lab journal — so the run is reproducible later.
tags: [documentation, media, tags, journal]
verbs_used: [media.categories, media.add_photo, tag.add, doc.append, doc.list]
---
A bench record is a photo (filed under a category), a timeline tag, and a note in the project README lab-journal.

## Steps
1. `media.categories` — confirm the filing categories (setup/sample/result/generic).
2. `media.add_photo {data_b64, category: "setup", label, comment, ext}` — store the rig photo VERBATIM plus an immutable media tag (the phone companion uses the same verb). Keep the returned `relpath`.
3. `tag.add {label: "bench configured", comment}` — drop a marker at now so the change is findable on the timeline.
4. `doc.append {text}` — write a dated note into the README lab-journal (the default doc), referencing the photo's relpath.
5. `doc.list` — verify the README shows up.

## Verbs used
media.categories, media.add_photo, tag.add, doc.append, doc.list

```skeleton
photo = command("media.add_photo", {
    "data_b64": b64_of_jpeg, "category": "setup",
    "label": "PSU + DUT", "comment": "3 V rail, 4-wire", "ext": "jpg"})
command("tag.add", {"label": "bench configured", "comment": "see setup photo"})
command("doc.append", {"text":
    f"\n## {today}\nBench configured. Photo: {photo['relpath']}\n"})
```
