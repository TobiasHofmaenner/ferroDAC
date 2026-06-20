# ferroDAC document view — web bundle

The in-app document renderer (markdown + LaTeX + code highlighting), hosted by
QtWebEngine in `ferrodac/ui/docs.py`. **The `.md` file is the source of truth**;
this only renders it.

## Node is BUILD-TIME ONLY

Node/npm here are a *dev toolchain*, exactly like protoc for the gRPC stubs — they
bundle the JS deps into **`dist/`, which is committed to the repo and ships with the
app**. At runtime, **QtWebEngine (Chromium) executes the bundle — there is no Node
process in the running app**, and the PyInstaller build never sees Node. You only
need Node if you want to *rebuild* the bundle.

## Rebuild (only when JS deps / `src/` change)

```sh
cd ferrodac/ui/web
npm install      # fetch deps into node_modules/ (gitignored)
npm run build    # esbuild → dist/ (commit the result)
```

`dist/` = `index.html` + `bundle.js` + `app.css` + KaTeX (`katex.min.css`, `fonts/`)
+ a highlight.js theme. Fully offline; no CDN.

## Stack

unified (remark/rehype) + remark-gfm + remark-math + rehype-katex + rehype-highlight,
bundled with esbuild. Later slices add CodeMirror 6 (in-app editing) and a Yjs live
overlay — same `dist/` component, grown.
