# ferrodac-ext-example

A **reference ferroDAC extension** — the template to copy when writing your own.
It provides one tiny, dependency-light processor (`WindowIntegral`: integrate a
spectrum over an m/z window → a scalar) plus its white paper.

## Layout

```
ferrodac-extension.toml      # the manifest — what this repo provides
ferrodac_ext_example/        # a normal importable Python package
  processors/integrate.py    # class WindowIntegral(Processor)
papers/integrate.md          # the algorithm's white paper (shown in-app)
tests/                       # the plugin's own tests
pyproject.toml               # optional — author tooling (tests/types/editor)
```

## How ferroDAC loads it

1. You add the repo's URL in ferroDAC → it `git clone`s a **pinned commit**.
2. ferroDAC reads `ferrodac-extension.toml`, checks the `api` version, and shows you
   what it provides + its white papers behind a "this runs code on your machine" gate.
3. On enable, each `entry` (`module:Class`) is imported and registered. The processor
   then appears like any built-in; "Show source" reads this very file.

## Writing your own

- Subclass the bases from the stable SDK: `from ferrodac.plugin import Processor, Port,
  Device, Widget, Trace, FLOAT, BOOL, TRACE`. **Never** import ferroDAC internals —
  `ferrodac.plugin` is the one surface promised to stay stable (versioned by `api`).
- Datatypes are a small, closed, documented set: `float`, `bool`, `trace` (a 1-D
  labelled array; interoperates with xarray/pint). Declare a processor's input via
  `accepts` and its outputs via `outputs() -> [Port]`.
- Add a `whitepaper` to each manifest entry — share the science, not just the code.

> Note: `ferrodac.plugin` is delivered by Phase 1 of the plugin platform. Until then
> this repo documents the **target** API.
