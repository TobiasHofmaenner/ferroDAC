#!/usr/bin/env python3
"""Entry point for the ferroDAC app.

Used by PyInstaller (and `python main.py`). It must use an **absolute** import:
PyInstaller runs the entry script as top-level ``__main__`` with no parent
package, so ``ferrodac/__main__.py``'s relative import can't be the frozen
entry point. (`python -m ferrodac` still works via ferrodac/__main__.py.)
"""

from ferrodac.ui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
