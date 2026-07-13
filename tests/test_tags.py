"""Immutable tags (fixed point-in-time events) — DESIGN §7.3.

A photo documents a captured INSTANT; a device/processor-emitted alarm is a fact
that happened WHEN it happened. Their time must not be dragged. User annotations
and recording spans stay movable (a REC marker moving re-slices its clip, §9.3).
Qt-free — the MarkerModel + tag entity only."""

from ferrodac.core.markers import MarkerModel, is_movable
from ferrodac.core.tag import (MEDIA, ORIGIN_DEVICE, ORIGIN_PROCESSOR,
                               ORIGIN_SYSTEM, ORIGIN_USER, RECORDING,
                               marker_from_dict, marker_to_dict)


def test_photo_and_emitted_events_are_immutable():
    m = MarkerModel()
    photo = m.get(m.add(100.0, kind=MEDIA, label="📷", payload={"file": "media/x.png"}))
    alarm = m.get(m.add(5.0, kind="alarm", origin_kind=ORIGIN_DEVICE, label="over-temp"))
    proc = m.get(m.add(7.0, kind="alarm", origin_kind=ORIGIN_PROCESSOR, label="fit-fail"))
    sysev = m.get(m.add(9.0, origin_kind=ORIGIN_SYSTEM, label="startup"))
    for ev in (photo, alarm, proc, sysev):
        assert ev.immutable and not is_movable(ev)


def test_user_tags_and_recordings_stay_movable():
    m = MarkerModel()
    note = m.get(m.add(50.0, label="note"))
    rec = m.get(m.add(10.0, kind=RECORDING, t_end=20.0, label="REC"))
    for ev in (note, rec):
        assert not ev.immutable and is_movable(ev)
    assert note.origin_kind == ORIGIN_USER


def test_move_refuses_immutable_but_allows_movable():
    m = MarkerModel()
    pid = m.add(100.0, kind=MEDIA, payload={"file": "media/x.png"})
    m.move(pid, 999.0)
    assert m.get(pid).t == 100.0                   # immutable → drag refused

    tid = m.add(50.0, label="note")
    m.move(tid, 60.0)
    assert m.get(tid).t == 60.0                    # user tag → drag honored


def test_explicit_immutable_overrides_the_default_rule():
    m = MarkerModel()
    pinned = m.get(m.add(30.0, label="pinned note", immutable=True))
    assert pinned.immutable and not is_movable(pinned)   # a user CAN pin an annotation
    loose = m.get(m.add(30.0, kind=MEDIA, payload={"file": "m/x.png"}, immutable=False))
    assert not loose.immutable and is_movable(loose)   # ...and un-pin a photo (explicit wins)
    m.move(loose.id, 40.0)
    assert m.get(loose.id).t == 40.0               # the un-pinned photo actually drags


def test_immutable_persists_and_legacy_derives():
    m = MarkerModel()
    photo = m.get(m.add(100.0, kind=MEDIA, payload={"file": "media/x.png"}))
    d = marker_to_dict(photo)
    assert d["immutable"] is True
    assert marker_from_dict(d).immutable is True    # round-trips
    # a tag persisted BEFORE the flag existed: derived immovable from its kind
    legacy = {k: v for k, v in d.items() if k != "immutable"}
    legacy["id"] = "legacy-1"
    lm = marker_from_dict(legacy)
    assert lm.immutable is None and not is_movable(lm)   # field absent → derive by kind
