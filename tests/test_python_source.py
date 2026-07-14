"""PYTHON SOURCE driver: user Python runs on the poll thread, each returned value
becomes a Reading. Headless / Qt-free — build the device, drive _read cycles, assert
values; a bad script -> NaN + last_error; a code edit hot-swaps the sources.
The defs file is redirected to a tmp dir so the suite never touches ~/.config."""
import pytest

import ferrodac.devices.python_source as mod
from ferrodac.core.device import RateMode
from ferrodac.devices.python_source import PythonSourceDevice


DET = '''
SOURCES = [
    {"id": "a", "name": "A", "unit": "V"},
    {"id": "b", "name": "B"},
]

def setup(ctx):
    ctx.state["k"] = 10

def poll(ctx):
    return {"a": 1.5, "b": ctx.state["k"]}
'''

BAD_RUNTIME = '''
SOURCES = [{"id": "a", "name": "A"}]
def poll(ctx):
    raise RuntimeError("boom")
'''

FLAKY = '''
SOURCES = [{"id": "a", "name": "A"}]
def poll(ctx):
    ctx.state["n"] = ctx.state.get("n", 0) + 1
    if ctx.state["n"] == 1:
        raise RuntimeError("first fails")
    return {"a": 7.0}
'''


@pytest.fixture(autouse=True)
def _isolate_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def _dev(code):
    d = PythonSourceDevice.new()
    d.set_option("code", code)             # -> _on_option -> _recompile + save_def
    return d


def _next_cycle(d):
    d._cache_t = -1e9                      # force the per-cycle cache to re-poll


# -- identity / defaults --------------------------------------------------------
def test_new_defaults_and_starter_sources():
    d = PythonSourceDevice.new()
    desc = d.describe()
    assert desc.driver == "python_source"
    assert desc.instance_id.startswith("python:")
    assert desc.hardware_id == desc.instance_id           # STABLE fingerprint
    assert d.discoverable is False and d.async_config is True
    assert [s.id for s in desc.sources] == ["sine", "ramp"]
    assert desc.rate.mode == RateMode.SETTABLE
    assert (desc.rate.default_hz, desc.rate.min_hz, desc.rate.max_hz) == (1.0, 0.1, 20.0)
    assert desc.options[0].key == "code" and desc.options[0].kind == "text"


def test_starter_template_compiles_and_polls():
    d = PythonSourceDevice.new()
    srcs = {s.id: s for s in d.describe().sources}
    (sine, _), (ramp, _) = d._read(srcs["sine"]), d._read(srcs["ramp"])
    assert -1.0 <= sine <= 1.0
    assert ramp == 1.0                                    # first cycle: n == 1


# -- deterministic values -------------------------------------------------------
def test_deterministic_poll_values():
    d = _dev(DET)
    srcs = {s.id: s for s in d.describe().sources}
    assert set(srcs) == {"a", "b"}
    assert d._read(srcs["a"]) == (1.5, 0)
    assert d._read(srcs["b"]) == (10.0, 0)               # value came from setup()'s state


def test_scalar_poll_infers_single_value_source():
    d = _dev("def poll(ctx):\n    return 42\n")
    srcs = d.describe().sources
    assert [s.id for s in srcs] == ["value"]
    assert d._read(srcs[0]) == (42.0, 0)


def test_poll_runs_once_per_cycle():
    code = ('SOURCES=[{"id":"a","name":"A"},{"id":"b","name":"B"}]\n'
            'def poll(ctx):\n'
            '    ctx.state["n"] = ctx.state.get("n",0)+1\n'
            '    return {"a": ctx.state["n"], "b": ctx.state["n"]}\n')
    d = _dev(code)
    srcs = d.describe().sources
    a = d._read(srcs[0])[0]
    b = d._read(srcs[1])[0]
    assert a == b                                         # one poll shared by both channels
    _next_cycle(d)
    assert d._read(srcs[0])[0] == a + 1                  # next cycle -> one more poll


# -- errors ---------------------------------------------------------------------
def test_runtime_poll_error_gives_nan_and_last_error():
    d = _dev(BAD_RUNTIME)
    src = d.describe().sources[0]
    val, status = d._read(src)
    assert status == 1 and val != val                    # NaN
    assert "boom" in (d.describe().last_error or "")


def test_error_then_recover_clears_last_error():
    d = _dev(FLAKY)
    src = d.describe().sources[0]
    val, status = d._read(src)
    assert status == 1 and val != val and d.describe().last_error
    _next_cycle(d)
    assert d._read(src) == (7.0, 0)
    assert d.describe().last_error is None                # error->ok edge cleared it


def test_compile_error_keeps_previous_sources():
    d = _dev(DET)                                         # good: sources a, b
    d.set_option("code", "def poll(ctx):\n    return {  # unterminated\n")
    desc = d.describe()
    assert desc.last_error is not None
    assert [s.id for s in desc.sources] == ["a", "b"]     # broken edit did NOT blank it


def test_unmatched_key_is_nan():
    d = _dev('SOURCES=[{"id":"a","name":"A"}]\ndef poll(ctx):\n    return {"zzz": 1}\n')
    val, status = d._read(d.describe().sources[0])
    assert status == 1 and val != val


# -- hot reload -----------------------------------------------------------------
def test_hot_reload_changes_sources():
    d = _dev(DET)
    assert [s.id for s in d.describe().sources] == ["a", "b"]
    d.set_option("code", "SOURCES=[{'id':'x','name':'X'}]\n"
                         "def poll(ctx):\n    return {'x': 3.0}\n")
    srcs = d.describe().sources
    assert [s.id for s in srcs] == ["x"]
    _next_cycle(d)
    assert d._read(srcs[0]) == (3.0, 0)


# -- persistence ----------------------------------------------------------------
def test_persistence_roundtrip_and_delete():
    d = _dev(DET)
    assert mod.load_defs().get(d.instance_id) == DET     # _on_option persisted the edit
    d2 = PythonSourceDevice.restore(d.instance_id)        # rehydrate from disk
    assert [s.id for s in d2.describe().sources] == ["a", "b"]
    assert d2._read(d2.describe().sources[0]) == (1.5, 0)
    mod.delete_def(d.instance_id)
    assert d.instance_id not in mod.load_defs()


def test_restore_all_rehydrates_active_set():
    a = _dev(DET)
    b = PythonSourceDevice.new()
    b.set_option("code", "def poll(ctx):\n    return 5\n")
    restored = {d.instance_id: d for d in PythonSourceDevice.restore_all()}
    assert a.instance_id in restored and b.instance_id in restored


# -- check() --------------------------------------------------------------------
def test_check_ok_reports_source_count():
    r = _dev(DET).check()
    assert r.ok and r.sources == 2 and "2 source" in r.summary


def test_check_reports_compile_error():
    d = PythonSourceDevice.new()
    d.set_option("code", "def poll(ctx):\n    return }{\n")
    r = d.check()
    assert not r.ok and "error" in r.summary.lower()


def test_check_reports_raising_poll():
    r = _dev(BAD_RUNTIME).check()
    assert not r.ok and "boom" in r.summary
