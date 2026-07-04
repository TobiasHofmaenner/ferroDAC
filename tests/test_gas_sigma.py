"""The gas analyzer publishes its bootstrap fit uncertainty INLINE (DESIGN §19.0 / X3b):
process() carries a reserved "_sigma" entry mapping each partial-pressure output to its
1σ, so a chart can draw a band. Qt-free (the fit math itself is reviewed separately)."""
import numpy as np

from ferrodac.analysis.gas import GasAnalyzer
from ferrodac.analysis.library import DEFAULT_GASES, LIBRARY
from ferrodac.core.trace import Trace


def _spectrum(gas_names, scale=100.0):
    x = np.arange(1.0, 50.0)
    y = np.zeros_like(x)
    for n in gas_names:
        for m, frac in LIBRARY[n].norm_pattern.items():
            y[np.abs(x - m) <= 0.5] += frac * scale
    return Trace(x, y, y_unit="mbar")


def test_gas_publishes_sigma_inline_when_mc_on():
    names = list(DEFAULT_GASES)[:3]
    ga = GasAnalyzer("g1", "rga/spec", gases=names, mc=32)
    out = ga.process(_spectrum(names))
    assert "_sigma" in out
    for n in names:                             # every partial pressure carries a 1σ
        k = f"gas/g1/{n}"
        assert k in out and k in out["_sigma"]
        assert np.isfinite(out["_sigma"][k]) and out["_sigma"][k] >= 0.0
    assert "_sigma" not in [p.key for p in ga.outputs()]   # not itself a port


def test_gas_no_sigma_without_mc():
    ga = GasAnalyzer("g1", "rga/spec", gases=list(DEFAULT_GASES)[:2], mc=0)  # single fit
    out = ga.process(_spectrum(list(DEFAULT_GASES)[:2]))
    assert "_sigma" not in out                  # no bootstrap → nothing to publish
