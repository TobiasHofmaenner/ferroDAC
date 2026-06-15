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

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# The spec lives in packaging/; the app lives one level up. SPECPATH is the
# directory containing this spec file (injected by PyInstaller).
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# Device drivers are discovered dynamically (pkgutil.iter_modules over
# ferrodac.devices), so PyInstaller's static analysis can't see them — pull the
# whole package in explicitly so every driver/source is bundled.
hiddenimports = collect_submodules("ferrodac")
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
    "cv2",                           # vision preprocessing
]

datas = collect_data_files("pyqtgraph")
# Bundle the window/taskbar icon so the running app can load it.
datas += [(os.path.join(ROOT, "ferrodac", "assets", "app.png"), "ferrodac/assets")]

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
    pathex=[ROOT],
    binaries=[],
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
