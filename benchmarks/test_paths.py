"""Headless regression benchmarks (pytest-benchmark) over the data-plane paths.

Run:  make bench   (or  uv run pytest benchmarks/ --benchmark-autosave)
Compare against a saved baseline:  uv run pytest benchmarks/ --benchmark-compare

NOT collected by the normal suite (pyproject testpaths=["tests"]) and deliberately
kept OUT of the CI gate — CI-runner noise makes absolute numbers unreliable. Use
these locally / on a stable box for before/after work; they reuse the SAME seeders
the in-app Benchmark dialog uses, so headless and in-app numbers are comparable.
"""

import os
import shutil
import tempfile

import pytest

from ferrodac.bench import SOURCES, seed_scalar, seed_trace
from ferrodac.core.bus import Bus
from ferrodac.core.history import HistoryBuffer
from ferrodac.store import PlaybackSource, RamTier, Resolver

_SCALAR = [10_000, 100_000]      # add 1_000_000 for a heavy run
_TRACE = [200, 1_000]


@pytest.fixture(scope="module")
def scratch():
    d = tempfile.mkdtemp(prefix="fd-bench-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _scalar(scratch, n, tag):
    st, a, b = seed_scalar(os.path.join(scratch, f"{tag}{n}.zarr"), n)
    return Resolver([RamTier(HistoryBuffer()), st]), a, b


@pytest.mark.parametrize("n", _SCALAR)
def test_query_decimated(benchmark, scratch, n):
    res, a, b = _scalar(scratch, n, "q")
    benchmark(lambda: [res.query(s, a, b, 2000) for s in SOURCES])


@pytest.mark.parametrize("n", _SCALAR)
def test_read_raw_fullres(benchmark, scratch, n):
    res, a, b = _scalar(scratch, n, "rr")
    benchmark(lambda: [res.read_raw(s, a, b) for s in SOURCES])


@pytest.mark.parametrize("n", _SCALAR)
def test_read_window_restream(benchmark, scratch, n):
    res, a, b = _scalar(scratch, n, "rw")
    play = PlaybackSource(res, Bus())
    benchmark(lambda: play.read_window(list(SOURCES), a, b))


@pytest.mark.parametrize("n", _TRACE)
def test_read_raw_trace(benchmark, scratch, n):
    st, a, b = seed_trace(os.path.join(scratch, f"t{n}.zarr"), n)
    res = Resolver([RamTier(HistoryBuffer()), st])
    benchmark(lambda: res.read_raw_trace("rga/spec", a, b))
