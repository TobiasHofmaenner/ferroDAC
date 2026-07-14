"""Control-surface MEDIA verbs (phone-companion backend) dispatched against a REAL
MainWindow via the shared `control_surface` fixture.

media.add_photo stores an uploaded photo VERBATIM (bit-exact — a phone JPEG is
already compressed, DESIGN §9.1) as a media file + an immutable media tag, and
media.categories exposes the fixed setup/sample/result/generic set. Each verb is
exercised end-to-end through ControlSurface.dispatch (scope gate + JSON-ability +
the true MediaService/markers contract). Marked `ui` (they build Qt).
"""

import base64
import json
import os

import pytest

from ferrodac.core.control import ControlError, ScopeError
from ferrodac.core.tag import MEDIA


def _json_able(value):
    json.dumps(value)          # raises if a QObject/QImage/set/Enum/Path leaked
    return value


@pytest.mark.ui
def test_media_categories_lists_the_fixed_set(control_surface):
    _, s = control_surface
    cats = _json_able(s.dispatch("media.categories", scope="read"))
    assert cats == ["setup", "sample", "result", "generic"]


@pytest.mark.ui
def test_add_photo_writes_bytes_verbatim_and_tags(control_surface):
    w, s = control_surface
    proj = w._project_mgr.active
    assert proj is not None
    # deliberately NOT a valid image: proves the file is written as-is, never
    # routed through QImage.save (which would reject/re-encode it).
    raw = b"\xff\xd8\xff\xe0not-a-real-jpeg-stored-verbatim\x00\x01\x02"
    out = _json_able(s.dispatch(
        "media.add_photo",
        {"data_b64": base64.b64encode(raw).decode(),
         "category": "sample", "comment": "left electrode", "ext": "jpg"},
        scope="control"))

    assert out["category"] == "sample"
    assert out["file"] == out["relpath"]
    assert out["relpath"].startswith("media")          # project-relative
    assert out["relpath"].endswith(".jpg")
    assert os.path.isfile(out["path"])
    # the file lives inside the ACTIVE project's media dir
    assert os.path.abspath(out["path"]).startswith(os.path.abspath(proj.media_dir))
    with open(out["path"], "rb") as fh:
        assert fh.read() == raw                          # BYTE-EXACT, not re-encoded

    tag = w.dashboard.markers.get(out["tag_id"])
    assert tag is not None and tag.kind == MEDIA
    assert tag.label == "\U0001F4F7 sample"              # default "📷 <category>"
    assert tag.immutable is True                          # a captured instant is fixed
    assert tag.payload["file"] == out["relpath"]
    assert tag.payload["source"] == "phone"
    assert tag.payload["format"] == "jpg"
    assert tag.payload["category"] == "sample"
    assert tag.payload["comment"] == "left electrode"


@pytest.mark.ui
def test_add_photo_defaults_category_and_sanitizes_ext(control_surface):
    w, s = control_surface
    # no category -> generic ; ".PNG" -> "png"
    out = s.dispatch("media.add_photo",
                     {"data_b64": base64.b64encode(b"\x89PNGfake").decode(),
                      "ext": ".PNG"}, scope="control")
    assert out["category"] == "generic"
    assert out["relpath"].endswith(".png")
    assert w.dashboard.markers.get(out["tag_id"]).payload["format"] == "png"
    assert w.dashboard.markers.get(out["tag_id"]).label == "\U0001F4F7 generic"
    # an unknown extension falls back to jpg
    out2 = s.dispatch("media.add_photo",
                      {"data_b64": base64.b64encode(b"GIF89a").decode(),
                       "ext": "gif"}, scope="control")
    assert out2["relpath"].endswith(".jpg")


@pytest.mark.ui
def test_add_photo_custom_label_overrides_default(control_surface):
    w, s = control_surface
    out = s.dispatch("media.add_photo",
                     {"data_b64": base64.b64encode(b"img").decode(),
                      "category": "result", "label": "final wafer"}, scope="control")
    assert w.dashboard.markers.get(out["tag_id"]).label == "final wafer"


@pytest.mark.ui
def test_add_photo_rejects_bad_base64(control_surface):
    _, s = control_surface
    with pytest.raises(ControlError):
        s.dispatch("media.add_photo", {"data_b64": "not@@base64!!"}, scope="control")


@pytest.mark.ui
def test_add_photo_rejects_unknown_category(control_surface):
    _, s = control_surface
    data = base64.b64encode(b"x").decode()
    with pytest.raises(ControlError):
        s.dispatch("media.add_photo", {"data_b64": data, "category": "bogus"},
                   scope="control")


@pytest.mark.ui
def test_add_photo_requires_data_param(control_surface):
    _, s = control_surface
    with pytest.raises(ControlError):
        s.dispatch("media.add_photo", {}, scope="control")


@pytest.mark.ui
def test_add_photo_requires_control_scope(control_surface):
    _, s = control_surface
    data = base64.b64encode(b"x").decode()
    with pytest.raises(ScopeError):
        s.dispatch("media.add_photo", {"data_b64": data}, scope="read")
