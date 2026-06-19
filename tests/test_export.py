"""Regression tests for the read-time CSV export (ferrodac.store.export).

Pins the bundle contract: absolute time, honest sparse-vs-forward-fill, traces in
their own matrix file, and a self-describing manifest. Qt-free (numpy + zarr).
"""

import csv
import json
import os
import tempfile

import numpy as np

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


def test_only_sources_with_data_in_window():
    d, st = _store_with_data()
    res = Resolver([RamTier(HistoryBuffer()), st])
    # a window BEFORE any data → nothing exported, no data.csv
    man = export_window(os.path.join(d, "empty"), _sources(), res, BASE - 100, BASE - 50)
    assert man["sources"] == []
    assert not os.path.exists(os.path.join(d, "empty", "data.csv"))
