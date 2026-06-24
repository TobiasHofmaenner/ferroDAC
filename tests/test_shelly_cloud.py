"""Shelly Cloud account driver: always-discoverable, GUI-configurable (server +
key), then its sensors stream as channels named by DEVICE name + a parenthetical
room. Enumeration reads /interface/device/list (name + room_id + an embedded status
snapshot) and /interface/room/list (room names); live values come from one bulk
/device/all_status. No network — the cloud calls are stubbed."""
import ferrodac.devices.shelly_cloud as mod
from ferrodac.core.device import Source
from ferrodac.devices.shelly_cloud import ShellyCloud

# /interface/device/list — devices keyed by id, each with name/room_id/category + ss.status
_LIST = {"devices": {
    "aa01": {"category": "sensor", "name": "Sensor A", "room_id": 3,
             "ss": {"status": {"temperature:0": {"tC": 22.5}, "humidity:0": {"rh": 41}}}},
    "bb02": {"category": "sensor", "name": "", "room_id": 9,          # unnamed → id fallback
             "ss": {"status": {"tmp": {"value": 4.1}}}},              # Gen1 temp-only
    "cc03": {"category": "relay", "name": "Plug",                     # relay → no channels
             "ss": {"status": {"switch:0": {"output": True}}}},
}}
# /interface/room/list — room id -> name (3 known; 9 absent → no parenthetical)
_ROOMS = {"rooms": {"3": {"id": 3, "name": "Assembly Hall"}}}
# /device/all_status — live values for polling / _read
_BULK = {"devices_status": {
    "aa01": {"temperature:0": {"tC": 22.5}, "humidity:0": {"rh": 41}},
    "bb02": {"tmp": {"value": 4.1}},
}}


def _stub(monkeypatch, sink=None):
    monkeypatch.setattr(mod.time, "sleep", lambda *a, **k: None)      # don't pause the suite
    def fake_get(server, path, params, **_):
        if sink is not None:
            sink.append((server, path))
        if "room" in path:
            return _ROOMS
        if "device/list" in path:
            return _LIST
        return _BULK                                                 # /device/all_status
    monkeypatch.setattr(mod, "_get", fake_get)


def test_account_always_offered_and_configurable():
    d = ShellyCloud.discover()[0].describe()
    assert d.name == "Shelly Cloud" and d.instance_id == "shelly:cloud"
    assert d.sources == []                                  # no channels until configured
    assert {o.key: o.kind for o in d.options} == {"server": "text", "auth_key": "secret"}


def test_unconfigured_makes_no_call(monkeypatch):
    acc = ShellyCloud.discover()[0]
    monkeypatch.setattr(mod, "_get",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    acc._refresh_sensors()                                  # no creds → no call, no crash
    assert acc.describe().sources == []


def test_channels_use_device_name_and_room_parenthetical(monkeypatch):
    acc = ShellyCloud.discover()[0]
    acc._option_values["server"] = "https://x.shelly.cloud/"     # full URL → normalized
    acc._option_values["auth_key"] = "k"
    calls = []
    _stub(monkeypatch, calls)
    acc._on_option("auth_key", "")                          # enumerate (what set_option does)
    names = [s.name for s in acc.describe().sources]
    assert names == [
        "Sensor A · Temperature (Assembly Hall)",           # device name + room
        "Sensor A · Humidity (Assembly Hall)",
        "Shelly bb02 · Temperature",                        # unnamed → id; room 9 unknown → no ()
    ]
    paths = {p for _s, p in calls}
    assert paths == {"/interface/room/list", "/interface/device/list"}   # scheme-stripped host
    assert all(s == "x.shelly.cloud" for s, _p in calls)


def test_read_maps_channel_gen3_and_gen1(monkeypatch):
    acc = ShellyCloud.discover()[0]
    acc._option_values["server"] = "x"; acc._option_values["auth_key"] = "k"
    _stub(monkeypatch)
    acc._refresh_sensors()
    src = {s.id: s for s in acc.describe().sources}
    assert acc._read(src["aa01_temperature_0"]) == (22.5, 0)      # Gen3 tC
    assert acc._read(src["aa01_humidity_0"]) == (41.0, 0)         # Gen3 rh
    assert acc._read(src["bb02_tmp"]) == (4.1, 0)                 # Gen1 value
    val, status = acc._read(Source(id="ghost", name="x", unit=""))
    assert status == 1 and val != val                            # unknown channel → NaN, error


def test_one_bulk_status_call_per_cycle(monkeypatch):
    acc = ShellyCloud.discover()[0]
    acc._option_values["server"] = "x"; acc._option_values["auth_key"] = "k"
    acc._rate_hz = 1 / 60
    calls = []
    _stub(monkeypatch, calls)
    acc._refresh_sensors()
    calls.clear()                                           # ignore enumeration's list/room calls
    for s in acc.describe().sources:                        # every channel reads from the cache
        acc._read(s)
    assert [p for _s, p in calls] == ["/device/all_status"]   # ONE status call for the cycle


# -- check(): the config GUI's "Check connection" diagnostic --------------------
def test_check_unconfigured():
    r = ShellyCloud.discover()[0].check()
    assert not r.ok and "server" in r.summary.lower()      # tells you to fill the fields


def test_check_success_reports_counts_and_applies(monkeypatch):
    acc = ShellyCloud.discover()[0]
    acc._option_values["server"] = "x"; acc._option_values["auth_key"] = "k"
    _stub(monkeypatch)
    r = acc.check()
    assert r.ok and r.sources == 3                         # 2 on aa01 + 1 on bb02
    assert "3 channel" in r.summary and "2 sensor" in r.summary
    assert len(acc.describe().sources) == 3                # a successful check also populates


def test_check_reports_auth_failure(monkeypatch):
    acc = ShellyCloud.discover()[0]
    acc._option_values["server"] = "x"; acc._option_values["auth_key"] = "bad"
    monkeypatch.setattr(mod.time, "sleep", lambda *a, **k: None)
    def fake_get(server, path, params, **_):
        if "room" in path:
            return _ROOMS                                  # best-effort room call is fine
        raise Exception("HTTP Error 401: Unauthorized")    # the authoritative device/list call
    monkeypatch.setattr(mod, "_get", fake_get)
    r = acc.check()
    assert not r.ok and "uthenticat" in r.summary          # "Authentication failed…"
    assert acc.describe().sources == []                    # a failed check changes nothing


def test_check_reports_no_sensors(monkeypatch):
    acc = ShellyCloud.discover()[0]
    acc._option_values["server"] = "x"; acc._option_values["auth_key"] = "k"
    monkeypatch.setattr(mod.time, "sleep", lambda *a, **k: None)
    relays = {"devices": {"cc03": {"category": "relay", "name": "Plug",
                                   "ss": {"status": {"switch:0": {"output": True}}}}}}
    monkeypatch.setattr(mod, "_get",
                        lambda s, p, q, **_: _ROOMS if "room" in p else relays)
    r = acc.check()
    assert not r.ok and "no temperature" in r.summary.lower()


def test_basedevice_check_default_counts_and_surfaces_errors():
    from ferrodac.core.base import BaseDevice
    from ferrodac.core.device import Interface

    class Dev(BaseDevice):
        driver = "t"
        def __init__(self, boom=False):
            super().__init__("t:1", "T", Interface(kind="sim"),
                             sources=[Source(id="a", name="A"), Source(id="b", name="B")])
            self._boom = boom
        def _connect(self):
            if self._boom:
                raise RuntimeError("nope")

    ok = Dev().check()
    assert ok.ok and ok.sources == 2 and "2 sources" in ok.summary
    bad = Dev(boom=True).check()
    assert not bad.ok and "nope" in bad.summary
