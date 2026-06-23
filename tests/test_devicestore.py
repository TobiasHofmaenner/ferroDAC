"""Per-device provenance records in the Zarr store (Phase 1): identity + a
metadata change-log that folds to 'record as of time T', stored alongside the
data so historic measurements know which instrument produced them."""
import os

from ferrodac.store import ZarrStore


def _store(tmp_path):
    return ZarrStore(os.path.join(str(tmp_path), "s.zarr"))


def test_put_device_and_fold_change_log(tmp_path):
    st = _store(tmp_path)
    did = "sim:gauge:A"                                    # colon id (Windows-unsafe)
    st.put_device(did, {"device_id": did, "name": "Sim Gauge A", "model": "SG6",
                        "cal_due": "2027-01-01"})
    # seed the log at t0, then change calibration at t1 (the writer's pattern)
    st.emit_device_meta(did, 100.0, "name", "Sim Gauge A")
    st.emit_device_meta(did, 100.0, "cal_due", "2027-01-01")
    st.emit_device_meta(did, 200.0, "cal_due", "2028-06-01")     # recalibrated

    assert st.device_record_at(did, 150.0)["cal_due"] == "2027-01-01"   # before change
    assert st.device_record_at(did, 250.0)["cal_due"] == "2028-06-01"   # after change
    assert st.device_record_at(did, 250.0)["name"] == "Sim Gauge A"


def test_colon_id_round_trips(tmp_path):
    st = _store(tmp_path)
    st.put_device("sim:gauge:A", {"device_id": "sim:gauge:A", "name": "G"})
    # reopen from disk → the colon id survives the Windows-safe group name
    st2 = ZarrStore(os.path.join(str(tmp_path), "s.zarr"), mode="r")
    assert st2.device_record_at("sim:gauge:A", 1e12)["name"] == "G"
    assert st2.device_ids() == ["sim:gauge:A"]


def test_sources_excludes_devices_group(tmp_path):
    st = _store(tmp_path)
    st.add_source("dev1/ch1", name="ch1")
    st.put_device("dev1", {"device_id": "dev1", "name": "Dev 1"})
    assert st.sources() == ["dev1/ch1"]                   # 'devices' group is NOT a source
    assert st.epoch_lengths() == {}                       # and not iterated as one


def test_unknown_device_and_empty_store_degrade(tmp_path):
    st = _store(tmp_path)
    assert st.device_record_at("nope", 1.0) == {}         # unknown → empty, no raise
    assert st.device_records() == []                      # no devices group yet
    assert st.device_ids() == []
