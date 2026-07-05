# ChartPanel multi-axis — decision analysis (2026-07-06)

6-agent workflow (map → 4 lenses reproducing every failure headlessly → synthesis). The
multi-axis chart handling regressed **3×** on the same subsystem; this decides the path.
**Recommendation: Option B — one Y axis per chart + an authoritative dimensional routing gate.**

## Root cause — 4 bugs → 5 roots → 1 structural cause

All the axis bugs collapse to the **asymmetry of the primary slot**: the built-in left axis +
main viewbox is a *special* slot (curves made with `plot.plot()`, cannot be hidden, uniquely
drives the shared-X auto-range every secondary is `setXLink`'d to, and can't be re-homed without
moving a curve *and its σ-band FillBetweenItem* between two creation regimes). Every regression
was a lifecycle transition (reload / remove / late-unit) that had to cross this asymmetry.

| Root | What | Class |
|---|---|---|
| R1 | `add_source` early-returns on `key in _curves`, dropping a late real unit (`panels.py:672`) | local (patchable) |
| R2 | primary axis pinned to the *first* unit, never re-purposed; a `""` first bind owns the left axis forever | structural |
| R3 | emptied primary never GC'd; an empty primary viewbox → `enableAutoRange()` is a silent no-op → the whole chart's shared X freezes at `[0,1]` ("auto-range dies") | structural |
| R4 | secondary viewboxes get degenerate geometry when allocated *before* layout (reload binds pre-layout); only the `showEvent` re-wire corrects it | semi-local (partly patched) |
| R5 | slots / scene viewboxes / stacked AxisItems are only ever added, never removed — a leak | structural |

- **Bug #1** (reload: no secondary axis, units wrong) = R1 + R2 — historic port binds `unit=""`
  first; the reconnect's real unit is discarded by R1.
- **Bug #2** (add-then-remove → unitless primary + two secondaries, auto-range dead) = R2 + R3.
  The dimensionless first source is *legitimate* → **not** fixable by resolving units at bind time.
- **Bug #3** (reverted "re-home on late unit" blanked charts) = R4 amplified — re-homing across
  viewboxes pre-layout is what pyqtgraph punishes hardest.

Steady state is sound (same-dimension mbar+Torr conversion works; 3-dimension fresh charts work);
it only breaks on lifecycle transitions. R1/R4 are cheap; R2/R3/R5 are fixable only by rewriting
the exact lifecycle that regressed 3×.

## Recommendation: Option B (one axis + routing gate)

- **Maintainability (deciding factor).** pyqtgraph 0.14.0 has **no** built-in multi-axis — the
  linked-viewbox pattern is literally its unmaintained `MultiplePlotAxes.py` example (author's
  "will eventually become built-in" comment, still unbuilt ~5y on; the upstream PR never merged).
  Option A means owning a mini-framework the library declined to build, **forever** — every
  robustness property (geometry sync, autoBtn-only-main, empty-vb autorange trap, `setAutoVisible`
  trap, scene GC, extra-vb mouse/export) hand-maintained with zero upstream support. B collapses
  onto the single built-in ViewBox path pyqtgraph fully supports.
- **Robustness.** B makes R2/R3/R4/R5 *cease to exist* (not "get fixed"): no secondary viewboxes
  → no geometry sync, no empty-primary/shared-X trap, no re-home, no scene leak, no per-key band
  viewbox. R1 degenerates to a pure in-place data update.
- **Feature lost:** only single-pane cross-dimension dual-Y (pressure-log + temp-linear on one time
  axis). **Kept:** same-dimension multi-unit overlay (mbar+Torr, K+°C) — the *common* vacuum
  overlay. Mitigation for the lost one: route the second dimension to a sibling chart (+ optional
  cross-panel X-link — chart panels aren't X-linked today, only the Timeline's internal charts).
- **Why not the middle path** (keep multi-axis, just fix the data model): those two fixes *are* the
  viewbox-moving / axis-reassigning surfaces that caused regression #3. B buys the same fixes and
  makes them trivial by removing the surface.

## Option B package

**Deletes** (~90-120 lines net): `_AxisSlot`; `sigResized→_sync_axis_geometry` +
`autoBtn→_autorange_extra_axes` wiring; `_alloc_slot` secondary branch; `_sync_axis_geometry`;
`_autorange_extra_axes`; `showEvent`/`_rewire_axes`; the per-extra-axis log fan-out in
`apply_config`; the `remove_source` hide-secondary branch; the `clear_history` extra-axes loop; the
per-key band viewbox (bands live on the main viewbox). Keeps one `display_unit`/dimension; `_conv`
(mbar→Torr) unchanged.

**Routing gate (authoritative — a menu-only gate is insufficient).** `compatible_sinks`
(`workspace.py:919`) gates only the menu; the real mutation `_apply_route→add_source` is reached by
two paths that bypass it (`import_layout` replay `712-714`, device-reconnect rebind `802-804`). So:
(1) when a chart adopts its first dimensioned source, mirror that dimension's canonical unit onto
its `SinkPort.unit` (reset to `""` when empty) — grays out incompatible sources in the menu with
zero other changes; (2) `add_source` returns a bool (adopted/same-dimension True; different False),
`_apply_route` drops the route on False (~+10 lines); (3) treat `""` as "no dimension yet" so a
later real unit can adopt the axis (the R1 fix).

**Migration (must plan — the one risk).** Existing layouts can have two dimensions on one chart;
after the switch the second is refused on restore and the source *silently vanishes*. Make
"refused-on-restore → auto-spawn a sibling chart for that dimension (+ X-link) + notice" a
first-class, tested part of the change. This is the only way B can surprise a user.

## Near-term stopgap (do regardless of A/B) — takes TWO fixes, not one

- **(i) `add_source` idempotent on unit change** — existing key, differing non-empty unit → update
  `_meta`, recompute `_conv`, relabel, reset log default. Kills **bug #1 single-dimension**. (Under
  multi-axis it's safe only for the single-dimension case — two different real dimensions arriving
  would trigger the pre-layout re-home that caused regression #3; under B the gate refuses the 2nd
  dimension so it's always trivial.)
- **(ii) never let an empty/dimensionless slot keep the left axis** — on `remove_source`, if the
  primary empties while another slot has data, promote a populated dimension to primary. Geometry-
  free; kills the auto-range-death class (bug #2). Fix (i) does nothing for bug #2 (its first source
  is legitimately unitless — nothing to "resolve").

"Resolve unit at bind time" alone is **overstated**: it covers part of #1 and none of #2. The real
stopper is the pair (i)+(ii), both trivial+safe under B.

## Effort & risk

- **Option B full package: ~1-2 focused days** (panel simplification ≈ deletion ½d; gate ½d;
  (i)+(ii) a few hours; migration ½d). Add headless regression tests reproducing bugs #1/#2/#3 (the
  lens probes are templates) so this can't regress a 4th time — highest-leverage add.
- **Option A strongest form** (declarative "rebuild all axes from `{key→unit}`, idempotent, layout-
  deferred") is a ~150-250 line rewrite that *still* owns the whole pyqtgraph multi-viewbox
  mechanism forever, *still* must be pre-layout-safe (a pre-layout teardown/rebuild re-introduces
  regression #3), *still* relocates σ-bands across viewboxes. More work, more permanent surface,
  more risk — to preserve a power-user overlay that has a decent mitigation.
- **The one risk (B):** the migration/silent-drop path — make refused-on-restore auto-spawn a
  sibling + notice, tested. Everything else about B is subtractive and low-risk.

*(Caveat: the pro-A "robust" lens errored and produced no analysis, so A was never argued by its
own advocate; the synthesis reconstructed A's strongest form and still chose B.)*
