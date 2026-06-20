// ferroDAC in-app document view (slice 2: editor + preview).
//
// The .md FILE is the source of truth. CodeMirror holds the live text; edits
// autosave (debounced) back to the file via the Qt `bridge`, and an external
// editor (Neovim, …) editing the raw file is reconciled in. Three view modes —
// Read / Edit / Split — over one editor + the markdown/LaTeX renderer. Bundled
// offline (no CDN).

import { EditorView, basicSetup } from "codemirror";
import { markdown } from "@codemirror/lang-markdown";
import { languages } from "@codemirror/language-data";
import { oneDark } from "@codemirror/theme-one-dark";

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

// A standalone `$$ … $$` line → centered display math (what every tool does);
// remark-math only treats it as display when the fences are on their own lines.
function expandDisplayMath(md) {
  return md.replace(/^[ \t]*\$\$([^$]+?)\$\$[ \t]*$/gm,
                    (_m, inner) => `$$\n${inner.trim()}\n$$`);
}

const $ = (id) => document.getElementById(id);

let bridge = null;
let editor = null;
let mode = "read";        // read | edit | split
let lastSynced = "";      // the text the file currently holds (per our knowledge)
let saveTimer = null;

async function renderPreview() {
  try {
    $("doc").innerHTML = String(await processor.process(expandDisplayMath(text())));
  } catch (e) {
    $("doc").innerHTML = `<pre class="render-error">render error: ${String(e)}</pre>`;
  }
}

function text() {
  return editor ? editor.state.doc.toString() : lastSynced;
}

function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    const t = text();
    if (t !== lastSynced && bridge) {
      lastSynced = t;
      bridge.save(t);               // → Qt writes the .md (file stays truth)
      status("saved");
    }
  }, 600);
}

function status(s) {
  const el = $("status");
  if (el) el.textContent = s;
}

function makeEditor(initial) {
  const onChange = EditorView.updateListener.of((u) => {
    if (u.docChanged) { renderPreview(); scheduleSave(); status("editing…"); }
  });
  editor = new EditorView({
    doc: initial,
    extensions: [basicSetup, markdown({ codeLanguages: languages }), oneDark, onChange],
    parent: $("editor"),
  });
}

function setEditorText(t) {
  if (!editor) { makeEditor(t); return; }
  editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: t } });
}

// A document arrived from Qt: the initial load, or an EXTERNAL edit (file-watch).
function onIncoming(_relpath, incoming) {
  if (incoming === lastSynced) return;          // our own save echoed back — ignore
  if (!editor || text() === lastSynced) {       // no unsaved local edits → take it
    lastSynced = incoming;
    setEditorText(incoming);
    renderPreview();
  } else {
    status("⚠ changed on disk");                // local edits in flight win (last writer)
  }
}

function setMode(m) {
  mode = m;
  document.body.dataset.mode = m;               // CSS shows/hides the panes
  for (const b of document.querySelectorAll("#toolbar [data-mode]"))
    b.classList.toggle("active", b.dataset.mode === m);
  if (m !== "read" && editor) editor.focus();
}

function wireToolbar() {
  for (const b of document.querySelectorAll("#toolbar [data-mode]"))
    b.addEventListener("click", () => setMode(b.dataset.mode));
}

// Standalone (no Qt host) → a sample so the bundle is testable on its own.
const SAMPLE = "# ferroDAC docs\n\nEdit me — autosaves to the file.\n\n$$E = mc^2$$\n";

function connect() {
  wireToolbar();
  setMode("read");
  if (!window.qt || !window.qt.webChannelTransport || typeof QWebChannel === "undefined") {
    onIncoming("", SAMPLE);                     // dev / standalone fallback
    return;
  }
  // eslint-disable-next-line no-undef
  new QWebChannel(qt.webChannelTransport, (channel) => {
    bridge = channel.objects.bridge;
    bridge.docChanged.connect(onIncoming);
    bridge.ready();
  });
}

window.addEventListener("DOMContentLoaded", connect);
