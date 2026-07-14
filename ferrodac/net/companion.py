"""CompanionServer — the "phone companion": a small LAN web app a phone joins over
WiFi to drop photos straight into the active project.

It RIDES the existing control surface: an upload dispatches the ``media.add_photo``
verb in-process (the MediaService owns storage — this server never touches the
media/ dir itself). Auth is a PRE-SHARED KEY: the app mints a pre-approved
connector (ConnectorRegistry.create_preshared) and hands the phone a link
``http://<lan-ip>:<port>/enter?k=<psk>`` (a QR in the app). /enter validates the
psk and drops an HttpOnly cookie so the phone stays "logged in".

Lifecycle mirrors LocalApiServer EXACTLY (Starlette ASGI on a uvicorn background
thread with its own asyncio loop; ``log_config=None`` — the frozen-Windows fix;
``install_signal_handlers`` disabled off the main thread; ``should_exit`` stop;
bound-port read). Differences: it binds 0.0.0.0 (the phone is on another box on the
LAN — there is NO TLS, so the UI carries a persistent "unencrypted" warning), and it
writes NO discovery file.

  GET  /health                 -> {ok, name, version}                    (no auth)
  GET  /enter?k=<psk>          -> 302 '/' + Set-Cookie, or an 'invalid link' page
  GET  /                       -> the mobile upload page, or a 'pair from the app' page
  POST /upload {category,label,data_b64} -> {ok, ...}   (cookie auth; needs 'control')

The upload page is server-rendered with INLINE css+js (mobile-first tiles): a big
active-project header, an <input type=file accept=image/* capture=environment> tile,
a category <select>, an optional caption, a client-side recent-thumbnails strip, and
the unencrypted-connection banner. JS: FileReader -> base64 -> fetch POST /upload JSON.
Qt-free.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import json
import logging
import threading
import time

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from ..core.control import ControlError, ScopeError, scope_rank

log = logging.getLogger("ferrodac.companion")

# The fixed set of photo categories (DESIGN — 'generic' is the catch-all).
CATEGORIES = ("setup", "sample", "result", "generic")

SESSION_COOKIE = "fdc_sess"


class CompanionServer:
    def __init__(self, surface, connectors, *, host: str = "0.0.0.0", port: int = 0,
                 app_name: str = "ferroDAC", version: str = "", get_project=None):
        self._surface = surface           # ControlSurface
        self._conns = connectors          # ConnectorRegistry
        self._host = host                 # 0.0.0.0 — the phone is on another LAN box
        self._req_port = port
        self._name = app_name
        self._version = version
        self._get_project = get_project   # () -> active project name (str) | dict | None
        self._server: "uvicorn.Server | None" = None
        self._thread: "threading.Thread | None" = None
        self._port = 0
        self._app = self._build_app()

    @property
    def port(self) -> int:
        return self._port

    # -- lifecycle (mirrors LocalApiServer) ---------------------------------
    def start(self) -> int:
        # log_config=None: never let uvicorn run logging.config.dictConfig — its default
        # 'default' formatter can't be built in the frozen, console-less Windows app.
        config = uvicorn.Config(self._app, host=self._host, port=self._req_port,
                                log_level="warning", access_log=False, log_config=None)
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None   # not on the main thread
        self._thread = threading.Thread(target=self._serve, name="fd-companion",
                                        daemon=True)
        self._thread.start()
        for _ in range(500):              # wait for the socket to bind (<=5 s)
            if self._server.started:
                break
            time.sleep(0.01)
        self._port = self._bound_port()
        log.info("phone companion on http://%s:%d", self._host, self._port)
        return self._port

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._server.serve())
        except Exception:                 # noqa: BLE001 — never crash the app thread
            log.debug("companion server ended", exc_info=True)

    def _bound_port(self) -> int:
        try:
            return self._server.servers[0].sockets[0].getsockname()[1]
        except (AttributeError, IndexError):
            return self._req_port

    # -- ASGI app ------------------------------------------------------------
    def _build_app(self) -> Starlette:
        return Starlette(routes=[
            Route("/health", self._health, methods=["GET"]),
            Route("/enter", self._enter, methods=["GET"]),
            Route("/", self._index, methods=["GET"]),
            Route("/upload", self._upload, methods=["POST"]),
        ])

    def _auth_cookie(self, request: Request):
        """Session cookie (the psk) -> connector, or None."""
        tok = request.cookies.get(SESSION_COOKIE, "")
        return self._conns.authenticate(tok) if tok else None

    def _project_name(self) -> str:
        if self._get_project is None:
            return ""
        try:
            p = self._get_project()
        except Exception:                 # noqa: BLE001 — a bad accessor != break the page
            return ""
        if p is None:
            return ""
        if isinstance(p, dict):
            return str(p.get("name") or "")
        return str(getattr(p, "name", p) or "")

    async def _health(self, request: Request):
        return JSONResponse({"ok": True, "name": self._name, "version": self._version})

    async def _enter(self, request: Request):
        k = request.query_params.get("k", "")
        conn = self._conns.authenticate(k) if k else None
        if conn is None:
            return HTMLResponse(_invalid_html(self._name), status_code=401)
        resp = RedirectResponse(url="/", status_code=302)
        # HttpOnly so page JS can't read the psk; SameSite=Lax; NOT Secure (plain http).
        resp.set_cookie(SESSION_COOKIE, k, httponly=True, samesite="lax", path="/")
        return resp

    async def _index(self, request: Request):
        conn = self._auth_cookie(request)
        if conn is None:
            return HTMLResponse(_pair_html(self._name), status_code=200)
        return HTMLResponse(_page_html(self._name, self._project_name()),
                            status_code=200)

    async def _upload(self, request: Request):
        conn = self._auth_cookie(request)
        if conn is None:
            return JSONResponse({"ok": False, "error": "not paired"}, status_code=401)
        if scope_rank(conn.scope) < scope_rank("control"):
            return JSONResponse({"ok": False, "error": "insufficient scope"},
                                status_code=403)
        body = await _json_body(request)
        category = str(body.get("category") or "generic")
        if category not in CATEGORIES:
            category = "generic"
        label = str(body.get("label") or "").strip()
        raw = _decode_image(body.get("data_b64"))
        if raw is None:
            return JSONResponse({"ok": False, "error": "bad image data"},
                                status_code=400)
        if not raw:
            return JSONResponse({"ok": False, "error": "empty upload"}, status_code=400)
        try:
            result = await run_in_threadpool(
                self._surface.dispatch, "media.add_photo",
                {"category": category, "label": label, "data": raw},
                scope=conn.scope, confirm=False)
        except ScopeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        except ControlError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "result": result})


# -- helpers -----------------------------------------------------------------
async def _json_body(request: Request) -> dict:
    try:
        body = await request.body()
        data = json.loads(body) if body else {}
        return data if isinstance(data, dict) else {}   # a scalar/array body -> {}
    except (ValueError, TypeError):
        return {}


def _decode_image(data_b64) -> "bytes | None":
    """Base64 -> bytes. Tolerates a ``data:...;base64,`` URL prefix (FileReader's
    readAsDataURL) even though the JS strips it. None on undecodable input."""
    if not data_b64:
        return b""
    s = str(data_b64)
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    try:
        return base64.b64decode(s, validate=False)
    except (ValueError, binascii.Error):
        return None


# -- server-rendered pages (inline css + vanilla js; NO external CDN) --------
_BANNER = ("⚠ Unencrypted connection — anyone on this network could view "
           "what you send. Fine on a trusted network.")

_STYLE = """
:root{color-scheme:light dark;--bg:#0f1216;--card:#181d24;--line:#273140;
--fg:#e7edf5;--muted:#8b95a4;--accent:#3b82f6;--accent2:#2563eb;--warn-bg:#3a2a12;
--warn-fg:#ffd8a8;--warn-line:#7a5a26;--ok:#22c55e}
@media (prefers-color-scheme:light){:root{--bg:#f2f4f8;--card:#ffffff;--line:#dde3ec;
--fg:#0e1621;--muted:#5b6675;--warn-bg:#fff4e5;--warn-fg:#8a5a00;--warn-line:#f0d9b0}}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,
sans-serif;background:var(--bg);color:var(--fg);-webkit-text-size-adjust:100%}
.wrap{max-width:560px;margin:0 auto;padding:16px 16px 48px}
.banner{background:var(--warn-bg);color:var(--warn-fg);border:1px solid var(--warn-line);
border-radius:12px;padding:11px 13px;font-size:13px;line-height:1.35;margin-bottom:16px;
position:sticky;top:8px;z-index:5}
.head{background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:16px;
padding:18px 18px 20px;margin-bottom:16px;color:#fff}
.head .lbl{font-size:12px;text-transform:uppercase;letter-spacing:.08em;opacity:.85}
.head .name{font-size:26px;font-weight:800;margin-top:3px;line-height:1.15;
word-break:break-word}
.head .name.none{opacity:.85;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:16px;margin-bottom:16px}
.tile{display:flex;flex-direction:column;align-items:center;justify-content:center;
gap:8px;padding:30px 16px;border:2px dashed var(--line);border-radius:16px;
background:var(--card);cursor:pointer;text-align:center;transition:border-color .15s,
background .15s}
.tile:active{border-color:var(--accent);background:rgba(59,130,246,.08)}
.tile .ico{font-size:44px;line-height:1}
.tile .t{font-size:18px;font-weight:700}
.tile .s{font-size:13px;color:var(--muted)}
.tile input{display:none}
label.f{display:block;font-size:13px;color:var(--muted);margin:14px 0 6px;font-weight:600}
select,input.cap{width:100%;padding:13px 12px;font-size:16px;border-radius:12px;
border:1px solid var(--line);background:var(--card);color:var(--fg);appearance:none}
select:focus,input.cap:focus{outline:2px solid var(--accent);border-color:var(--accent)}
.strip{display:flex;gap:10px;overflow-x:auto;padding:4px 0 6px;-webkit-overflow-scrolling:touch}
.strip:empty::after{content:"No photos sent yet";color:var(--muted);font-size:13px}
.thumb{position:relative;flex:0 0 auto;width:76px;height:76px;border-radius:12px;
overflow:hidden;border:1px solid var(--line);background:#000}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.thumb .badge{position:absolute;left:0;right:0;bottom:0;font-size:10px;text-align:center;
padding:2px 0;background:rgba(0,0,0,.55);color:#fff}
.thumb.pending img{opacity:.45}
.thumb .spin{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
font-size:22px}
.thumb.ok::before{content:"\\2713";position:absolute;top:3px;right:4px;color:#fff;
background:var(--ok);border-radius:50%;width:17px;height:17px;font-size:12px;
display:flex;align-items:center;justify-content:center}
.thumb.err::before{content:"!";position:absolute;top:3px;right:4px;color:#fff;
background:#ef4444;border-radius:50%;width:17px;height:17px;font-size:12px;font-weight:800;
display:flex;align-items:center;justify-content:center}
.msg{font-size:13px;margin-top:10px;min-height:18px}
.msg.err{color:#ef4444}.msg.ok{color:var(--ok)}
h2{font-size:14px;margin:0 0 10px;color:var(--muted);font-weight:700;
text-transform:uppercase;letter-spacing:.05em}
"""


def _shell(name: str, body: str, script: str = "") -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content=\"width=device-width,initial-scale=1,"
        "viewport-fit=cover\">"
        "<meta name=robots content=noindex>"
        f"<title>{html.escape(name)} — photo upload</title>"
        f"<style>{_STYLE}</style></head><body><div class=wrap>"
        f"<div class=banner>{html.escape(_BANNER)}</div>"
        f"{body}</div>{script}</body></html>"
    )


def _page_html(name: str, project: str) -> str:
    if project:
        head = (f"<div class=head><div class=lbl>Uploading to project</div>"
                f"<div class=name>{html.escape(project)}</div></div>")
    else:
        head = ("<div class=head><div class=lbl>Active project</div>"
                "<div class=\"name none\">No active project</div></div>")
    opts = "".join(
        f"<option value={c}{' selected' if c == 'generic' else ''}>"
        f"{c.capitalize()}</option>" for c in CATEGORIES)
    body = (
        f"{head}"
        "<div class=card>"
        "<label class=tile id=tile>"
        "<div class=ico>\U0001f4f7</div>"
        "<div class=t>Upload photo</div>"
        "<div class=s>Take a photo or choose from your library</div>"
        "<input id=file type=file accept=image/* capture=environment>"
        "</label>"
        f"<label class=f for=category>Category</label>"
        f"<select id=category>{opts}</select>"
        "<label class=f for=caption>Caption (optional)</label>"
        "<input class=cap id=caption type=text maxlength=200 "
        "placeholder=\"e.g. sample after 2h anneal\">"
        "<div class=msg id=msg></div>"
        "</div>"
        "<div class=card><h2>Recently sent</h2>"
        "<div class=strip id=strip></div></div>"
    )
    return _shell(name, body, f"<script>{_UPLOAD_JS}</script>")


def _pair_html(name: str) -> str:
    body = ("<div class=head><div class=lbl>Photo companion</div>"
            f"<div class=name>{html.escape(name)}</div></div>"
            "<div class=card><h2>Not connected</h2>"
            "<p style=\"color:var(--muted);font-size:14px;line-height:1.5;margin:0\">"
            "Open <b>Connections → Phone companion</b> in the desktop app and scan "
            "the QR code (or open the link it shows) to pair this phone.</p></div>")
    return _shell(name, body)


def _invalid_html(name: str) -> str:
    body = ("<div class=head><div class=lbl>Photo companion</div>"
            f"<div class=name>{html.escape(name)}</div></div>"
            "<div class=card><h2>Invalid or expired link</h2>"
            "<p style=\"color:var(--muted);font-size:14px;line-height:1.5;margin:0\">"
            "This pairing link is no longer valid. Get a fresh QR code from "
            "<b>Connections → Phone companion</b> in the desktop app.</p></div>")
    return _shell(name, body)


_UPLOAD_JS = r"""
(function(){
  var file=document.getElementById('file'),
      cat=document.getElementById('category'),
      cap=document.getElementById('caption'),
      strip=document.getElementById('strip'),
      msg=document.getElementById('msg');
  function say(t,cls){msg.textContent=t||'';msg.className='msg'+(cls?' '+cls:'');}
  file.addEventListener('change',function(){
    var f=file.files&&file.files[0]; if(!f) return;
    var r=new FileReader();
    r.onload=function(){
      var url=r.result, b64=String(url).split(',')[1]||'';
      send(b64,url,cat.value,cap.value.trim());
    };
    r.onerror=function(){say('Could not read that file.','err');};
    r.readAsDataURL(f);
    file.value='';                       // allow re-picking the same file
  });
  function thumb(url,label){
    var d=document.createElement('div'); d.className='thumb pending';
    d.innerHTML='<img src=\"'+url+'\"><div class=spin>…</div>'+
      '<div class=badge>'+label+'</div>';
    strip.insertBefore(d,strip.firstChild); return d;
  }
  function send(b64,url,category,caption){
    if(!b64){say('That file was empty.','err');return;}
    say('Sending…');
    var t=thumb(url,category);
    fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({category:category,label:caption,data_b64:b64})})
      .then(function(res){return res.json().then(function(j){return {s:res.status,j:j};});})
      .then(function(o){
        var sp=t.querySelector('.spin'); if(sp) sp.remove();
        t.className='thumb'+(o.j&&o.j.ok?' ok':' err');
        if(o.j&&o.j.ok){say('Sent ✓','ok');cap.value='';}
        else{say((o.j&&o.j.error)||('Upload failed ('+o.s+')'),'err');}
      })
      .catch(function(){
        var sp=t.querySelector('.spin'); if(sp) sp.remove();
        t.className='thumb err'; say('Network error — is the app still running?','err');
      });
  }
})();
"""
