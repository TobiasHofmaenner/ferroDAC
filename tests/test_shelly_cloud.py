"""Shelly Cloud account driver: always-discoverable, GUI-configurable (server +
key), then its sensors stream as channels via ONE bulk /device/all_status call,
named from /interface/room/list. No network — the cloud calls are stubbed."""
import ferrodac.devices.shelly_cloud as mod
from ferrodac.core.device import Source
from ferrodac.devices.shelly_cloud import ShellyCloud

# /device/all_status data payload, keyed by device id:
_BULK = {"devices_status": {
    "aa01": {"temperature:0": {"tC": 22.5}, "humidity:0": {"rh": 41}},   # Gen3 H&T
    "bb02": {"tmp": {"value": 4.1}},                                      # Gen1 temp-only
    "cc03": {"switch:0": {"output": True}},                              # relay → no channels
}}
# /interface/room/list — friendly names mapped to each room's main_sensor:
_ROOMS = {"rooms": {"1": {"name": "Lab", "main_sensor": "aa01"}}}        # bb02 has no room


def _stub(monkeypatch, sink=None):
    def fake_get(server, path, params, **_):
        if sink is not None:
            sink.append((server, path))
        return _ROOMS if "room" in path else _BULK
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


def test_config_enumerates_names_and_strips_scheme(monkeypatch):
    acc = ShellyCloud.discover()[0]
    acc._option_values["server"] = "https://x.shelly.cloud/"     # full URL → normalized
    acc._option_values["auth_key"] = "k"
    calls = []
    _stub(monkeypatch, calls)
    acc._on_option("auth_key", "")                          # enumerate (what set_option does)
    names = [s.name for s in acc.describe().sources]
    assert names == ["Lab · Temperature", "Lab · Humidity",         # room name from room/list
                     "Shelly bb02 · Temperature"]                   # no room → id fallback
    assert calls[0] == ("x.shelly.cloud", "/device/all_status")     # scheme stripped, bulk call
    assert any(p == "/interface/room/list" for _s, p in calls)      # names fetched too


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
    status_calls = []

    def fake_get(server, path, params, **_):
        if path == "/device/all_status":
            status_calls.append(1)
        return _ROOMS if "room" in path else _BULK
    monkeypatch.setattr(mod, "_get", fake_get)
    acc._refresh_sensors()                                  # 1 bulk status fetch (cached)
    for s in acc.describe().sources:                        # every channel reads from the cache
        acc._read(s)
    assert len(status_calls) == 1                           # ONE status call for the whole cycle
