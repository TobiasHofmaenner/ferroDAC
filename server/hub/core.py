"""Hub core: the in-memory catalog and the live fan-out.

No storage in Milestone 1 — the hub holds which devices are *currently* publishing
(announced by an agent's Session) and fans each ReadingBatch out to the viewers
subscribed to it. Devices vanish when their agent's session ends (→ placeholder
on viewers, §6.1). Everything here is pure asyncio, single event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid as _uuid

from google.protobuf import json_format

from ferrodac_contract.v1 import data_plane_pb2 as pb

log = logging.getLogger("hub")

CONTRACT_VERSION = 1   # wire contract version; MIRRORED in ferrodac/net/__init__.py + the .proto — keep equal
HUB_VERSION = "0.1.0"


def _offer(q: "asyncio.Queue", item) -> None:
    """Non-blocking enqueue; drop the oldest on overflow. The live tier is
    expendable by design — a slow viewer must never block ingest or another
    viewer. (Durability is the recorded-bundle path, not this one.)"""
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass


class Subscriber:
    """A live viewer stream. `refs` is the set of (device_uuid, source_id) it
    wants, or None for 'everything'."""

    __slots__ = ("queue", "refs")

    def __init__(self, refs: "set[tuple[str, str]] | None"):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.refs = refs

    def wants(self, device_uuid: str, source_id: str) -> bool:
        return self.refs is None or (device_uuid, source_id) in self.refs


class DocMember:
    """One client's membership in one doc room. `queue` is the client's single
    out-stream (a DocServerMsg queue shared across every room it joined); `seq` is
    its join order in this room (leader/seeder election picks the lowest)."""

    __slots__ = ("queue", "actor", "seq")

    def __init__(self, queue: "asyncio.Queue", actor: str):
        self.queue = queue
        self.actor = actor
        self.seq = 0


class DocRoom:
    """A live collaborative-editing room for one document. The hub never parses the
    Yjs bytes — it keeps a compacted `baseline` (the seed/last compaction) plus a
    `log` of incrementals since, and replays baseline+log to each joiner. `leader`
    (lowest seq) is the SOLE writer of the materialised `.md`."""

    __slots__ = ("doc_id", "members", "seeded", "seeder", "baseline", "log",
                 "leader", "_seq", "md_path", "blob_path")

    def __init__(self, doc_id: str, md_path: str, blob_path: str):
        self.doc_id = doc_id
        self.members: list[DocMember] = []        # join order preserved
        self.seeded = False                       # has the CRDT been seeded yet?
        self.seeder: "DocMember | None" = None    # who builds the doc from text
        self.baseline: bytes = b""                # compacted full-state Yjs update
        self.log: list[bytes] = []                # incrementals since the baseline
        self.leader: "DocMember | None" = None    # sole .md materialiser
        self._seq = 0
        self.md_path = md_path                    # materialised text on the server
        self.blob_path = blob_path                # the .ycrdt baseline blob


class Hub:
    def __init__(self, tags_path: "str | None" = None,
                 projects_dir: "str | None" = None, gitea=None,
                 backup_dir: "str | None" = None) -> None:
        self.gitea = gitea                          # transparent dial: auto-provision repos
        self._devices: dict[str, pb.DeviceDescriptor] = {}
        self._subs: set[Subscriber] = set()
        self._frame_subs: set[Subscriber] = set()   # WatchFrames watchers (§9)
        self._frame_demand: dict = {}               # (uuid, source) -> watcher count
        self._agent_outq: dict = {}                 # device uuid -> Ingest out-queue
        self._agent_conns: dict = {}                # agent_id -> Ingest out-queue (Hello);
        #   routes to a client with NO active device (remote-add targets the agent)
        self._pending_cmds: dict = {}               # request_id -> Future[Ack] (control §5.3)
        self._watchers: set[asyncio.Queue] = set()
        # Tags (DESIGN §7.3): a durable, reliable store keyed by id, merged
        # last-write-wins on version, tombstones kept so deletes propagate.
        # Held in RAM for fan-out; PERSISTED to a JSON TagBackend so the hub is
        # authoritative — tags survive a restart (the data already does, via Zarr).
        self._tags: dict[str, pb.Tag] = {}
        self._tag_watchers: set[asyncio.Queue] = set()
        self._tags_path = tags_path
        self._save_pending = False
        self._load_tags()
        # Projects (DESIGN §8.1): a SHARED EXPERIMENT INDEX — same reliable, LWW,
        # tombstoned model as tags (NOT a file store). But the hub stores each one
        # as a REAL project FOLDER (the same layout as a local project, written by
        # ferrodac.core.projects.Project) under `projects_dir`, so the dir is
        # mountable and a project opens as-if-local. The wire record is just the
        # transport; the folder is the source of truth. `_projects` is the RAM
        # fan-out cache (id -> record), rebuilt by scanning the folders on load.
        self._projects: dict[str, pb.Project] = {}
        self._project_watchers: set[asyncio.Queue] = set()
        self._projects_dir = projects_dir
        self._load_projects()
        # Authoritative project backup (DESIGN §20): the hub mirrors each project folder
        # one-way + incrementally into a backend dir. Off unless backup_dir is set.
        self._backup = None
        self._backup_dirty: set[str] = set()
        self._backup_lock = threading.Lock()
        if backup_dir and projects_dir:
            from .backup import ProjectBackup
            self._backup = ProjectBackup(projects_dir, backup_dir)
            self._backup_dirty = set(self._projects)     # initial full mirror on startup
            log.info("project backup → %s", backup_dir)
        # Docs (DESIGN §10.x): live collaborative-editing rooms keyed by doc_id.
        # The hub is a DUMB relay of opaque Yjs bytes — it never parses them. Rooms
        # live in RAM (fan-out cache); durability is the .ycrdt baseline + the
        # materialised .md, both inside the hub project's docs/ folder.
        self._rooms: dict[str, DocRoom] = {}

    # -- catalog -------------------------------------------------------------
    def snapshot(self) -> list:
        return list(self._devices.values())

    def announce(self, desc: pb.DeviceDescriptor) -> None:
        desc.online = True
        is_update = desc.uuid in self._devices
        self._devices[desc.uuid] = desc
        etype = pb.CatalogEvent.UPDATED if is_update else pb.CatalogEvent.ADDED
        self._emit_catalog(pb.CatalogEvent(type=etype, device=desc))

    def retire(self, device_uuid: str) -> None:
        desc = self._devices.pop(device_uuid, None)
        if desc is not None:
            self._emit_catalog(
                pb.CatalogEvent(type=pb.CatalogEvent.REMOVED, device=desc))

    def _emit_catalog(self, event: pb.CatalogEvent) -> None:
        for q in self._watchers:
            _offer(q, event)

    # -- watchers (WatchCatalog streams) ------------------------------------
    def add_watcher(self, q: "asyncio.Queue") -> None:
        self._watchers.add(q)

    def remove_watcher(self, q: "asyncio.Queue") -> None:
        self._watchers.discard(q)

    # -- subscribers (Subscribe streams) ------------------------------------
    def add_subscriber(self, sub: Subscriber) -> None:
        self._subs.add(sub)

    def remove_subscriber(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    def publish(self, batch: pb.ReadingBatch) -> None:
        """Fan one ingest batch out to every interested subscriber. FRAME
        payloads are split off to the explicit WatchFrames watchers (§9 —
        video never rides the catch-all Subscribe stream)."""
        frames = [r for r in batch.readings if r.WhichOneof("payload") == "frame"]
        if frames:
            plain = [r for r in batch.readings
                     if r.WhichOneof("payload") != "frame"]
            for sub in self._frame_subs:
                wanted = [r for r in frames
                          if sub.wants(r.device_uuid, r.source_id)]
                if wanted:
                    _offer(sub.queue, pb.ReadingBatch(readings=wanted))
            if not plain:
                return
            batch = pb.ReadingBatch(readings=plain)
        if not self._subs:
            return
        for sub in self._subs:
            if sub.refs is None:
                _offer(sub.queue, batch)
            else:
                wanted = [r for r in batch.readings
                          if sub.wants(r.device_uuid, r.source_id)]
                if wanted:
                    _offer(sub.queue, pb.ReadingBatch(readings=wanted))

    # -- live video: frame watchers + demand (§9) ------------------------------
    # WatchFrames streams register here; the count of explicit refs per
    # (uuid, source) IS the demand signal, forwarded to the owning agent's
    # Ingest session so cameras encode only while somebody watches.
    def register_agent(self, agent_uuid_outq) -> None:
        """An Ingest session announces (uuid -> its out-queue). Re-sends any
        ACTIVE demand so an agent reconnect resumes running streams."""
        uuid, outq = agent_uuid_outq
        self._agent_outq[uuid] = outq
        for (du, sid), n in self._frame_demand.items():
            if du == uuid and n > 0:
                _offer(outq, pb.HubMessage(frame_demand=pb.FrameDemand(
                    device_uuid=du, source_id=sid, active=True)))

    def register_agent_session(self, agent_id: str, outq) -> None:
        """Track a client's down-channel by its identity (from Hello), so we can
        route to a client that has NO active device yet — remote-add (below) targets
        the agent, not a device uuid."""
        if agent_id:
            self._agent_conns[agent_id] = outq

    def unregister_agent_session(self, agent_id: str, outq) -> None:
        if self._agent_conns.get(agent_id) is outq:   # don't clobber a newer session
            self._agent_conns.pop(agent_id, None)
        for rid, (fut, oq) in list(self._pending_cmds.items()):   # session gone → fail
            if oq is outq and not fut.done():                     # ONLY its commands
                fut.set_result(pb.Ack(request_id=rid, ok=False,
                                      detail="client disconnected"))

    async def add_device(self, req) -> "pb.Ack":
        """Route a viewer's AddRemoteDevice to the owning client (by agent_id) as an
        AddDevice down-message, awaiting its Ack (same correlation as send_command).
        The client onboards the device, which then re-announces as active."""
        outq = self._agent_conns.get(req.agent_id)
        if outq is None:
            return pb.Ack(ok=False, detail="that client is not connected")
        rid = _uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending_cmds[rid] = (fut, outq)
        _offer(outq, pb.HubMessage(add_device=pb.AddDevice(
            request_id=rid, instance_id=req.instance_id)))
        try:
            return await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            return pb.Ack(request_id=rid, ok=False, detail="the client did not respond")
        finally:
            self._pending_cmds.pop(rid, None)

    def unregister_agent(self, uuid: str) -> None:
        # per-DEVICE retire only — must NOT fail pending commands (a device retiring
        # mid-session, e.g. an available device going active on add, would clobber the
        # add's own Ack). Session teardown fails pending in unregister_agent_session.
        self._agent_outq.pop(uuid, None)

    # -- control plane (DESIGN §5.3 / §7.5): route a command to the owning agent -
    async def send_command(self, req) -> "pb.Ack":
        """A viewer's SendCommand → the owning agent's Ingest down-channel, awaiting
        the agent's Ack (correlated by request_id). Milestone-1 transport: `req.token`
        is carried but not yet enforced (RBAC is milestone 2). The actual device
        effect is observed via the sink's READBACK source (§7.5), not this Ack —
        the Ack only says the command reached a live agent and was accepted."""
        outq = self._agent_outq.get(req.device_uuid)
        if outq is None:
            return pb.Ack(ok=False, detail="no agent owns this device")
        rid = _uuid.uuid4().hex
        cmd = pb.Command(request_id=rid, device_uuid=req.device_uuid,
                         sink_id=req.sink_id)
        which = req.WhichOneof("value")             # copy the value arm the viewer set
        if which == "scalar":
            cmd.scalar = req.scalar
        elif which == "boolean":
            cmd.boolean = req.boolean
        elif which == "text":
            cmd.text = req.text
        elif which == "trigger":
            cmd.trigger = req.trigger
        fut = asyncio.get_running_loop().create_future()
        self._pending_cmds[rid] = (fut, outq)
        _offer(outq, pb.HubMessage(command=cmd))
        try:
            return await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            return pb.Ack(request_id=rid, ok=False, detail="agent did not respond")
        finally:
            self._pending_cmds.pop(rid, None)

    async def send_config(self, req) -> "pb.Ack":
        """A viewer's SetConfig → the owning agent's down-channel as a Configure,
        awaiting the Ack (same request_id correlation as send_command, §5.3). The new
        state returns to viewers on the re-announced catalog descriptor."""
        outq = self._agent_outq.get(req.device_uuid)
        if outq is None:
            return pb.Ack(ok=False, detail="no agent owns this device")
        rid = _uuid.uuid4().hex
        cfg = pb.Configure(request_id=rid, device_uuid=req.device_uuid)
        which = req.WhichOneof("action")
        if which == "option":
            cfg.option.CopyFrom(req.option)
        elif which == "rate_hz":
            cfg.rate_hz = req.rate_hz
        elif which == "rename":
            cfg.rename = req.rename
        fut = asyncio.get_running_loop().create_future()
        self._pending_cmds[rid] = (fut, outq)
        _offer(outq, pb.HubMessage(configure=cfg))
        try:
            return await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            return pb.Ack(request_id=rid, ok=False, detail="agent did not respond")
        finally:
            self._pending_cmds.pop(rid, None)

    async def remove_device(self, req) -> "pb.Ack":
        """A viewer's RemoveRemoteDevice → the owning agent's down-channel as a
        RemoveDevice, awaiting its Ack (same request_id correlation as send_command).
        The reverse of add_device: routed by the device uuid (translated to the owner's
        instance_id), the agent runs manager.remove and the device leaves the catalog."""
        outq = self._agent_outq.get(req.device_uuid)
        desc = self._devices.get(req.device_uuid)
        if outq is None or desc is None:
            return pb.Ack(ok=False, detail="no agent owns this device")
        rid = _uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending_cmds[rid] = (fut, outq)
        _offer(outq, pb.HubMessage(remove_device=pb.RemoveDevice(
            request_id=rid, instance_id=desc.instance_id)))
        try:
            return await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            return pb.Ack(request_id=rid, ok=False, detail="agent did not respond")
        finally:
            self._pending_cmds.pop(rid, None)

    def deliver_ack(self, ack) -> None:
        """An agent's Ack came up the Ingest stream → resolve the waiting command,
        configure, add-device or remove-device (all correlate by request_id)."""
        entry = self._pending_cmds.get(ack.request_id)
        if entry is not None:
            fut, _outq = entry
            if not fut.done():
                fut.set_result(ack)

    def add_frame_watcher(self, sub: Subscriber) -> None:
        self._frame_subs.add(sub)
        for ref in (sub.refs or ()):
            n = self._frame_demand.get(ref, 0)
            self._frame_demand[ref] = n + 1
            if n == 0:
                self._signal_demand(ref, True)

    def remove_frame_watcher(self, sub: Subscriber) -> None:
        self._frame_subs.discard(sub)
        for ref in (sub.refs or ()):
            n = self._frame_demand.get(ref, 0) - 1
            if n <= 0:
                self._frame_demand.pop(ref, None)
                self._signal_demand(ref, False)
            else:
                self._frame_demand[ref] = n

    def _signal_demand(self, ref, active: bool) -> None:
        outq = self._agent_outq.get(ref[0])
        if outq is not None:
            _offer(outq, pb.HubMessage(frame_demand=pb.FrameDemand(
                device_uuid=ref[0], source_id=ref[1], active=active)))

    # -- tag persistence (JSON backend; SQLite/Postgres-CNPG later) ----------
    def _load_tags(self) -> None:
        if not self._tags_path or not os.path.isfile(self._tags_path):
            return
        try:
            with open(self._tags_path, encoding="utf-8") as fh:
                for d in json.load(fh):
                    t = json_format.ParseDict(d, pb.Tag())
                    if t.id:
                        self._tags[t.id] = t
            log.info("loaded %d tag(s) from %s", len(self._tags), self._tags_path)
        except Exception as exc:                     # noqa: BLE001
            log.warning("could not load tags (%s): %s", self._tags_path, exc)

    def _mark_dirty(self) -> None:
        """Persist soon — coalesce a burst (e.g. a client's snapshot on connect)
        into one atomic write instead of O(N) rewrites of a growing file."""
        if not self._tags_path or self._save_pending:
            return
        self._save_pending = True
        try:
            asyncio.get_running_loop().call_later(1.0, self._flush_tags)
        except RuntimeError:                         # no loop (tests) → write now
            self._flush_tags()

    def _flush_tags(self) -> None:
        self._save_pending = False
        if not self._tags_path:
            return
        try:
            data = [json_format.MessageToDict(t, preserving_proto_field_name=True)
                    for t in self._tags.values()]
            tmp = self._tags_path + ".tmp"
            os.makedirs(os.path.dirname(self._tags_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self._tags_path)         # atomic — no corruption on crash
        except Exception as exc:                     # noqa: BLE001
            log.warning("could not persist tags: %s", exc)

    # -- tags (own reliable channel; LWW by id+version, tombstoned) ----------
    def tag_snapshot(self) -> list:
        """Every stored tag — live AND tombstones — so a reconnecting peer
        converges (it may need a delete it missed while away)."""
        return list(self._tags.values())

    def add_tag_watcher(self, q: "asyncio.Queue") -> None:
        self._tag_watchers.add(q)

    def remove_tag_watcher(self, q: "asyncio.Queue") -> None:
        self._tag_watchers.discard(q)

    def publish_tag(self, tag: pb.Tag) -> bool:
        """Merge a tag, last-write-wins on version. Returns True if it changed
        our state (and was fanned out), False if stale/duplicate."""
        cur = self._tags.get(tag.id)
        if cur is not None and tag.version < cur.version:
            return False                         # stale — older than what we have
        if cur is not None and tag.version == cur.version \
                and not tag.deleted and not cur.deleted:
            return False                         # idempotent same-version upsert
        self._tags[tag.id] = tag
        self._mark_dirty()                           # persist (durable + authoritative)
        if tag.deleted:
            etype = pb.TagEvent.REMOVED
        elif cur is None:
            etype = pb.TagEvent.ADDED
        else:
            etype = pb.TagEvent.UPDATED
        self._emit_tag(pb.TagEvent(type=etype, tag=tag))
        return True

    def delete_tag(self, tag_id: str, version: int, origin_id: str = "") -> bool:
        """Tombstone a tag. The tombstone's version must beat the live one to
        win LWW; bump it if the caller's is too low. Carries the live tag's
        context (t/kind/label) into the REMOVED event for the audit log."""
        cur = self._tags.get(tag_id)
        if cur is not None and version <= cur.version:
            version = cur.version + 1
        tomb = pb.Tag(id=tag_id, version=version, deleted=True,
                      origin_id=origin_id)
        if cur is not None:
            tomb.t, tomb.kind, tomb.label = cur.t, cur.kind, cur.label
            tomb.scope, tomb.severity = cur.scope, cur.severity
        return self.publish_tag(tomb)

    def _emit_tag(self, event: pb.TagEvent) -> None:
        for q in self._tag_watchers:
            q.put_nowait(event)                  # unbounded queue — tags are reliable

    # -- project storage (FOLDERS, via ferrodac.core.projects — mountable) ---
    def _load_projects(self) -> None:
        if not self._projects_dir or not os.path.isdir(self._projects_dir):
            return
        try:
            from ferrodac.core.projects import Project, is_project
        except Exception as exc:                     # noqa: BLE001
            log.warning("projects disabled (no ferrodac.core.projects): %s", exc)
            return
        for name in sorted(os.listdir(self._projects_dir)):
            d = os.path.join(self._projects_dir, name)
            if not is_project(d):
                continue
            try:
                rec = json_format.ParseDict(Project(d).to_record(), pb.Project())
                if rec.id:
                    self._projects[rec.id] = rec
            except Exception as exc:                 # noqa: BLE001
                log.warning("could not load project %s: %s", d, exc)
        log.info("loaded %d hub project(s) from %s",
                 len(self._projects), self._projects_dir)

    def _write_project_folder(self, project: pb.Project) -> None:
        if not self._projects_dir:
            return
        try:
            from ferrodac.core.projects import Project
            rec = json_format.MessageToDict(project, preserving_proto_field_name=True)
            Project(os.path.join(self._projects_dir, project.id)).apply_record(rec)
        except Exception as exc:                     # noqa: BLE001
            log.warning("could not write project folder %s: %s", project.id, exc)
        if self._backup is not None:                 # queue an incremental backup
            with self._backup_lock:
                self._backup_dirty.add(project.id)

    def flush_backups(self) -> None:
        """Mirror any projects queued since the last flush — blocking file I/O, so the
        hub runs this in an executor off the event loop (main._backup_loop)."""
        if self._backup is None:
            return
        with self._backup_lock:
            pending, self._backup_dirty = self._backup_dirty, set()
        for pid in pending:
            rec = self._projects.get(pid)
            self._backup.mirror(pid, rec.name if rec is not None else "")

    def _remove_project_folder(self, project_id: str) -> None:
        if not self._projects_dir:
            return
        import shutil
        try:
            shutil.rmtree(os.path.join(self._projects_dir, project_id))
        except FileNotFoundError:
            pass
        except Exception as exc:                     # noqa: BLE001
            log.warning("could not remove project folder %s: %s", project_id, exc)

    # -- projects (shared index; LWW by id+version, tombstoned) --------------
    def project_snapshot(self) -> list:
        """Every project the hub holds, plus any session tombstones, so a peer
        converging mid-session sees deletes it missed. (Tombstones aren't durable —
        a deleted project is just an absent folder after a restart.)"""
        return list(self._projects.values())

    def add_project_watcher(self, q: "asyncio.Queue") -> None:
        self._project_watchers.add(q)

    def remove_project_watcher(self, q: "asyncio.Queue") -> None:
        self._project_watchers.discard(q)

    def publish_project(self, project: pb.Project) -> bool:
        """Merge a project, last-write-wins on version, persisting it as a real
        project FOLDER. Returns True if it changed our state (and was fanned out)."""
        cur = self._projects.get(project.id)
        if cur is not None and project.version < cur.version:
            return False                         # stale
        if cur is not None and project.version == cur.version \
                and not project.deleted and not cur.deleted:
            return False                         # idempotent same-version upsert
        # transparent dial: the first time we see a project with no remote, provision a
        # git repo in the bundled Gitea and carry its URL into the stored + fanned-out
        # record, so every member gets a clone URL without setting up git themselves.
        if (not project.deleted and self.gitea is not None and not project.git_remote):
            url = self.gitea.provision(project.id)
            if url:
                project.git_remote = url
                log.info("transparent git: provisioned repo %s/%s for project %r",
                         getattr(self.gitea, "org", "?"), project.id, project.name)
        self._projects[project.id] = project
        if project.deleted:
            self._remove_project_folder(project.id)
            etype = pb.ProjectEvent.REMOVED
        else:
            self._write_project_folder(project)
            etype = pb.ProjectEvent.ADDED if cur is None else pb.ProjectEvent.UPDATED
        self._emit_project(pb.ProjectEvent(type=etype, project=project))
        return True

    def git_credential(self, project_id: str) -> tuple:
        """The ephemeral (url, username, password) for pushing/pulling this project's
        provisioned repo, or None if there's no transparent-git repo for it. Handed
        out over GetGitCredential and held in memory client-side — the token is NEVER
        put in the fanned-out record (that was the leak). Only projects we actually
        know and that carry a (credential-free) remote qualify."""
        if self.gitea is None:
            return None
        proj = self._projects.get(project_id)
        if proj is None or proj.deleted or not proj.git_remote:
            return None
        return self.gitea.credentials(project_id)

    def delete_project(self, project_id: str, version: int, origin_id: str = "") -> bool:
        """Tombstone a project (removes its folder). The tombstone's version must
        beat the live one to win LWW; bump it if the caller's is too low."""
        cur = self._projects.get(project_id)
        if cur is not None and version <= cur.version:
            version = cur.version + 1
        tomb = pb.Project(id=project_id, version=version, deleted=True,
                          origin_id=origin_id)
        if cur is not None:
            tomb.name = cur.name                 # carry context into the audit event
        return self.publish_project(tomb)

    def _emit_project(self, event: pb.ProjectEvent) -> None:
        for q in self._project_watchers:
            q.put_nowait(event)                  # unbounded — projects are reliable

    # -- docs (live collaborative editing; opaque Yjs relay + materialised .md) --
    def _doc_paths(self, doc_id: str) -> "tuple[str | None, str | None]":
        """(md_path, blob_path) for ``doc_id = "<project_id>::<relpath>"`` — the
        file under the hub project's docs/ folder. Returns (None, None) if there's
        no projects dir or the path escapes it (traversal / absolute)."""
        if not self._projects_dir or "::" not in doc_id:
            return None, None
        pid, relpath = doc_id.split("::", 1)
        if not pid or not relpath:
            return None, None
        base = os.path.abspath(self._projects_dir)
        md = os.path.normpath(os.path.join(base, pid, "docs", relpath))
        try:
            if os.path.commonpath([base, md]) != base:
                return None, None                # escaped the projects dir — refuse
        except ValueError:                       # different drives (Windows)
            return None, None
        return md, md + ".ycrdt"

    def _read_doc_text(self, room: DocRoom) -> str:
        try:
            if room.md_path and os.path.isfile(room.md_path):
                with open(room.md_path, encoding="utf-8") as fh:
                    return fh.read()
        except Exception:                        # noqa: BLE001
            pass
        return ""

    def _get_room(self, doc_id: str) -> "DocRoom | None":
        room = self._rooms.get(doc_id)
        if room is not None:
            return room
        md_path, blob_path = self._doc_paths(doc_id)
        if md_path is None:
            return None                          # malformed / traversal — refuse
        room = DocRoom(doc_id, md_path, blob_path)
        try:                                     # cold start: replay a persisted baseline
            if blob_path and os.path.isfile(blob_path):
                with open(blob_path, "rb") as fh:
                    room.baseline = fh.read()
                room.seeded = bool(room.baseline)
        except Exception:                        # noqa: BLE001
            pass
        self._rooms[doc_id] = room
        return room

    def doc_join(self, doc_id: str, member: DocMember) -> bool:
        """Attach `member` to a room and send it the cold-start handshake + replay.
        AWAIT-FREE on purpose: the cold-check-and-claim must stay atomic on the
        single loop, else two concurrent joiners could both think they should seed.
        Returns False if the doc_id is refused (malformed / traversal)."""
        room = self._get_room(doc_id)
        if room is None:
            return False
        room._seq += 1
        member.seq = room._seq
        room.members.append(member)
        if room.leader is None:
            room.leader = member                 # first member leads (the .md writer)
        if not room.seeded and room.seeder is None and not room.log:
            room.seeder = member                 # COLD: this peer seeds from the file
            member.queue.put_nowait(pb.DocServerMsg(seed=pb.DocSeed(
                doc_id=doc_id, should_seed=True, text=self._read_doc_text(room))))
        else:                                    # warm: start empty, replay state
            member.queue.put_nowait(pb.DocServerMsg(
                seed=pb.DocSeed(doc_id=doc_id, should_seed=False)))
            if room.baseline:
                member.queue.put_nowait(pb.DocServerMsg(
                    update=pb.DocUpdate(doc_id=doc_id, update=room.baseline)))
            for u in room.log:
                member.queue.put_nowait(pb.DocServerMsg(
                    update=pb.DocUpdate(doc_id=doc_id, update=u)))
        self._emit_presence(room)
        return True

    def doc_update(self, doc_id: str, member: DocMember,
                   update: bytes, compaction: bool = False) -> None:
        room = self._rooms.get(doc_id)
        if room is None or member not in room.members:
            return
        if compaction:                           # leader-only: new baseline, drop the log
            if member is room.leader or member is room.seeder:
                room.baseline, room.log, room.seeded = update, [], True
                self._persist_blob(room)
        elif member is room.seeder and not room.seeded:
            room.baseline, room.seeded = update, True   # the seeder's first update IS the baseline
            self._persist_blob(room)
        else:
            room.log.append(update)              # incremental — not persisted (compaction is)
        msg = pb.DocServerMsg(update=pb.DocUpdate(
            doc_id=doc_id, update=update, compaction=compaction))
        for m in room.members:
            if m is not member:                  # never echo a sender its own update
                m.queue.put_nowait(msg)          # unbounded — updates are reliable

    def doc_awareness(self, doc_id: str, member: DocMember, state: bytes) -> None:
        room = self._rooms.get(doc_id)
        if room is None:
            return
        msg = pb.DocServerMsg(awareness=pb.DocAwareness(doc_id=doc_id, state=state))
        for m in room.members:
            if m is not member:
                _offer(m.queue, msg)             # presence is expendable

    def doc_snapshot(self, doc_id: str, member: DocMember, text: str) -> None:
        """Materialise the human-readable .md — LEADER ONLY (one writer, no races)."""
        room = self._rooms.get(doc_id)
        if room is None or member is not room.leader or not room.md_path:
            return
        try:
            os.makedirs(os.path.dirname(room.md_path), exist_ok=True)
            tmp = room.md_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, room.md_path)        # atomic
        except Exception as exc:                 # noqa: BLE001
            log.warning("could not materialise doc %s: %s", doc_id, exc)

    def doc_leave(self, doc_id: str, member: DocMember) -> None:
        room = self._rooms.get(doc_id)
        if room is None or member not in room.members:
            return
        room.members.remove(member)
        if room.seeder is member:
            room.seeder = None
            if not room.seeded and room.members:  # seeder left COLD → re-designate (C2)
                nxt = min(room.members, key=lambda m: m.seq)
                room.seeder = nxt
                nxt.queue.put_nowait(pb.DocServerMsg(seed=pb.DocSeed(
                    doc_id=doc_id, should_seed=True, text=self._read_doc_text(room))))
        if room.leader is member:                # promote the longest-connected member
            room.leader = min(room.members, key=lambda m: m.seq) if room.members else None
        self._emit_presence(room)                # keep the room in RAM (baseline persists)

    def _emit_presence(self, room: DocRoom) -> None:
        msg = pb.DocServerMsg(presence=pb.DocPresence(
            doc_id=room.doc_id, actors=[m.actor for m in room.members]))
        for m in room.members:
            _offer(m.queue, msg)

    def _persist_blob(self, room: DocRoom) -> None:
        """Write the compacted baseline blob (.ycrdt) atomically. Only the seed
        update and compactions change it — incrementals are not persisted (bounded
        I/O); a clean restart replays this baseline, a crash loses <1 compaction."""
        if not room.blob_path or not room.baseline:
            return
        try:
            os.makedirs(os.path.dirname(room.blob_path), exist_ok=True)
            tmp = room.blob_path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(room.baseline)
            os.replace(tmp, room.blob_path)
        except Exception as exc:                 # noqa: BLE001
            log.warning("could not persist doc baseline %s: %s", room.doc_id, exc)
