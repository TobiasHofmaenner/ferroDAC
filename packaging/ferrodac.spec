# PyInstaller spec for ferroDAC.
#
# Build ON WINDOWS (or a Windows CI runner), from the repo root:
#
#     pip install -r requirements.txt pyinstaller
#     pyinstaller packaging/ferrodac.spec
#
# Result: dist/ferroDAC.exe  (one-file, windowed, x86-64).
#
# NOTE: PyInstaller does not cross-compile. Running this on Linux produces a
# Linux binary, not a Windows .exe. OCR (camera text detection) additionally
# needs Tesseract installed separately on the target machine; it degrades
# gracefully if `tesseract` is not on PATH.

import os
import sys

from PyInstaller.utils.hooks import (collect_all, collect_submodules,
                                     collect_data_files, collect_dynamic_libs)

# The spec lives in packaging/; the app lives one level up. SPECPATH is the
# directory containing this spec file (injected by PyInstaller).
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# The generated gRPC contract (ferrodac_contract) lives outside the ferrodac
# package, under server/gen — put it on the path *before* any collect_submodules
# so it's importable while PyInstaller analyses ferrodac.net.
GEN = os.path.join(ROOT, "server", "gen")
if GEN not in sys.path:
    sys.path.insert(0, GEN)

# Device drivers are discovered dynamically (the registry imports them at
# runtime), so PyInstaller's static analysis can't see them — pull the whole
# package in explicitly so every driver/source is bundled.
hiddenimports = collect_submodules("ferrodac")
# collect_submodules("ferrodac") has proven UNRELIABLE on the Windows CI runner:
# it silently returned a list WITHOUT ferrodac.devices.*, so the drivers (incl.
# the sim devices) were never bundled and the app saw zero devices. List the
# builtin drivers explicitly so they're guaranteed in the frozen app regardless.
hiddenimports += [f"ferrodac.devices.{m}"
                  for m in ("camera", "fake", "qms200", "tpg256a")]
# pyqtgraph loads a lot lazily; include its submodules but skip the optional 3D
# OpenGL package (needs PyOpenGL, which we don't use).
hiddenimports += collect_submodules(
    "pyqtgraph", filter=lambda name: not name.startswith("pyqtgraph.opengl")
)
# pyserial picks its port-enumeration backend at runtime by platform, so the
# Windows one (serial.tools.list_ports_windows) is invisible to static analysis;
# pull every serial submodule in or the frozen app can't list COM ports at all.
hiddenimports += collect_submodules("serial")
hiddenimports += [
    "PySide6.QtMultimedia",          # camera capture
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetwork",             # the multimedia ffmpeg backend pulls this
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtSvg",
    "PySide6.QtWebEngineWidgets",    # in-app document view (Docs / editor / collab)
    "PySide6.QtWebEngineCore",       # → pulls the QtWebEngineProcess + resources hook
    "PySide6.QtWebChannel",          # the Qt↔JS bridge for the editor
    "cv2",                           # vision preprocessing
    "psutil",                        # Timeline perf HUD (runtime import in PerfStrip)
]

datas = collect_data_files("pyqtgraph")
# Bundle the window/taskbar icon so the running app can load it.
datas += [(os.path.join(ROOT, "ferrodac", "assets", "app.png"), "ferrodac/assets")]

# The in-app document view (Docs / editor / live collaboration) is a QtWebEngine
# page that loads the OFFLINE web bundle under ferrodac/ui/web/dist (built by
# esbuild, committed — no CDN at runtime). Ship the whole tree (HTML/JS/CSS + the
# KaTeX fonts), preserving its path so docs.py's `_DIST` resolves inside the frozen
# app. Without this the Docs panel can't load its page.
_WEB = os.path.join(ROOT, "ferrodac", "ui", "web", "dist")
for _dp, _dn, _fn in os.walk(_WEB):
    for _f in _fn:
        datas += [(os.path.join(_dp, _f), os.path.relpath(_dp, ROOT))]

# The "General" OCR engine (RapidOCR / ONNX Runtime) — bundle its models + libs.
# Wrapped so a packaging hiccup can't break the build; the engine degrades to
# Tesseract at runtime if it isn't bundled.
binaries = []

# Local data store (DESIGN §7.4) — NEW since the last release, so untested in a
# frozen build. zarr 3.x registers codecs via entry points and numcodecs ships
# compiled codecs, both of which PyInstaller misses without collect_all. Wrapped
# so a bundling hiccup degrades the store (app.py guards it) rather than failing
# the build.
try:
    for _pkg in ("zarr", "numcodecs"):
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
except Exception as exc:                                   # noqa: BLE001
    print(f"[ferrodac.spec] local store not fully bundled: {exc}")

try:
    hiddenimports += collect_submodules("rapidocr_onnxruntime")
    hiddenimports += collect_submodules("onnxruntime")
    hiddenimports += ["pyclipper", "shapely"]
    datas += collect_data_files("rapidocr_onnxruntime")   # the .onnx models + yaml
    datas += collect_data_files("onnxruntime")
    binaries += collect_dynamic_libs("onnxruntime")
    binaries += collect_dynamic_libs("shapely")
except Exception as exc:                                   # noqa: BLE001
    print(f"[ferrodac.spec] general OCR engine not bundled: {exc}")

# Hub networking (gRPC). The contract stubs live under server/gen (outside the
# ferrodac package); bundle them plus grpc + the protobuf runtime. Wrapped so a
# bundling hiccup can't break the build — the feature degrades via
# net.GRPC_AVAILABLE if grpc isn't importable at runtime.
try:
    hiddenimports += collect_submodules("ferrodac_contract")
    grpc_datas, grpc_bins, grpc_hidden = collect_all("grpc")
    datas += grpc_datas
    binaries += grpc_bins
    hiddenimports += grpc_hidden
    hiddenimports += collect_submodules("google.protobuf")
except Exception as exc:                                   # noqa: BLE001
    print(f"[ferrodac.spec] hub networking not bundled: {exc}")

# Keep only PySide6: exclude the other Qt bindings so qtpy resolves to PySide6
# and we don't bundle a second toolkit (opencv-python-headless also avoids
# shipping its own Qt).
excludes = [
    "PyQt5", "PyQt6", "PySide2",
    "OpenGL", "pyqtgraph.opengl", "pyqtgraph.examples",
    "tkinter", "matplotlib", "IPython", "pytest", "numpy.testing",
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],     # absolute-import launcher (see main.py)
    pathex=[ROOT, GEN],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ferroDAC",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,        # set True for a debug console
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, "packaging", "app.ico"),
)
