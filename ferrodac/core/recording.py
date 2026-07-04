"""RecordingController — the recording lifecycle as a testable unit (DESIGN §4.1 L2).

A recording is just a marked SPAN over the always-on durable store: Start opens a
REC marker (the data is already being persisted), Stop closes it and auto-exports
the span as a self-describing bundle. This holds the lifecycle *policy* — start/stop,
finalise-and-export, crash recovery, and the crashed-recording end-time computation —
which used to live inline in the Qt MainWindow shell (the audit: "recording
finalisation embedded in the Qt shell, untestable headless").

It operates on INJECTED collaborators (a marker model, the tiered resolver, the
store writer, an export runner, and small callbacks), so it imports no Qt and is
unit-testable with fakes. The MainWindow supplies the real (Qt) collaborators + a
status-line callback and keeps only the thin view wiring.
"""

from __future__ import annotations

import os
import time

from .markers import RECORDING


class RecordingController:
    def __init__(self, *, markers, resolver, store_writer, run_export, runs_dir,
                 export_sources, on_status=None, on_saved=None, commit=None,
                 now=time.time):
        self._markers = markers            # MarkerModel: add/get/update/all
        self._resolver = resolver          # tiered resolver (may be None = no store)
        self._store_writer = store_writer  # for a flush before export (may be None)
        self._run_export = run_export      # (dest, sources, t0, t1, *, flush, exclusive,
        #                                     on_ok, on_fail) — runs the export off-thread
        self._runs_dir = runs_dir          # () -> the active project's reports dir
        self._export_sources = export_sources   # () -> [source keys] to export
        self._on_status = on_status or (lambda msg, timeout=0: None)
        self._on_saved = on_saved or (lambda mid, dest, n: None)  # refresh UI on save
        self._commit = commit or (lambda msg: None)               # §8.2 boundary commit
        self._now = now
        self._open_mid = None              # the open REC marker id, or None

    @property
    def recording(self) -> bool:
        return self._open_mid is not None

    @property
    def open_mid(self):
        return self._open_mid

    def toggle(self) -> str:
        """Start (open a REC marker) or Stop (close it + finalise+export). Returns
        'started' or 'stopped' so the view updates its Record button/label."""
        if self._open_mid is None:
            self._open_mid = self._markers.add(
                self._now(), kind=RECORDING, label="REC", comment="recording…")
            self._on_status("● Recording — persisting to the store")
            return "started"
        mid, self._open_mid = self._open_mid, None
        m = self._markers.get(mid)
        t0 = m.t if m else self._now()
        t1 = self._now()
        self._markers.update(mid, t_end=t1)        # close the region
        self.finalize(mid, t0, t1)
        return "stopped"

    def close_open_marker(self):
        """Close the open REC marker (t_end=now) WITHOUT exporting — for app
        shutdown, where the store writer's stop() flush is the durability. Returns
        the mid, or None if not recording."""
        mid, self._open_mid = self._open_mid, None
        if mid is not None:
            self._markers.update(mid, t_end=self._now())
        return mid

    def finalize(self, mid, t0, t1) -> None:
        """Materialise the span [t0,t1] as a bundle via the export runner (off the
        GUI thread). The durable Zarr store IS the crash-safe data, so a failed
        export never risks loss — the recording marker is kept either way."""
        if self._resolver is None:
            return
        sources = self._export_sources()
        dest = os.path.join(
            self._runs_dir(),
            "run_" + time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(t0)))

        def ok(man):
            n = len(man.get("sources", []))
            self._markers.update(mid, run_dir=dest, comment=f"{n} sources")
            self._on_saved(mid, dest, n)
            self._commit(f"Recorded {os.path.basename(dest)}")

        def fail(msg):
            self._on_status(f"Recording kept; export failed: {msg}", 8000)

        self._on_status("■ Saving recording in the background…", 4000)
        self._run_export(dest, sources, t0, t1, flush=True,
                         exclusive=f"export:{mid}", on_ok=ok, on_fail=fail)

    def recover_open(self) -> int:
        """A recording interrupted by a crash survives as an OPEN REC marker
        (t_end=None). Finalise each (t_end = the last data we have) + export.
        Returns how many were recovered."""
        open_recs = [m for m in self._markers.all()
                     if m.kind == RECORDING and m.t_end is None]
        for m in open_recs:
            t_end = self.last_data_time(m.t) or m.t
            self._markers.update(m.id, t_end=t_end)
            self.finalize(m.id, m.t, t_end)
        return len(open_recs)

    def last_data_time(self, t0):
        """Latest stored sample time in [t0, now] across the export sources (to
        finalise a crashed recording's end). None if nothing was stored."""
        if self._resolver is None:
            return None
        now = self._now()
        latest = None
        for key in self._export_sources():
            for a, b in self._resolver.coverage(key):
                if a <= now and b >= t0:
                    v = min(b, now)
                    latest = v if latest is None else max(latest, v)
        return latest
