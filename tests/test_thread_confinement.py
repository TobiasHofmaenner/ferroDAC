"""Cross-thread confinement regressions (concurrency-audit downgraded findings).

- tag.list must reach the GUI-confined MarkerModel via the gui() marshal
  (GuiBridge.post_and_wait), not directly on the localapi worker thread.
- DeviceManager catalog reads that run on hub-agent/connector worker threads must
  iterate an ATOMIC snapshot of _active/_available, so a concurrent GUI-thread
  discovery merge can't raise RuntimeError('dictionary changed size during
  iteration') into a remote command Ack.
"""

import types

import pytest

from ferrodac.core.manager import DeviceManager


# -- tag.list rides the gui() wrapper ----------------------------------------
@pytest.mark.ui
def test_tag_list_rides_the_gui_wrapper(control_surface):
    from ferrodac.ui.appcontrol import build_control_surface

    w, s = control_surface
    # roundtrip through the fixture's surface: add → list contains it
    mid = s.dispatch("tag.add", {"label": "confined", "comment": "c"},
                     scope="control")["id"]
    listing = s.dispatch("tag.list", scope="read")
    assert any(m["id"] == mid for m in listing)

    # rebuild the surface against a RECORDING bridge shim: the query must marshal
    # through post_and_wait (the gui() wrapper), never touch MarkerModel directly.
    marshalled = []
    real = w._gui_bridge
    w._gui_bridge = types.SimpleNamespace(
        post_and_wait=lambda fn, **kw: (marshalled.append(1), fn())[1])
    try:
        s2 = build_control_surface(w)
    finally:
        w._gui_bridge = real                    # window keeps the real bridge

    out = s2.dispatch("tag.list", scope="read")
    assert marshalled, "tag.list bypassed GuiBridge.post_and_wait (gui() wrapper)"
    assert any(m["id"] == mid for m in out)


# -- DeviceManager snapshot iteration ----------------------------------------
# Devices whose attribute access MUTATES the owning dict — a deterministic
# stand-in for the GUI-thread discovery merge landing between two iteration
# steps of a worker-thread catalog read (no real threads needed).
class _UuidMutator:
    def __init__(self, uuid, target):
        self._u, self._target = uuid, target

    @property
    def uuid(self):
        self._target["injected-" + self._u] = object()   # the 'discovery merge'
        return self._u


class _FingerprintMutator:
    def __init__(self, fp, target):
        self._fp, self._target = fp, target

    @property
    def fingerprint(self):
        self._target["injected-" + self._fp] = object()
        return self._fp


class _DescribeMutator:
    def __init__(self, name, target):
        self._name, self._target = name, target

    def describe(self):
        self._target["injected-" + self._name] = object()
        return self._name


def test_the_mutator_actually_trips_live_dict_iteration():
    """Harness sanity: without a snapshot this pattern DOES raise — so the tests
    below would catch a regression back to live-dict iteration."""
    d = {}
    d["a"] = _UuidMutator("u0", d)
    d["b"] = _UuidMutator("u1", d)
    with pytest.raises(RuntimeError):
        for _k, v in d.items():
            _ = v.uuid


def _mgr(active=None, available=None, registry=None):
    """Duck-typed self for the (pure-Python) catalog methods — no QObject init."""
    return types.SimpleNamespace(_active=active or {}, _available=available or {},
                                 _registry=registry)


def test_instance_for_uuid_iterates_a_snapshot():
    m = _mgr()
    for i in range(4):
        m._active[f"d{i}"] = _UuidMutator(f"u{i}", m._active)
    assert DeviceManager.instance_for_uuid(m, "u3") == "d3"
    assert DeviceManager.instance_for_uuid(m, "missing") is None


def test_available_for_uuid_iterates_a_snapshot():
    reg = types.SimpleNamespace(fingerprint_for=lambda uuid: "fp2")
    m = _mgr(registry=reg)
    for i in range(4):
        m._available[f"d{i}"] = _FingerprintMutator(f"fp{i}", m._available)
    assert DeviceManager.available_for_uuid(m, "whatever") == "d2"


def test_descriptor_lists_iterate_a_snapshot():
    m = _mgr()
    for i in range(4):
        m._active[f"a{i}"] = _DescribeMutator(f"A{i}", m._active)
        m._available[f"v{i}"] = _DescribeMutator(f"V{i}", m._available)
    assert DeviceManager.active_descriptors(m) == ["A0", "A1", "A2", "A3"]
    assert DeviceManager.available_descriptors(m) == ["V0", "V1", "V2", "V3"]


def test_active_devices_returns_an_atomic_copy():
    m = _mgr(active={"a": object(), "b": object()})
    out = DeviceManager.active_devices(m)
    assert out == list(m._active.values())
    m._active["c"] = object()
    assert len(out) == 2                        # a copy, not a live view
