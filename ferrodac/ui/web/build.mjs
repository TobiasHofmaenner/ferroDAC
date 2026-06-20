// Bundle the document renderer into dist/ (committed to the repo so the app ships
// it OFFLINE — no CDN, local-first). Run: `npm run build` (Node + npm only).
import * as esbuild from "esbuild";
import { cpSync, mkdirSync, copyFileSync } from "node:fs";

const OUT = "dist";
mkdirSync(OUT, { recursive: true });

await esbuild.build({
  entryPoints: ["src/main.js"],
  bundle: true,
  format: "iife",
  minify: true,
  sourcemap: false,
  outfile: `${OUT}/bundle.js`,
  logLevel: "info",
});

// static page + styles
copyFileSync("src/index.html", `${OUT}/index.html`);
copyFileSync("src/app.css", `${OUT}/app.css`);

// KaTeX stylesheet + its fonts (the CSS references ./fonts/*)
copyFileSync("node_modules/katex/dist/katex.min.css", `${OUT}/katex.min.css`);
cpSync("node_modules/katex/dist/fonts", `${OUT}/fonts`, { recursive: true });

// highlight.js theme for rendered code blocks
copyFileSync("node_modules/highlight.js/styles/github-dark.css", `${OUT}/highlight.css`);

console.log("built →", OUT);
