"""Per-device lab-journal metadata.

What the hardware doesn't report — calibration, asset tag, manufacturer, a serial for
dumb devices — the user fills in here, keyed by the device's stable id. For the /dev
journal these are merged OVER the descriptor:

    user metadata  >  device-reported (descriptor)  >  (future) device DB

so a Keithley that self-reports everything needs no entry, while a humble gauge gets its
calibration typed in once.
"""
import json
import os

# fields a user can fill/override per device (notes is user-only; the rest shadow the
# matching descriptor field)
JOURNAL_FIELDS = ("manufacturer", "model", "serial", "firmware",
                  "cal_date", "cal_due", "cal_cert", "asset_tag", "notes")


def device_key(descriptor) -> str:
    """The stable id we key metadata by — the serial if the device reports one, else
    its instance id."""
    return (getattr(descriptor, "hardware_id", None)
            or getattr(descriptor, "instance_id", "") or "")


class DeviceMeta:
    """A small JSON store of per-device user metadata (lab-wide, not per-project)."""

    def __init__(self, path: str):
        self.path = path
        self._data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def get(self, key: str) -> dict:
        return dict(self._data.get(key, {}))

    def set(self, key: str, fields: dict) -> None:
        clean = {k: v.strip() for k, v in (fields or {}).items()
                 if k in JOURNAL_FIELDS and isinstance(v, str) and v.strip()}
        if clean:
            self._data[key] = clean
        else:
            self._data.pop(key, None)           # cleared → drop the entry
        self.save()


def merge_device_info(descriptor, meta: dict) -> dict:
    """Resolved journal fields for a device: user metadata wins, else what the device
    reported. `meta` is the user dict from DeviceMeta.get(device_key(descriptor))."""
    d = descriptor
    out = {
        "name": getattr(d, "name", ""),
        "driver": getattr(d, "driver", ""),
        "manufacturer": getattr(d, "manufacturer", None),
        "model": getattr(d, "model", None),
        "serial": getattr(d, "hardware_id", None),
        "firmware": getattr(d, "firmware", None),
        "cal_date": getattr(d, "cal_date", None),
        "cal_due": getattr(d, "cal_due", None),
        "cal_cert": getattr(d, "cal_cert", None),
        "asset_tag": getattr(d, "asset_tag", None),
        "notes": "",
    }
    for k in JOURNAL_FIELDS:
        v = (meta or {}).get(k)
        if v:                                   # user-entered value overrides the device
            out[k] = v
    return out
