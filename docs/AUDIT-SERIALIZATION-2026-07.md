# ferroDAC Serialization — Architecture Assessment (2026-07-05)

Read-only synthesis of a 6-agent workflow (map → 4 lenses: layout / store / wire / unify →
synthesis), load-bearing claims re-verified against source. Line numbers are the write/read
call sites.

---

## 1. VERDICT

Serialization **has** been thought about — but only at the two tiers that were *designed*
(the proto wire and the σ-model registry), plus a clean plugin-state seam. Everything else —
layout/session, projects, on-disk tags, device config/metadata — is hand-rolled per surface:
defensively coded and crash-safe on read, but a set of N bespoke dicts with no shared
`Serializable`/record contract, several multi-copy entities kept in lock-step by hand (one
already drifted), and version fields that are almost entirely write-only. The codebase
demonstrably knows the right patterns (it built two of the best) and simply hasn't propagated
them; the one irreversible gap is that the Zarr store — the irreplaceable raw data — carries
no format version at all.

| Axis | Grade | One-line justification |
|---|---|---|
| **Abstraction** | **C+** | Bimodal: real seams (`Widget.state`, σ-registry, proto) vs. no shared contract for the hand-rolled tier; most surfaces lack even a `from_dict`. |
| **Maintainability** | **C+** | Loads are uniformly crash-safe, but multi-copy drift is real, round-trip tests exist for only 2 of ~10 surfaces, and 2 hot writers are non-atomic. |
| **Extensibility** | **B‑** | Superb for plugins (Panel/Processor = 1 touch) and σ-models (1 touch); heavy for wire/device/tag (5–6 hand edits) and store modalities (6–8). |
| **Versioning / Migration** | **D** | Version fields are decorative (session/export/project write-never-read), `CONTRACT_VERSION` carried-not-enforced, **Zarr store entirely unversioned**, zero migrators. |
| **Unification** | **C+** | Exemplary at wire+σ+docs; Project exists in 3 forms, Tag in 2 (already diverged), the device concept in ~5, and time is split ISO-vs-epoch. |

Overall: **a solid B‑ dragged down by a D in versioning** — principled where designed,
hand-maintained and version-blind everywhere else.

---

## 2. What is genuinely WELL-DONE

1. **The σ-model registry — best serializer in the codebase (`core/uncertainty.py`).**
   Discriminated union on `"type"`, registry-dispatched (`_TYPES = {c.TYPE: c for c in (...)}`),
   frozen dataclasses with 3-line `to_dict`/`_from_dict`, one serializer + three consumers with
   zero hand copies, recursive composition (`Spec`), graceful-unknown, and the **only surface
   with a round-trip test**. Adding a 6th model = 1 touch-point. This is the template.
2. **The proto is a real single source of truth** (`server/proto/.../data_plane.proto`) — one
   file drives agent/viewer/hub, and the hub persists to disk *through* it via `json_format`
   (tags, project folders), so wire-form and hub-disk-form share one schema.
3. **`Widget.state()/set_state()` is a genuine polymorphic seam** — a new Panel type is a true
   1-touch operation; dock `objectName` auto-derives so Qt geometry keys itself.
4. **`Project.to_record()/apply_record()` is well-factored** — proto-shaped record, so
   `net/convert.py` is a thin `json_format` wrapper and `project.json` auto-persists.
5. **Event-sourced provenance/config** (`zarrstore.py`) — an ops log folded to record-at-T,
   inherently version-free; the σ model piggybacks with zero special-casing.
6. **Units-as-canonical-strings** (`core/units.py`) — bare pint canonical strings everywhere,
   no second representation to drift against. The cleanest possible answer, not a debt.
7. **LWW-by-(id,version)-with-tombstones** — coherent convergence protocol, identical on client
   and hub, with re-publish-on-reconnect self-heal; atomic `tmp+os.replace` in most siblings.

---

## 3. TOP issues (ranked by impact × frequency)

1. **Zarr store carries no format version (IRREVERSIBLE).** No `version` anywhere in
   `zarrstore.py`; compat is pure `attrs.get(k, default)` degradation, no migrator. You cannot
   retrofit a version onto data already on disk. **Fix: stamp `schema_version` on root + device
   group + epoch `.attrs` NOW** (cheap, additive, forward-looking). Do regardless of any refactor.
2. **Two non-atomic writers on the hottest/most-crash-exposed paths.** `_write_session`
   (`ui/app.py`, the every-few-seconds working autosave) and `Project.save()` (`core/projects.py`)
   are plain `open()+json.dump`; a crash mid-write truncates the file → next launch `json.load`
   throws → silent loss of the working layout. Siblings already use `tmp+os.replace`. Two-line fix.
3. **Multi-copy entities with no compiler check, one already drifted.** Tag has two independent
   serializers (`marker_to_dict` disk vs `tag_to_proto` wire) that diverge (`run_dir` disk-only;
   payload values string-coerced on hub round-trip). Device concept: ~5 forms. Fix: back-port the
   Project proto-shaped-record pattern to Tag; declare local-only fields explicitly.
4. **`CONTRACT_VERSION` carried but never enforced** (hub reads only `agent_id`), and it's two
   divergent constants; docs claim it's "negotiated." Fix: gate at the hub or downgrade wording;
   collapse to one shared constant.
5. **No modality registry in the store** — dtype branches on TWO discriminators
   (`source.attrs["dtype"]` and epoch `attrs["modality"]`); a new dtype = 6–8 string-literal edits.
   Latent: `bool` persists via the scalar path so `_DTYPE_MAP["bool"]` is dead for historic
   sources. Fix: a modality registry mirroring the σ-registry.
6. **Four-method-per-panel field-axis drift** — `config_fields`/`apply_config`/`state`/`set_state`
   + `__init__` default, none compiler-checked; forget `state`/`set_state` and a field is
   editable but silently not persisted (there's even a `sigma_2`↔`sigma_k` key skew). Fix: one
   declarative field schema per panel driving both the config UI and persistence.
7. **Near-zero round-trip test coverage for the hand-rolled tier** — exactly two round-trip
   assertions in the whole suite (the two most-unified surfaces). No test for
   `import_layout(export_layout())`, `apply_record(to_record())`, `marker_to_dict`↔`from_dict`,
   or device config. Fix: add those round-trip tests — cheap, converts every future drift into a
   red test (de-risks #3 and #6).

---

## 4. Highest-leverage RECOMMENDATION

**Don't invent a framework — formalize the two patterns the repo already proved, and add the one
thing missing from both: a version that is actually *read*.**

- **One `Record`/`Serializable` convention codifying the two proven shapes:** polymorphic
  entities → the σ-registry shape (`TYPE` discriminant + `{TYPE: cls}` registry); dual disk/wire
  entities → the Project shape (one proto-shaped record dict + `json_format` bridge). Back-port
  Tag and the device concept onto this.
- **A real version hook, not a decorative field.** One shared `SCHEMA_VERSION` that readers
  *branch on* (`if version < N` + a `MIGRATORS` registry), stamped at every persistence root
  (Zarr root/device/epoch immediately, then session/project/export). Collapse the overloaded
  `version` terminology (format-tag vs LWW counter).
- **Declarative field schemas per Panel** (fixes #6 + the `sigma_2/sigma_k` skew).
- **Round-trip tests** for `export_layout`, `to_record`, `marker_to_dict`, device config
  (fixes #7 and locks in the above).

High-leverage because it's *propagation, not invention* — two of these pieces already run in
production here. The single most urgent sub-item (only irreversible one): stamp the Zarr
`schema_version` now.

---

## 5. EXTEND-COST verdict

- **Add a new Panel type — 1 touch-point today** (best-in-repo; register + write the class,
  persistence inherited). Unchanged under the recommendation.
- **Add a field to a Project — 3 touch-points** (proto+regen, `to_record` emit, `apply_record`
  consume); ~1 for a purely-local field. → ~1–2 under the recommendation.
- **Contrast Tag = 6 today** (proto + dataclass + 4 uncompiler-checked mappers) → 3 once it
  adopts the Project pattern; **store modality = 6–8**.

The through-line: extend-cost is excellent wherever a pattern was applied (Panel 1, σ-model 1,
Project 3) and heavy wherever it wasn't (Tag 6, device ~5, store modality 6–8). The fix is
finishing the propagation of what already works.
