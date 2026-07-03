"""ferroDAC — extensible, local-first lab data-acquisition platform."""

__version__ = "0.0.0+dev"    # placeholder — release CI stamps the git tag here


def _git_describe() -> str:
    """Dev checkouts: a live version straight from git (`v0.52.0-3-gabc12` →
    `0.52.0-3-gabc12`), so nothing has to be bumped by hand. Release builds never
    get here (CI stamps ``__version__`` from the tag before freezing); a frozen
    exe or a checkout without git keeps the placeholder."""
    import os
    import subprocess
    import sys
    if getattr(sys, "frozen", False):      # a frozen exe has no repo (and a child
        return ""                          # process could flash a console window)
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=3)
        v = (out.stdout or "").strip()
        return v[1:] if v.startswith("v") else v
    except Exception:
        return ""


if __version__.startswith("0.0.0"):
    __version__ = _git_describe() or __version__
