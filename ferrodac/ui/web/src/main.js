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

// -- collaboration state --------------------------------------------------
const collabCompartment = new Compartment();   // holds the yCollab binding (empty in solo)
let collabActive = false;
let collabReady = false;  // have we got our initial state? (gates materialise)
let ydoc = null;
let awareness = null;
let actorName = "";
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
    if (incoming !== text()) status("⚠ changed on disk");
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
    if (origin === "remote") collabReady = true;       // got state from a peer
    renderPreview();
    scheduleMaterialise();
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
  status("");
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
    bridge.ready();
  });
}

window.addEventListener("DOMContentLoaded", connect);
