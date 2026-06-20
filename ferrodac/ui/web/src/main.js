// ferroDAC in-app document renderer (slice 1: render-only).
//
// The .md FILE is the source of truth. This view never touches disk — it asks the
// Qt `bridge` (QWebChannel) for the current document's text and re-renders on every
// change the daemon/file-watcher reports. An external editor (Neovim, …) editing the
// raw file is just another writer the watcher notices. Bundled offline (no CDN).

import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import remarkRehype from "remark-rehype";
import rehypeKatex from "rehype-katex";
import rehypeHighlight from "rehype-highlight";
import rehypeStringify from "rehype-stringify";

const processor = unified()
  .use(remarkParse)
  .use(remarkGfm)
  .use(remarkMath)
  .use(remarkRehype)
  .use(rehypeHighlight, { detect: true })
  .use(rehypeKatex)
  .use(rehypeStringify);

const docEl = () => document.getElementById("doc");

// A line that is exactly `$$ … $$` should be CENTERED display math (what every
// other tool does), but remark-math only treats `$$` as display when the fences
// are on their own lines. Expand a standalone `$$…$$` line to that block form
// before parsing — the file on disk is untouched, this is render-only.
function expandDisplayMath(md) {
  return md.replace(/^[ \t]*\$\$([^$]+?)\$\$[ \t]*$/gm,
                    (_m, inner) => `$$\n${inner.trim()}\n$$`);
}

async function render(md) {
  try {
    docEl().innerHTML = String(await processor.process(expandDisplayMath(md || "")));
  } catch (e) {
    docEl().innerHTML = `<pre class="render-error">render error: ${String(e)}</pre>`;
  }
}

// Standalone (no Qt host) → render a sample so the bundle is testable on its own.
const SAMPLE = `# ferroDAC docs

A live, offline render of your project's markdown — edited in **your** editor.

## Math
Inline $E = mc^2$ and a block:

$$\\int_0^\\infty e^{-x^2}\\,dx = \\tfrac{\\sqrt{\\pi}}{2}$$

## Code
\`\`\`python
def base_pressure(p): return p < 1e-9   # mbar
\`\`\`

- [x] auto-context captured
- [ ] add a setup photo
`;

function connectBridge() {
  if (!window.qt || !window.qt.webChannelTransport || typeof QWebChannel === "undefined") {
    render(SAMPLE);                       // dev / standalone fallback
    return;
  }
  // eslint-disable-next-line no-undef
  new QWebChannel(qt.webChannelTransport, (channel) => {
    const bridge = channel.objects.bridge;
    bridge.docChanged.connect((_relpath, text) => render(text));  // (re)load + watch
    bridge.ready();                       // tell Qt we're ready → it sends the doc
  });
}

window.addEventListener("DOMContentLoaded", connectBridge);
