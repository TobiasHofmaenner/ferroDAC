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
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import { languages } from "@codemirror/language-data";
import { startCompletion } from "@codemirror/autocomplete";
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
// suppressErrorRendering: don't draw mermaid's "bomb" error SVG (it gets appended to
// document.body and orphans there); we validate + show a clean inline error instead.
mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict",
                     suppressErrorRendering: true });

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
let docDir = "";          // the open doc's folder — resolves relative image links
let recordingsCache = [];               // [{id,label,t0,t1}] — the /rec macro's choices
const recordingsWaiters = [];           // resolvers awaiting a cold-cache fetch
let pendingRec = null;                  // {id, mode:"list"|"export"} — drives stage 2
const existingResults = new Map();      // recId → files ALREADY exported (fast scan)
const existingWaiters = new Map();
const exportResults = new Map();        // recId → files from a fresh export-now
const exportWaiters = new Map();
let processorsCache = [];               // [{kind,label}] — the /proc macro's choices
const processorsWaiters = [];           // resolvers awaiting a cold-cache fetch
let awaitingProcInsert = false;         // a picked processor's source is being fetched
let awaitingDevTable = false;           // the /dev instruments table is being built
let awaitingMeta = false;               // the /meta front-matter block is being built
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
  html = resolveLocalImages(html);
  const doc = $("doc");
  const top = doc.scrollTop;                        // replacing innerHTML resets scroll
  doc.innerHTML = html;
  doc.scrollTop = top;                              // …so put the reader back where they were
  await renderMermaid(doc, seq);
}

// The preview page is served from dist/, so a relative ![](plots/x.png) would resolve
// against the BUNDLE, not the .md. Rewrite relative image srcs to an absolute file://
// under the doc's folder. A <template> parses inertly (no image fetch), so each img
// loads exactly once, with the right URL.
function resolveLocalImages(html) {
  if (!docDir) return html;
  const tmpl = document.createElement("template");
  tmpl.innerHTML = html;
  for (const img of tmpl.content.querySelectorAll("img[src]")) {
    const src = img.getAttribute("src");
    if (!src || /^(https?:|data:|file:|blob:|\/\/)/i.test(src)) continue;
    try {
      img.setAttribute("src", new URL(src, "file://" + docDir + "/").href);
    } catch (e) { /* leave the original src */ }
  }
  return tmpl.innerHTML;
}

// Turn ```mermaid fences into rendered SVG. Mermaid is async, so honour the render
// sequence: bail if a newer render started while we were drawing.
async function renderMermaid(root, seq) {
  const blocks = root.querySelectorAll("code.language-mermaid, code.lang-mermaid");
  for (const code of blocks) {
    if (seq !== renderSeq) return;
    const src = code.textContent.trim();
    const host = code.closest("pre") || code;
    const fail = (e) => {
      if (seq !== renderSeq) return;
      const err = document.createElement("div");
      err.className = "mermaid-error";
      err.textContent = "⚠ mermaid: " + ((e && (e.message || e.str)) || String(e));
      host.replaceWith(err);
    };
    try {
      await mermaid.parse(src);                     // validate FIRST — no DOM, no orphan bomb
    } catch (e) {
      fail(e);
      continue;
    }
    if (seq !== renderSeq) return;
    try {
      const { svg } = await mermaid.render("m" + Math.random().toString(36).slice(2), src);
      if (seq !== renderSeq) return;
      const fig = document.createElement("div");
      fig.className = "mermaid-figure";
      fig.innerHTML = svg;
      host.replaceWith(fig);
    } catch (e) {
      fail(e);
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

// ── editor macros: `/rec` → recording → plot/CSV → insert markdown ──────────
// Exports are on-demand: picking a recording asks Qt to materialise its CSV+plots
// (into the project's reports/), then we offer those files as the next completions.
function waitMap(results, waiters, id) {        // resolve from cache or await the signal
  if (results.has(id)) return Promise.resolve(results.get(id));
  return new Promise((resolve) => {
    const ws = waiters.get(id) || [];
    ws.push(resolve);
    waiters.set(id, ws);
  });
}

function resolveMap(results, waiters, id, files) {
  results.set(id, files);
  const ws = waiters.get(id);
  waiters.delete(id);
  if (ws) ws.forEach((r) => r(files));
}

function spanLabel(rec) {
  try {
    const when = new Date(rec.t0 * 1000).toLocaleString();
    return `${when} · ${Math.max(0, rec.t1 - rec.t0).toFixed(0)}s`;
  } catch (e) { return ""; }
}

function refMarkdown(f) {                      // image embed for plots, plain link for data
  return f.kind === "plot" ? `![${f.name}](${f.relpath})` : `[${f.name}](${f.relpath})`;
}

function fileOption(f) {                        // a completion that inserts one file's link
  return {
    label: f.name, detail: f.kind,
    type: f.kind === "plot" ? "text" : "string",
    apply: (v, c, from, to) =>
      v.dispatch({ changes: { from, to, insert: refMarkdown(f) } }),
  };
}

// Resolve a macro's data cache. Always fires a refresh; when the cache is cold
// (e.g. right after a doc reload reset the page) it AWAITS the fetch so the menu
// populates on the first keystroke instead of silently showing nothing. A short
// timeout falls back to whatever we have, so the completion never hangs.
function whenCache(get, request, waiters, timeoutMs = 1500) {
  request();
  if (get().length) return get();              // warm → synchronous (snappy, as before)
  return new Promise((resolve) => {            // cold → wait for `*Available`
    let done = false;
    const finish = () => { if (done) return; done = true; resolve(get()); };
    waiters.push(finish);
    setTimeout(finish, timeoutMs);
  });
}

function flushWaiters(waiters) {               // a `*Available` arrived → wake the menu
  waiters.splice(0).forEach((fn) => fn());
}

// A completion source may answer synchronously or with a Promise — build either way.
function resolveMenu(data, build) {
  return data instanceof Promise ? data.then(build) : build(data);
}

// A single CM6 completion source driving the whole cascade via `pendingRec` state:
//   /rec → recordings → (existing files + Export-now) → [fresh files] → insert.
function slashSource(context) {
  // Stage 2 — a recording was picked. Clearing pendingRec up front keeps it transient.
  if (pendingRec) {
    const { id, mode } = pendingRec;
    pendingRec = null;
    if (mode === "export") {                   // awaited a fresh export → pick a file
      return waitMap(exportResults, exportWaiters, id).then((files) => {
        if (!files || !files.length) return null;
        return { from: context.pos, filter: false, options: files.map(fileOption) };
      });
    }
    // mode "list" — already-exported files, plus an Export-now option
    return waitMap(existingResults, existingWaiters, id).then((files) => {
      const options = (files || []).map(fileOption);
      options.push({
        label: "⟳ Export now", detail: "render fresh", type: "keyword",
        apply: (v) => {
          pendingRec = { id, mode: "export" };
          exportResults.delete(id);            // force a fresh await
          if (bridge) bridge.requestRecordingExport(id);
          startCompletion(v);                  // → stage 2 (export)
        },
      });
      return { from: context.pos, filter: false, options };
    });
  }
  // Stage 1 — a slash command: `/rec` (recordings) or `/proc` (processor source).
  const w = context.matchBefore(/\/(\w*)/);
  if (!w || (w.from === w.to && !context.explicit)) return null;
  const cmd = w.text.slice(1).toLowerCase();
  if (cmd === "rec") {
    return resolveMenu(
      whenCache(() => recordingsCache,
                () => { if (bridge) bridge.requestRecordings(); },
                recordingsWaiters),
      (recs) => !recs.length ? null : {
        from: w.from, filter: false,
        options: recs.map((rec) => ({
          label: "rec: " + rec.label, detail: spanLabel(rec), type: "function",
          apply: (v, c, from, to) => {
            v.dispatch({ changes: { from, to, insert: "" } });   // drop the `/rec`
            pendingRec = { id: rec.id, mode: "list" };
            existingResults.delete(rec.id);                       // re-scan fresh
            if (bridge) bridge.requestRecordingExports(rec.id);   // list existing exports
            startCompletion(v);                                    // → stage 2 (list)
          },
        })),
      });
  }
  if (cmd === "proc") {
    return resolveMenu(
      whenCache(() => processorsCache,
                () => { if (bridge) bridge.requestProcessors(); },
                processorsWaiters),
      (procs) => !procs.length ? null : {
        from: w.from, filter: false,
        options: procs.map((proc) => ({
          label: "proc: " + proc.label, detail: "source", type: "function",
          apply: (v, c, from, to) => {
            v.dispatch({ changes: { from, to, insert: "" } });   // drop the `/proc`
            awaitingProcInsert = true;
            if (bridge) bridge.requestProcessorSource(proc.kind);  // inserted on arrival
          },
        })),
      });
  }
  if (cmd === "dev") {
    return {
      from: w.from, filter: false,
      options: [{
        label: "dev: Instruments used", detail: "lab-journal table", type: "function",
        apply: (v, c, from, to) => {
          v.dispatch({ changes: { from, to, insert: "" } });   // drop the `/dev`
          awaitingDevTable = true;
          if (bridge) bridge.requestDeviceTable();              // inserted on arrival
        },
      }],
    };
  }
  if (cmd === "meta") {
    return {
      from: w.from, filter: false,
      options: [{
        label: "meta: Report header", detail: "front matter", type: "function",
        apply: (v, c, from, to) => {
          v.dispatch({ changes: { from, to, insert: "" } });   // drop the `/meta`
          awaitingMeta = true;
          if (bridge) bridge.requestRunMeta();                 // inserted on arrival
        },
      }],
    };
  }
  return null;
}

// Insert a Markdown block the app built (the curated devices, the run header, …).
function insertMarkdownBlock(md) {
  if (!editor || !md) return;
  const block = "\n\n" + md.replace(/\s+$/, "") + "\n";
  const at = editor.state.selection.main.head;
  editor.dispatch({ changes: { from: at, insert: block },
                    selection: { anchor: at + block.length } });
}

// Insert a processor's source as a fenced code block (open science — cite the
// algorithm), plus a link to its white paper when the extension ships one.
function insertProcessorSource(kind, src, paperRel) {
  if (!editor || !src) return;
  const proc = processorsCache.find((p) => p.kind === kind);
  const label = (proc && proc.label) || kind;
  const cite = paperRel ? ` ([white paper](${paperRel}))` : "";
  const block = `\n\n*${label} — processor source${cite}:*\n\n\`\`\`python\n`
    + src.replace(/\s+$/, "") + "\n```\n";
  const at = editor.state.selection.main.head;
  editor.dispatch({ changes: { from: at, insert: block },
                    selection: { anchor: at + block.length } });
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
                 markdownLanguage.data.of({ autocomplete: slashSource }),  // /rec macro
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

// Editor slash commands — shown by the ⓘ button (extend as more land).
const SLASH_HELP = [
  { cmd: "/rec", title: "Insert a recording's exports",
    desc: "Pick a recording → its already-exported plots / CSV (or “⟳ Export now”) → "
        + "inserts the image (or a link) with a path relative to the doc." },
  { cmd: "/proc", title: "Insert a processor's source",
    desc: "Pick a processor in use → drops its source as a python code block, plus a "
        + "white-paper link if the (plugin) processor ships one." },
  { cmd: "/dev", title: "Insert an instruments table",
    desc: "Drops a lab-journal table of the devices behind your curated sources — "
        + "manufacturer, model, serial, firmware, calibration and asset tag." },
  { cmd: "/meta", title: "Insert a report header",
    desc: "Drops a front-matter block — experiment, date(s), experimenter(s), "
        + "sample, instruments, recordings and the ferroDAC version." },
];

function buildMacroHelp() {
  const pop = $("macrohelp");
  if (!pop) return;
  pop.innerHTML =
    "<div class='mh-head'>Editor slash commands — type <code>/</code> where you want to "
    + "insert:</div>"
    + SLASH_HELP.map((c) =>
        `<div class='mh-row'><code class='mh-cmd'>${c.cmd}</code>`
        + `<div><b>${c.title}</b><br><span class='mh-desc'>${c.desc}</span></div></div>`)
      .join("");
}

function wireToolbar() {
  for (const b of document.querySelectorAll("#toolbar [data-mode]"))
    b.addEventListener("click", () => setMode(b.dataset.mode));
  const rb = $("reload");
  if (rb) rb.addEventListener("click", reloadFromDisk);
  const pb = $("savepdf");
  if (pb) pb.addEventListener("click", () => {
    renderPreview();                          // make sure the preview is current
    if (bridge) bridge.requestPdf();          // Qt picks a path + prints (status back)
    else status("PDF needs the app");
  });
  const hb = $("macrohelp-btn");
  if (hb) {
    buildMacroHelp();
    hb.addEventListener("click", () => {
      const pop = $("macrohelp");
      if (pop) pop.hidden = !pop.hidden;
    });
    document.addEventListener("click", (e) => {      // click away to dismiss
      const pop = $("macrohelp");
      if (pop && !pop.hidden && e.target !== hb && !pop.contains(e.target))
        pop.hidden = true;
    });
  }
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
    bridge.docContext.connect((dir) => { docDir = dir || ""; renderPreview(); });
    bridge.docChanged.connect(onIncoming);
    bridge.recordingsAvailable.connect((j) => {
      try { recordingsCache = JSON.parse(j) || []; } catch (e) { recordingsCache = []; }
      flushWaiters(recordingsWaiters);          // wake a cold-cache /rec menu
    });
    bridge.recordingExports.connect((recId, j) => {        // already-exported files
      let files = []; try { files = JSON.parse(j) || []; } catch (e) { /* none */ }
      resolveMap(existingResults, existingWaiters, recId, files);
    });
    bridge.recordingExported.connect((recId, j) => {       // a fresh export-now
      let files = []; try { files = JSON.parse(j) || []; } catch (e) { /* none */ }
      existingResults.set(recId, files);                  // it now "exists" too
      resolveMap(exportResults, exportWaiters, recId, files);
    });
    bridge.processorsAvailable.connect((j) => {
      try { processorsCache = JSON.parse(j) || []; } catch (e) { processorsCache = []; }
      flushWaiters(processorsWaiters);          // wake a cold-cache /proc menu
    });
    bridge.processorSource.connect((kind, src, paperRel) => {  // picked /proc → insert
      if (!awaitingProcInsert) return;
      awaitingProcInsert = false;
      insertProcessorSource(kind, src, paperRel);
    });
    bridge.deviceTable.connect((md) => {                   // picked /dev → insert
      if (!awaitingDevTable) return;
      awaitingDevTable = false;
      insertMarkdownBlock(md);
    });
    bridge.runMeta.connect((md) => {                       // picked /meta → insert
      if (!awaitingMeta) return;
      awaitingMeta = false;
      insertMarkdownBlock(md);
    });
    bridge.pdfExported.connect((msg) => status(msg));      // save-PDF result
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
  // /rec macro test hooks
  recordings: () => recordingsCache,
  startCompletion: () => editor && startCompletion(editor),
  // simulate a just-reloaded page (caches reset) to exercise the cold-cache path
  _coldCaches: () => { recordingsCache = []; processorsCache = []; },
  // run the real completion source for "/<cmd>" at the doc end → option labels
  // (awaits the async cold-cache fetch, like CM6 does). For macro-menu regressions.
  slashOptions: (cmd) => {
    if (!editor) return Promise.resolve([]);
    const L = editor.state.doc.length;
    const ins = "\n\n/" + cmd;
    editor.dispatch({ changes: { from: L, insert: ins },
                      selection: { anchor: L + ins.length } });
    const pos = editor.state.selection.main.head;
    const ctx = {                                 // a minimal CompletionContext
      pos, explicit: true, state: editor.state,
      matchBefore: (re) => {
        const line = editor.state.doc.lineAt(pos);
        const text = line.text.slice(0, pos - line.from);
        const m = text.match(new RegExp(re.source + "$"));
        return m ? { from: pos - m[0].length, to: pos, text: m[0] } : null;
      },
    };
    return Promise.resolve(slashSource(ctx))
      .then((res) => (res && res.options) ? res.options.map((o) => o.label) : []);
  },
  // type `/rec` at the end with the cursor right after it, then open the menu
  openRecMenu: () => {
    if (!editor) return;
    const L = editor.state.doc.length;
    editor.dispatch({ changes: { from: L, insert: "\n\n/rec" },
                      selection: { anchor: L + 6 } });
    startCompletion(editor);
  },
  // /proc test hooks
  processors: () => processorsCache,
  insertProcessorSource: (kind) => {            // pick a processor → fetch + insert source
    awaitingProcInsert = true;
    if (bridge) bridge.requestProcessorSource(kind);
  },
  // /dev test hook: request the instruments table → inserted on arrival
  insertDeviceTable: () => {
    awaitingDevTable = true;
    if (bridge) bridge.requestDeviceTable();
  },
  // /meta test hook: request the report header → inserted on arrival
  insertMeta: () => {
    awaitingMeta = true;
    if (bridge) bridge.requestRunMeta();
  },
  // compute the stage-2 (list) menu for a recording → result in __doc._lastLabels
  stage2Labels: (recId) => {
    window.__doc._lastLabels = null;
    existingResults.delete(recId);
    if (bridge) bridge.requestRecordingExports(recId);
    waitMap(existingResults, existingWaiters, recId).then((files) => {
      const labels = (files || []).map((f) => f.name);
      labels.push("⟳ Export now");
      window.__doc._lastLabels = labels;
    });
  },
  // drive a fresh export→insert flow for a recording (without the dropdown UI)
  insertFirstPlot: async (recId) => {
    if (bridge) bridge.requestRecordingExport(recId);
    const files = await waitMap(exportResults, exportWaiters, recId);
    const f = files.find((x) => x.kind === "plot") || files[0];
    if (!f || !editor) return null;
    editor.dispatch(
      { changes: { from: editor.state.doc.length, insert: "\n\n" + refMarkdown(f) } });
    return f.relpath;
  },
};

window.addEventListener("DOMContentLoaded", connect);
