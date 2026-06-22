"""Per-device lab-journal metadata + the descriptor merge (Qt-free)."""
import types


def _desc(**kw):
    base = dict(name="RGA", driver="qms", instance_id="i1", hardware_id="SN-1",
                model="Q200", firmware="1.2", manufacturer="Acme",
                cal_date="2026-01-01", cal_due="2027-01-01", cal_cert="C1",
                asset_tag=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_device_key_prefers_serial():
    from ferrodac.core.devicemeta import device_key
    assert device_key(_desc()) == "SN-1"
    assert device_key(_desc(hardware_id=None)) == "i1"   # falls back to instance id


def test_merge_descriptor_then_user_override(tmp_path):
    from ferrodac.core.devicemeta import DeviceMeta, device_key, merge_device_info
    d = _desc()
    meta = DeviceMeta(str(tmp_path / "dm.json"))
    info = merge_device_info(d, meta.get(device_key(d)))      # no user meta
    assert info["serial"] == "SN-1" and info["manufacturer"] == "Acme"
    assert info["cal_date"] == "2026-01-01" and info["asset_tag"] is None

    meta.set("SN-1", {"manufacturer": "Keithley", "asset_tag": "LAB-007",
                      "notes": "shared scope"})
    info2 = merge_device_info(d, meta.get("SN-1"))
    assert info2["manufacturer"] == "Keithley"               # user wins
    assert info2["serial"] == "SN-1"                         # descriptor kept (no override)
    assert info2["asset_tag"] == "LAB-007" and info2["notes"] == "shared scope"


def test_device_meta_persists_and_clears(tmp_path):
    from ferrodac.core.devicemeta import DeviceMeta
    p = str(tmp_path / "dm.json")
    DeviceMeta(p).set("SN-1", {"asset_tag": "X1", "junk": "ignored"})
    again = DeviceMeta(p)
    assert again.get("SN-1") == {"asset_tag": "X1"}          # only known fields, persisted
    again.set("SN-1", {})                                    # cleared → dropped
    assert DeviceMeta(p).get("SN-1") == {}
