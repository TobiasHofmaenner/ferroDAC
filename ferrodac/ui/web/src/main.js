// ferroDAC in-app document view (editor + live preview + collaboration).
//
// The .md FILE is the source of truth. CodeMirror holds the live text:
//   * SOLO  — edits autosave (debounced) to the file via the Qt `bridge`.
//   * COLLAB — a Yjs doc (bound via y-codemirror.next, behind a Compartment so
//     solo is byte-for-byte untouched) drives the editor; opaque updates ride the
//     Qt bridge to the hub, and the text is materialised back to the .md (locally
//     and, by the room leader, on the server). Presence cursors come from Yjs
//     awareness. Three view modes — Read / Edit / Split. Bundled offline (no CDN).

import { EditorView, basicSetup } from "codemirror";
import { Compartment } from "@codemirror/state";
import { markdown } from "@codemirror/lang-markdown";
import { languages } from "@codemirror/language-data";
import { oneDark } from "@codemirror/theme-one-dark";

import * as Y from "yjs";
import { yCollab } from "y-codemirror.next";
import {
  Awareness,
  encodeAwarenessUpdate,
  applyAwarenessUpdate,
} from "y-protocols/awareness";

import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import remarkRehype from "remark-rehype";
import rehypeKatex from "rehype-katex";
import rehypeHighlight from "rehype-highlight";
import rehypeStringify from "rehype-stringify";

import mermaid from "mermaid";
mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });

const processor = unified()
  .use(remarkParse)
  .use(remarkGfm)
  .use(remarkMath)
  .use(remarkRehype)
  // ignoreMissing: a ```mermaid (or other unknown) fence passes through untouched
  // (raw source preserved) instead of throwing — we render mermaid ourselves below.
  .use(rehypeHighlight, { detect: true, ignoreMissing: true })
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

// -- collaboration state --------------------------------------------------
const collabCompartment = new Compartment();   // holds the yCollab binding (empty in solo)
let collabActive = false;
let collabReady = false;  // have we got our initial state? (gates materialise)
let ydoc = null;
let awareness = null;
let actorName = "";
let pendingDiskText = null;  // external .md edit awaiting an explicit "reload from disk"
let matTimer = null;      // debounce: materialise the text → local .md + server snapshot

// base64 ↔ Uint8Array — Yjs updates are binary, the Qt/JS bridge carries strings.
function b64encode(u8) {
  let s = "";
  const CH = 0x8000;
  for (let i = 0; i < u8.length; i += CH)
    s += String.fromCharCode.apply(null, u8.subarray(i, i + CH));
  return btoa(s);
}
function b64decode(str) {
  const s = atob(str);
  const u8 = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) u8[i] = s.charCodeAt(i);
  return u8;
}

function colorFor(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360} 70% 60%)`;
}

function renderPresence(actors) {
  const el = $("presence");
  if (!el) return;
  el.replaceChildren();
  for (const a of actors) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.title = a;
    chip.textContent = ((a.split("@")[0] || "?")[0] || "?").toUpperCase();
    chip.style.background = colorFor(a);
    el.appendChild(chip);
  }
}

let renderSeq = 0;
async function renderPreview() {
  const seq = ++renderSeq;                          // drop stale out-of-order renders
  let html;
  try {
    html = String(await processor.process(expandDisplayMath(text())));
  } catch (e) {
    html = `<pre class="render-error">render error: ${String(e)}</pre>`;
  }
  if (seq !== renderSeq) return;                    // only the latest render wins
  $("doc").innerHTML = html;
  await renderMermaid($("doc"), seq);
}

// Turn ```mermaid fences into rendered SVG. Mermaid is async, so honour the render
// sequence: bail if a newer render started while we were drawing.
async function renderMermaid(root, seq) {
  const blocks = root.querySelectorAll("code.language-mermaid, code.lang-mermaid");
  for (const code of blocks) {
    if (seq !== renderSeq) return;
    const src = code.textContent;
    const host = code.closest("pre") || code;
    try {
      const id = "m" + Math.random().toString(36).slice(2);
      const { svg } = await mermaid.render(id, src);
      if (seq !== renderSeq) return;
      const fig = document.createElement("div");
      fig.className = "mermaid-figure";
      fig.innerHTML = svg;
      host.replaceWith(fig);
    } catch (e) {                                   // invalid diagram → leave the source
      /* keep the code block as-is */
    }
  }
}

function text() {
  return editor ? editor.state.doc.toString() : lastSynced;
}

function scheduleSave() {                       // SOLO only
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
    if (!u.docChanged) return;
    renderPreview();
    if (collabActive) { status("collaborating"); scheduleMaterialise(); }
    else { status("editing…"); scheduleSave(); }
  });
  editor = new EditorView({
    doc: initial,
    extensions: [basicSetup, markdown({ codeLanguages: languages }), oneDark,
                 collabCompartment.of([]), onChange],
    parent: $("editor"),
  });
}

function setEditorText(t) {
  if (!editor) { makeEditor(t); return; }
  editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: t } });
}

// A document arrived from Qt: the initial load, or an EXTERNAL edit (file-watch).
function onIncoming(_relpath, incoming) {
  if (collabActive) {                           // the live session is truth, not the file
    // Don't auto-merge — an external save is a snapshot that could clobber others'
    // live edits. Offer an explicit "↻ Reload from disk" instead.
    if (incoming !== text()) {
      pendingDiskText = incoming;
      showReload(true);
      status("⚠ changed on disk");
    } else {
      pendingDiskText = null;
      showReload(false);
    }
    return;
  }
  if (incoming === lastSynced) return;          // our own save echoed back — ignore
  if (!editor || text() === lastSynced) {       // no unsaved local edits → take it
    lastSynced = incoming;
    setEditorText(incoming);
    renderPreview();
  } else {
    status("⚠ changed on disk");                // local edits in flight win (last writer)
  }
}

// -- collaboration --------------------------------------------------------
function scheduleMaterialise() {
  clearTimeout(matTimer);
  matTimer = setTimeout(() => {
    if (!collabActive || !collabReady || !bridge) return;
    const t = text();
    lastSynced = t;
    bridge.save(t);                   // local .md — file-as-truth on this client
    bridge.collabSendSnapshot(t);     // server .md — the hub honours only the leader's
  }, 800);
}

function enterCollab(shouldSeed, seedText, actor) {
  if (collabActive) return;
  actorName = actor || "editor";
  // The seeder establishes the shared doc. Prefer THIS view's current text (the
  // local .md the user is looking at) over the server's — on the FIRST ever
  // collaboration the server file is empty, and seeding from it would wipe the
  // local content. The server text is only a fallback (an empty local view).
  const localText = text();
  ydoc = new Y.Doc();
  const ytext = ydoc.getText("md");
  ydoc.on("update", (u, origin) => {
    if (origin !== "remote" && bridge)
      bridge.collabSendUpdate(b64encode(u), false);   // local edit → up
    else if (origin === "remote")
      collabReady = true;                              // got state from a peer
    // Render + materialise are driven by the editor's updateListener (onChange),
    // which fires AFTER y-codemirror reflects this change into the editor — so it
    // reads FRESH text. Rendering here would read the editor before that sync.
  });
  awareness = new Awareness(ydoc);
  awareness.setLocalStateField("user", { name: actorName, color: colorFor(actorName) });
  awareness.on("update", ({ added, updated, removed }) => {
    if (!bridge) return;
    const changed = added.concat(updated, removed);
    bridge.collabSendAwareness(
      b64encode(encodeAwarenessUpdate(awareness, changed)));
  });

  if (!editor) makeEditor("");
  collabActive = true;
  collabReady = shouldSeed;                      // seeder has state now; others await it
  // Clear the editor so it matches the EMPTY ytext before binding — otherwise
  // y-codemirror could push this view's stale file text into the shared doc
  // (the duplication trap). The seeder then fills the doc from the file text.
  editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: "" } });
  editor.dispatch({ effects: collabCompartment.reconfigure(yCollab(ytext, awareness)) });
  if (shouldSeed)
    ydoc.transact(() => ytext.insert(0, localText || seedText || ""), "seed");
  if (mode === "read") setMode("split");         // surface the collaboration
  status("collaborating");
}

function leaveCollab() {
  if (!collabActive) return;
  const finalText = text();
  editor.dispatch({ effects: collabCompartment.reconfigure([]) });
  if (awareness) awareness.destroy();
  if (ydoc) ydoc.destroy();
  ydoc = null;
  awareness = null;
  collabActive = false;
  collabReady = false;
  clearTimeout(matTimer);
  lastSynced = finalText;
  if (bridge) bridge.save(finalText);            // land the final text in the local .md
  renderPresence([]);
  pendingDiskText = null;
  showReload(false);
  status("");
}

function showReload(on) {
  const b = $("reload");
  if (b) b.hidden = !on;
}

// Explicit "↻ Reload from disk": apply the on-disk text to the LIVE doc (one Yjs
// transaction, broadcast to peers). Last-writer-wins by the user's choice.
function reloadFromDisk() {
  if (pendingDiskText == null || !ydoc) return;
  applyTextToYText(ydoc.getText("md"), pendingDiskText);
  pendingDiskText = null;
  showReload(false);
  status("collaborating");
}

// Replace ytext's content with newText via a minimal prefix/suffix diff — keeps the
// untouched regions (and cursors there) instead of churning the whole doc.
function applyTextToYText(ytext, newText) {
  const old = ytext.toString();
  if (old === newText) return;
  const m = Math.min(old.length, newText.length);
  let s = 0;
  while (s < m && old[s] === newText[s]) s++;
  let e = 0;
  while (e < m - s && old[old.length - 1 - e] === newText[newText.length - 1 - e]) e++;
  ytext.doc.transact(() => {
    if (old.length - s - e > 0) ytext.delete(s, old.length - s - e);
    const ins = newText.slice(s, newText.length - e);
    if (ins) ytext.insert(s, ins);
  }, "reload");
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
  const rb = $("reload");
  if (rb) rb.addEventListener("click", reloadFromDisk);
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
    bridge.collabSeed.connect(enterCollab);
    bridge.collabUpdate.connect((b64) => {
      if (ydoc) Y.applyUpdate(ydoc, b64decode(b64), "remote");
    });
    bridge.collabAwareness.connect((b64) => {
      if (awareness) applyAwarenessUpdate(awareness, b64decode(b64), "remote");
    });
    bridge.collabPresence.connect((actorsJson) => {
      let actors = [];
      try { actors = JSON.parse(actorsJson); } catch (e) { /* ignore */ }
      renderPresence(actors);
      const n = actors.length;
      status(`collaborating · ${n} editor${n === 1 ? "" : "s"}`);
    });
    bridge.collabStopped.connect(leaveCollab);
    bridge.collabRequestState.connect(() => {
      // a new LOCAL view (e.g. a popped-out window) needs the current doc — dump our
      // full Yjs state so it converges (it started empty).
      if (ydoc && bridge)
        bridge.collabSendUpdate(b64encode(Y.encodeStateAsUpdate(ydoc)), false);
    });
    bridge.ready();
  });
}

// Headless test/diagnostic hook (harmless in production): drive a local edit and
// read the rendered output without reaching into the IIFE scope.
window.__doc = {
  insert: (s) => editor && editor.dispatch(
    { changes: { from: editor.state.doc.length, insert: s } }),
  html: () => $("doc").innerHTML,
  text,
};

window.addEventListener("DOMContentLoaded", connect);
