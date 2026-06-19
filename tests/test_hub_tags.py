"""The hub persists tags (JSON backend) so it's authoritative across a restart.

Tags used to live only in RAM on the hub — lost on restart, unlike the data
(durable in Zarr). This pins the JSON TagBackend: persist on change (atomically),
reload on construction, LWW edits + delete tombstones survive.
"""

import os
import tempfile

import pytest

pytest.importorskip("ferrodac_contract.v1.data_plane_pb2")
from ferrodac_contract.v1 import data_plane_pb2 as pb       # noqa: E402
from hub.core import Hub                                     # noqa: E402


def test_hub_persists_tags_across_restart():
    path = os.path.join(tempfile.mkdtemp(), "tags.json")
    h = Hub(tags_path=path)
    assert h.publish_tag(pb.Tag(id="t1", t=1000.0, label="start", version=1))
    assert h.publish_tag(pb.Tag(id="t2", t=1010.0, label="spike", version=1))
    assert os.path.exists(path)                  # _mark_dirty flushes sync (no loop)

    # a fresh hub (restart) reloads them → authoritative
    assert {t.label for t in Hub(tags_path=path).tag_snapshot()} == {"start", "spike"}

    # an LWW edit persists as the new current
    h.publish_tag(pb.Tag(id="t1", t=1000.0, label="start (edited)", version=2))
    h2 = Hub(tags_path=path)
    assert h2._tags["t1"].label == "start (edited)" and h2._tags["t1"].version == 2

    # a delete tombstone persists (so the delete propagates after a restart)
    h2.delete_tag("t2", version=2)
    h3 = Hub(tags_path=path)
    assert h3._tags["t2"].deleted is True

    # a stale write is rejected — and not persisted as current
    assert not h3.publish_tag(pb.Tag(id="t1", t=1.0, label="stale", version=1))
    assert Hub(tags_path=path)._tags["t1"].label == "start (edited)"

    # project membership (the curation lens) persists too
    h3.publish_tag(pb.Tag(id="t3", t=2.0, label="grouped", version=1,
                          projects=["projA", "projB"]))
    assert list(Hub(tags_path=path)._tags["t3"].projects) == ["projA", "projB"]


def test_no_path_is_in_memory_only():
    h = Hub()                                    # no persistence configured
    assert h.publish_tag(pb.Tag(id="x", t=1.0, version=1))
    assert len(h.tag_snapshot()) == 1            # works in RAM, just not durable
