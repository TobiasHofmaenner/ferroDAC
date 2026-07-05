"""The Zarr store stamps a format version at every persistence root (store / source /
device / epoch) so a future breaking layout change has a home + a migrator hook, and an
old unversioned store reads back as 0 (legacy) rather than being mislabelled. Qt-free."""
import os
import tempfile

import numpy as np

from ferrodac.store import ZarrStore


def _new():
    return ZarrStore(os.path.join(tempfile.mkdtemp(), "s.zarr"))


def test_new_store_is_stamped_v1():
    st = _new()
    assert st.schema_version() == ZarrStore.SCHEMA_VERSION == 1
    assert st.root.attrs["schema_version"] == 1


def test_source_device_and_epoch_groups_carry_the_version():
    st = _new()
    st.add_source("dev/a", unit="mbar")
    st.append("dev/a", np.array([1.0, 2.0]), np.array([1.0, 2.0]), epoch="e0")
    st.put_device("dev", {"name": "Gauge"})
    v = ZarrStore.SCHEMA_VERSION
    assert st.root[st._gname("dev/a")].attrs["schema_version"] == v
    assert st.root[st._gname("dev/a")]["e0"].attrs["schema_version"] == v
    assert st.root["devices"][st._gname("dev")].attrs["schema_version"] == v


def test_trace_epoch_carries_the_version():
    st = _new()
    st.add_source("rga/spec", dtype="trace")
    st.append_trace("rga/spec", 1.0, np.arange(8.0), np.ones(8), epoch="e0")
    assert st.root[st._gname("rga/spec")]["e0"].attrs["schema_version"] == ZarrStore.SCHEMA_VERSION


def test_legacy_store_reads_back_as_zero_not_mislabelled():
    """A store written before versioning existed (no stamp, has data) must NOT be
    silently claimed as v1 — the reader defaults to 0 (legacy) so a migrator can act."""
    path = os.path.join(tempfile.mkdtemp(), "legacy.zarr")
    st = ZarrStore(path)
    st.add_source("dev/a")                       # give it content
    del st.root.attrs["schema_version"]          # simulate a pre-versioning store
    reopened = ZarrStore(path)
    assert reopened.schema_version() == 0        # legacy, not re-stamped as v1
    assert "schema_version" not in reopened.root.attrs
