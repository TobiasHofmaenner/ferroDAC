"""Shared UI helpers (colours, formatting) used across panels and the shell."""

from __future__ import annotations

from ..core.device import Status

CHANNEL_COLORS = [
    "#4fc3f7", "#ff8a65", "#81c784", "#ba68c8", "#ffd54f", "#e57373",
    "#64b5f6", "#a1887f", "#4db6ac", "#f06292",
]

STATUS_COLORS = {
    Status.DISCOVERED: "#7f8a99",
    Status.CONNECTING: "#ffd54f",
    Status.CONNECTED: "#69db7c",
    Status.ERROR: "#ff6b6b",
    Status.DISCONNECTED: "#7f8a99",
}

# Stable colour per channel key so a card, a chart curve and an LCD all match.
_COLOR_MAP: dict[str, str] = {}


def color_for(key: str) -> str:
    if key not in _COLOR_MAP:
        _COLOR_MAP[key] = CHANNEL_COLORS[len(_COLOR_MAP) % len(CHANNEL_COLORS)]
    return _COLOR_MAP[key]


def fmt(value, unit: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):                # bool is an int — handle first
        return "on" if value else "off"
    if not isinstance(value, (int, float)):    # ENUM strings, etc.
        s = str(value)
        return f"{s} {unit}".rstrip() if unit else s
    if value != value:                         # NaN
        return "—"
    a = abs(value)
    s = f"{value:.3e}" if (a != 0 and (a < 1e-3 or a >= 1e4)) else f"{value:.4g}"
    return f"{s} {unit}".rstrip()


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
