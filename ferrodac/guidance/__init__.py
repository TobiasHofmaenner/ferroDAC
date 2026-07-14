"""GuidanceLibrary — procedural PLAYBOOKS (the HOW) served alongside /describe's
verbs (the WHAT). Qt-free, plain text.

A playbook is a markdown file with a small frontmatter block::

    ---
    id: set-up-a-live-readout
    title: Set up a live readout
    when_to_use: You want a source's value shown live on the dashboard.
    tags: [dashboard, routing, live]
    verbs_used: [source.list, layout.add_panel, layout.route]
    ---
    ## Steps ...
    ```skeleton
    ... copy-pasteable client code ...
    ```

Playbooks load from a BUILT-IN dir (this package) and the USER config dir
(~/.config/ferrodac/guidance); a user file with the same id shadows a built-in.
The frontmatter parser is intentionally tiny (scalars + inline [a,b] lists) so
guidance stays a zero-dependency text feature — no PyYAML at runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..core.connectors import default_config_dir

BUILTIN_DIR = os.path.dirname(__file__)


def user_guidance_dir() -> str:
    return os.path.join(default_config_dir(), "guidance")


@dataclass
class Playbook:
    id: str
    title: str
    when_to_use: str
    tags: list
    verbs_used: list
    body: str
    source: str
    path: str

    def index(self) -> dict:
        return {"id": self.id, "title": self.title,
                "when_to_use": self.when_to_use, "tags": list(self.tags),
                "source": self.source}

    def full(self) -> dict:
        return {"id": self.id, "title": self.title,
                "when_to_use": self.when_to_use, "tags": list(self.tags),
                "verbs_used": list(self.verbs_used), "body": self.body,
                "source": self.source}


def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    return v


def _value(raw: str):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    return _scalar(raw)


def parse_frontmatter(text: str) -> "tuple[dict, str]":
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta: dict = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, val = line.partition(":")
        if sep:
            meta[key.strip()] = _value(val)
    return meta, "\n".join(lines[end + 1:]).lstrip("\n")


def _playbook_from_file(path: str, source: str) -> "Playbook | None":
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    meta, body = parse_frontmatter(text)
    pid = str(meta.get("id") or os.path.splitext(os.path.basename(path))[0]).strip()
    if not pid:
        return None

    def _as_list(v):
        return v if isinstance(v, list) else ([str(v)] if v else [])

    return Playbook(id=pid, title=str(meta.get("title") or pid),
                    when_to_use=str(meta.get("when_to_use") or ""),
                    tags=_as_list(meta.get("tags")),
                    verbs_used=_as_list(meta.get("verbs_used")),
                    body=body, source=source, path=path)


class GuidanceLibrary:
    """Built-in + user playbooks; a user file shadows a built-in of the same id.
    Re-reads on each call so a dropped-in .md needs no restart (files are tiny)."""

    def __init__(self, builtin_dir: str = BUILTIN_DIR, user_dir: "str | None" = None):
        self._builtin_dir = builtin_dir
        self._user_dir = user_dir or user_guidance_dir()

    def _load(self) -> dict:
        books: dict = {}
        for d, source in ((self._builtin_dir, "builtin"), (self._user_dir, "user")):
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for name in names:
                if name.endswith(".md"):
                    pb = _playbook_from_file(os.path.join(d, name), source)
                    if pb is not None:
                        books[pb.id] = pb        # user (2nd) shadows built-in
        return books

    def list(self) -> list:
        return [pb.index() for pb in sorted(self._load().values(), key=lambda b: b.id)]

    def get(self, pid: str) -> "dict | None":
        pb = self._load().get(str(pid))
        return pb.full() if pb is not None else None
