"""Shelly Cloud account driver: always-discoverable, GUI-configurable (server +
key), then its H&T sensors stream as channels. No network — the cloud calls are
stubbed."""
from ferrodac.devices.shelly_cloud import ShellyCloud


def _account():
    return ShellyCloud.discover()[0]


def test_account_is_always_offered_and_configurable():
    acc = _account()
    d = acc.describe()
    assert d.name == "Shelly Cloud" and d.instance_id == "shelly:cloud"
    assert d.sources == []                                  # no channels until configured
    kinds = {o.key: o.kind for o in d.options}
    assert kinds == {"server": "text", "auth_key": "secret"}   # GUI text + masked


def test_unconfigured_does_not_enumerate():
    acc = _account()
    acc._list_sensors = staticmethod(lambda *_: (_ for _ in ()).throw(AssertionError))
    acc._refresh_sensors()                                  # missing creds → no call, no crash
    assert acc.describe().sources == []


def test_config_enumerates_channels():
    acc = _account()
    acc._option_values["server"] = "s.shelly.cloud"
    acc._option_values["auth_key"] = "k"
    acc._list_sensors = staticmethod(
        lambda *_: [{"id": "aa01", "name": "Lab H&T"}, {"id": "bb02", "name": "Fridge"}])
    acc._on_option("auth_key", "k")                         # what set_option triggers
    srcs = acc.describe().sources
    assert [s.name for s in srcs] == [
        "Lab H&T · Temperature", "Lab H&T · Humidity",
        "Fridge · Temperature", "Fridge · Humidity"]
    assert [s.unit for s in srcs] == ["°C", "% RH", "°C", "% RH"]


def test_read_maps_channel_to_sensor_and_metric_gen1_and_gen3():
    acc = _account()
    acc._chan = {"aa01_temperature": ("aa01", "temperature"),
                 "bb02_humidity": ("bb02", "humidity")}
    acc._fetch_status = lambda sid: (
        {"temperature:0": {"tC": 22.5}} if sid == "aa01"      # Gen3
        else {"hum": {"value": 80}})                          # Gen1
    from ferrodac.core.device import Source
    assert acc._read(Source(id="aa01_temperature", name="t", unit="°C")) == (22.5, 0)
    assert acc._read(Source(id="bb02_humidity", name="h", unit="%")) == (80.0, 0)
    val, status = acc._read(Source(id="ghost", name="x", unit=""))
    assert status == 1 and val != val                      # unknown channel → NaN, error


def test_fetch_status_caches_per_sensor_within_a_cycle():
    acc = _account()
    acc._option_values["server"] = "s"
    acc._option_values["auth_key"] = "k"
    acc._rate_hz = 1 / 60                                   # 60 s cycle
    calls = []
    import ferrodac.devices.shelly_cloud as mod
    mod._post = lambda server, path, payload: (calls.append(payload["id"])
                                               or {"device_status": {"tmp": {"value": 1}}})
    acc._fetch_status("aa01")
    acc._fetch_status("aa01")                              # same cycle → cached, no 2nd call
    assert calls == ["aa01"]
