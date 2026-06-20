"""Store-sync must bound each PushChunk so it can't exceed the gRPC message cap.

A trace row carries `m` bins, so chunking by a fixed ROW count let a wide-spectrum
epoch produce a >4 MiB message (the RESOURCE_EXHAUSTED the hub rejected). SyncEngine
now sizes the row step by a VALUE budget for traces.
"""

import numpy as np

from ferrodac.store.sync import SyncEngine


class _FakeStore:
    def __init__(self, rows, m):
        self.rows, self.m = rows, m

    def epoch_lengths(self):
        return {("src", "ep"): self.rows}

    def read_epoch(self, uuid, epoch, start, end):
        n = max(0, end - start)
        return {"dtype": "trace", "t": np.zeros(n),
                "y": np.zeros((n, self.m)), "x": np.zeros(self.m)}


class _RecordingTransport:
    def __init__(self):
        self.values = []          # values pushed per call
        self.rows = 0

    def state(self):
        return {}                 # remote has nothing → upload the whole epoch

    def push(self, source, epoch, chunk):
        arr = np.asarray(chunk["y"] if chunk["dtype"] == "trace" else chunk["v"])
        self.values.append(int(arr.size))
        self.rows += arr.shape[0]


def test_trace_push_is_value_bounded():
    rows, m = 50_000, 64                    # 50k scans × 64 bins = 3.2M values if unsplit
    tx = _RecordingTransport()
    eng = SyncEngine(_FakeStore(rows, m), tx, max_values=250_000)
    sent = eng.sync_once()

    assert sent == rows and tx.rows == rows            # everything uploaded, in order
    assert tx.values, "nothing pushed"
    assert max(tx.values) <= 250_000, "a push exceeded the value budget (could blow the cap)"
    # each push stays well under even the DEFAULT 4 MiB cap (8 bytes/double)
    assert max(tx.values) * 8 < 4 * 1024 * 1024


def test_scalar_push_uses_row_chunk():
    class _ScalarStore:
        def epoch_lengths(self):
            return {("s", "e"): 45_000}

        def read_epoch(self, uuid, epoch, start, end):
            n = max(0, end - start)
            return {"dtype": "scalar", "t": np.zeros(n), "v": np.zeros(n)}

    tx = _RecordingTransport()
    SyncEngine(_ScalarStore(), tx, chunk=20_000).sync_once()
    assert tx.rows == 45_000                           # 20k + 20k + 5k
