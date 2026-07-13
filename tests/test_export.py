"""Regression tests for the read-time CSV export (ferrodac.store.export).

Pins the bundle contract: absolute time, honest sparse-vs-forward-fill, traces in
their own matrix file, and a self-describing manifest. Qt-free (numpy + zarr).
"""

import csv
import json
import os
import tempfile

import numpy as np
import pytest

from ferrodac.core.uncertainty import Rel
from ferrodac.store import Resolver, RamTier, ZarrStore, export_window
from ferrodac.core.history import HistoryBuffer

BASE = 1_700_000_000.0


def _store_with_data():
    d = tempfile.mkdtemp()
    st = ZarrStore(os.path.join(d, "s.zarr"))
    # two channels on the SAME device cadence (shared timestamps)
    gt = BASE + np.arange(10) * 1.0
    st.add_source("dev:psu/voltage", name="Voltage", unit="V")
    st.append("dev:psu/voltage", gt, 5 + 0 * gt, epoch="e0")
    st.add_source("dev:psu/current", name="Current", unit="A")
    st.append("dev:psu/current", gt, 1 + 0 * gt, epoch="e0")
    # a slower gauge on a DIFFERENT cadence (offset times → blanks under no-fill)
    ht = BASE + 0.5 + np.arange(5) * 2.0
    st.add_source("dev:g/p", name="Pirani", unit="mbar")
    st.append("dev:g/p", ht, 1e-6 + 0 * ht, epoch="e0")
    # a trace source
    ax = np.linspace(1, 50, 32)
    st.add_source("rga/spec", name="Mass spectrum", unit="mbar", dtype="trace")
    for i in range(4):
        st.append_trace("rga/spec", BASE + i * 3, ax, np.exp(-((ax - 28) ** 2)), epoch="t0")
    return d, st


def _sources():
    return {
        "dev:psu/voltage": {"name": "Voltage", "unit": "V", "dtype": "float"},
        "dev:psu/current": {"name": "Current", "unit": "A", "dtype": "float"},
        "dev:g/p": {"name": "Pirani", "unit": "mbar", "dtype": "float"},
        "rga/spec": {"name": "Mass spectrum", "unit": "mbar", "dtype": "trace"},
    }


def _read(path):
    with open(path, newline="") as fh:
        return list(csv.reader(fh))


def test_export_adds_gum_uncertainty_columns():
    """A scalar source with a declared σ model gets a GUM companion column
    u(name) [unit] after its value column, and the manifest records the model."""
    d = tempfile.mkdtemp()
    st = ZarrStore(os.path.join(d, "s.zarr"))
    gt = BASE + np.arange(5) * 1.0
    st.add_source("dev:g/p", name="Pirani", unit="mbar")
    st.append("dev:g/p", gt, 1e-6 + 0 * gt, epoch="e0")
    model = Rel(0.1)                                   # σ = 10 % of reading
    st.put_device("dev:g", {"uncertainty:p": model.to_dict()})
    st.emit_device_meta("dev:g", BASE, "uncertainty:p", model.to_dict())

    sources = {"dev:g/p": {"name": "Pirani", "unit": "mbar", "dtype": "float"}}
    dest = os.path.join(d, "out")
    man = export_window(dest, sources, st, BASE, BASE + 10, store=st)

    header = _read(os.path.join(dest, "data.csv"))[0]
    assert "Pirani [mbar]" in header and "u(Pirani) [mbar]" in header    # GUM companion
    row = _read(os.path.join(dest, "data.csv"))[1]
    val = float(row[header.index("Pirani [mbar]")])
    u = float(row[header.index("u(Pirani) [mbar]")])
    assert u == pytest.approx(0.1 * val, rel=1e-6)     # σ = 10 % of the reading

    src = next(s for s in man["sources"] if s["key"] == "dev:g/p")
    assert src["uncertainty"]["column"] == "u(Pirani) [mbar]"
    assert src["uncertainty"]["k"] == 1
    assert src["uncertainty"]["model"] == model.to_dict()


def test_export_without_store_has_no_uncertainty_columns():
    d, st = _store_with_data()
    dest = os.path.join(d, "out")
    export_window(dest, _sources(), st, BASE, BASE + 100)     # no store= → no σ columns
    header = _read(os.path.join(dest, "data.csv"))[0]
    assert not any(h.startswith("u(") for h in header)


def test_export_bundle_structure_and_absolute_time():
    d, st = _store_with_data()
    res = Resolver([RamTier(HistoryBuffer()), st])
    dest = os.path.join(d, "out")
    man = export_window(dest, _sources(), res, BASE - 1, BASE + 30)

    assert os.path.exists(os.path.join(dest, "data.csv"))
    assert os.path.exists(os.path.join(dest, "manifest.json"))
    rows = _read(os.path.join(dest, "data.csv"))
    # ABSOLUTE time columns, then one column per scalar source
    assert rows[0][:2] == ["time_iso", "time_epoch_s"]
    assert "Voltage [V]" in rows[0] and "Pirani [mbar]" in rows[0]
    assert rows[1][0].startswith("20") and float(rows[1][1]) >= BASE  # epoch seconds

    # manifest is self-describing + reimport-ready (keys, dtypes, files)
    saved = json.load(open(os.path.join(dest, "manifest.json")))
    assert saved["fill"] == "none" and saved["time_columns"] == ["time_iso", "time_epoch_s"]
    by_key = {s["key"]: s for s in saved["sources"]}
    assert by_key["rga/spec"]["dtype"] == "trace" and by_key["rga/spec"]["file"].startswith("trace_")
    assert by_key["dev:psu/voltage"]["file"] == "data.csv"


def test_sparse_vs_forward_fill():
    d, st = _store_with_data()
    res = Resolver([RamTier(HistoryBuffer()), st])
    # no fill (default): Pirani (offset cadence) is BLANK on rows it didn't sample
    man = export_window(os.path.join(d, "raw"), _sources(), res, BASE - 1, BASE + 30, fill=False)
    rows = _read(os.path.join(d, "raw", "data.csv"))
    pir = rows[0].index("Pirani [mbar]")
    assert any(r[pir] == "" for r in rows[1:]), "no-fill should leave honest blanks"

    # forward-fill: Pirani carries its last value → no blanks once it has started
    export_window(os.path.join(d, "held"), _sources(), res, BASE - 1, BASE + 30, fill=True)
    rows_f = _read(os.path.join(d, "held", "data.csv"))
    started = [r for r in rows_f[1:] if float(r[1]) >= BASE + 0.5]
    assert started and all(r[pir] != "" for r in started), "fill should carry the last value"


def test_trace_matrix_file():
    d, st = _store_with_data()
    res = Resolver([RamTier(HistoryBuffer()), st])
    man = export_window(os.path.join(d, "out"), _sources(), res, BASE - 1, BASE + 30)
    tf = next(s["file"] for s in man["sources"] if s["dtype"] == "trace")
    rows = _read(os.path.join(d, "out", tf))
    assert rows[0][0] == "time_epoch_s" and len(rows[0]) == 1 + 32   # time + 32 m/z bins
    assert len(rows) - 1 == 4                                        # 4 scans
    assert float(rows[1][0]) >= BASE                                 # absolute scan time


def test_tags_in_export():
    d, st = _store_with_data()
    res = Resolver([RamTier(HistoryBuffer()), st])
    tags = [
        {"id": "t1", "t": BASE + 1, "label": "start", "kind": "tag",
         "severity": "info", "projects": ["pA"], "comment": "go"},
        {"id": "t2", "t": BASE - 1000, "label": "before window", "projects": ["pA"]},
        {"id": "t3", "t": BASE + 2, "t_end": BASE + 8, "label": "run",
         "kind": "recording", "projects": ["pA", "pB"]},
        {"id": "t4", "t": BASE + 3, "label": "gone", "deleted": True, "projects": ["pA"]},
    ]
    man = export_window(os.path.join(d, "out"), _sources(), res, BASE - 1, BASE + 30, tags=tags)
    assert man.get("tags_file") == "tags.csv" and man["tags"] == 2   # in-window, live
    rows = _read(os.path.join(d, "out", "tags.csv"))
    assert rows[0][:3] == ["time_iso", "time_epoch_s", "t_end_epoch_s"]
    assert {r[3] for r in rows[1:]} == {"start", "run"}     # t2 out, t4 tombstoned
    run = next(r for r in rows[1:] if r[3] == "run")
    assert run[6] == "pA;pB" and run[2]                    # projects col + span end


def test_only_sources_with_data_in_window():
    d, st = _store_with_data()
    res = Resolver([RamTier(HistoryBuffer()), st])
    # a window BEFORE any data → nothing exported, no data.csv
    man = export_window(os.path.join(d, "empty"), _sources(), res, BASE - 100, BASE - 50)
    assert man["sources"] == []
    assert not os.path.exists(os.path.join(d, "empty", "data.csv"))


def test_export_carries_span_media(tmp_path):
    """§9.3 phase 2: photos + clips in the span are copied into <dest>/media/ and
    listed in manifest['media']; out-of-span, deleted, and escaping payloads are
    excluded, a missing file is skipped, multi-part clips carry all parts."""
    from ferrodac.store import export_window
    from ferrodac.store import Resolver, RamTier, ZarrStore
    from ferrodac.core.history import HistoryBuffer
    import os
    proj = tmp_path / "proj"
    (proj / "media").mkdir(parents=True)
    for n, data in (("a.png", b"PNG"), ("c.part1.mp4", b"C1"), ("c.part2.mp4", b"C2")):
        (proj / "media" / n).write_bytes(data)
    st = ZarrStore(os.path.join(str(tmp_path), "s.zarr"))
    import numpy as np
    gt = BASE + np.arange(10) * 1.0
    st.add_source("dev/p", name="P", unit="mbar")
    st.append("dev/p", gt, 1e-6 + 0 * gt, epoch="e0")
    res = Resolver([RamTier(HistoryBuffer()), st])
    sources = {"dev/p": {"name": "P", "unit": "mbar", "dtype": "float"}}
    tags = [
        {"id": "p1", "kind": "media", "t": BASE + 2, "payload":
         {"file": "media/a.png", "format": "png", "source": "cam/frame"}},
        {"id": "c1", "kind": "media", "t": BASE + 1, "t_end": BASE + 8, "payload":
         {"file": "media/c.part1.mp4", "files": ["media/c.part1.mp4", "media/c.part2.mp4"],
          "format": "mp4", "source": "cam/frame", "rec_mid": "R1"}},
        {"id": "x1", "kind": "media", "t": BASE + 999, "payload":
         {"file": "media/a.png", "format": "png"}},                     # out of span
        {"id": "x2", "kind": "media", "t": BASE + 2, "deleted": True,
         "payload": {"file": "media/a.png", "format": "png"}},          # deleted
        {"id": "x3", "kind": "media", "t": BASE + 2,
         "payload": {"file": "../../etc/passwd", "format": "png"}},     # escape
        {"id": "x4", "kind": "media", "t": BASE + 2,
         "payload": {"file": "media/gone.png", "format": "png"}},       # missing
    ]
    dest = tmp_path / "out"
    man = export_window(str(dest), sources, res, BASE - 1, BASE + 30,
                        tags=tags, media_root=str(proj))
    assert man["media_dir"] == "media"
    bundled = sorted(os.listdir(str(dest / "media")))
    assert bundled == ["a.png", "c.part1.mp4", "c.part2.mp4"]           # 1 photo + 2 parts
    kinds = sorted(m["kind"] for m in man["media"])
    assert kinds == ["clip", "photo"]
    clip = next(m for m in man["media"] if m["kind"] == "clip")
    assert len(clip["files"]) == 2 and clip["rec_mid"] == "R1"


def test_append_media_to_bundle_patches_manifest(tmp_path):
    """A clip landing AFTER the auto-export copies into the existing run bundle +
    patches manifest.json; a re-slice of the same source replaces, not piles up."""
    from ferrodac.store import append_media_to_bundle, export_window
    from ferrodac.store import Resolver, RamTier, ZarrStore
    from ferrodac.core.history import HistoryBuffer
    import json
    import numpy as np
    import os
    proj = tmp_path / "proj"
    (proj / "media").mkdir(parents=True)
    (proj / "media" / "clip.mp4").write_bytes(b"V1")
    st = ZarrStore(os.path.join(str(tmp_path), "s.zarr"))
    gt = BASE + np.arange(5) * 1.0
    st.add_source("dev/p", name="P", unit="mbar"); st.append("dev/p", gt, 0*gt+1, epoch="e0")
    res = Resolver([RamTier(HistoryBuffer()), st])
    dest = tmp_path / "run"
    export_window(str(dest), {"dev/p": {"name": "P", "unit": "mbar", "dtype": "float"}},
                  res, BASE - 1, BASE + 10, tags=[], media_root=str(proj))  # no media yet
    assert "media" not in json.load(open(str(dest / "manifest.json")))

    clip_tag = [{"id": "c1", "kind": "media", "t": BASE + 1, "t_end": BASE + 5,
                 "payload": {"file": "media/clip.mp4", "files": ["media/clip.mp4"],
                             "format": "mp4", "source": "cam/frame", "rec_mid": "R1"}}]
    n = append_media_to_bundle(str(dest), clip_tag, BASE - 1, BASE + 10, str(proj))
    assert n == 1
    man = json.load(open(str(dest / "manifest.json")))
    assert man["media"][0]["kind"] == "clip" and os.path.exists(str(dest / "media" / "clip.mp4"))

    # re-slice: a bigger clip replaces the same source's entry (not duplicate)
    (proj / "media" / "clip2.mp4").write_bytes(b"V2-longer")
    clip_tag[0]["payload"]["files"] = ["media/clip2.mp4"]
    clip_tag[0]["payload"]["file"] = "media/clip2.mp4"
    n2 = append_media_to_bundle(str(dest), clip_tag, BASE - 1, BASE + 10, str(proj))
    man2 = json.load(open(str(dest / "manifest.json")))
    assert n2 == 1 and len(man2["media"]) == 1                          # replaced, not piled
    assert man2["media"][0]["files"] == ["clip2.mp4"]
