"""Pick one rebuild candidate per real project from the process 2 pipeline-dirs table.

Pure functions over the TSV rows (no disk, no database), so they are testable without radar-41.

1. Keep your own folders: drop hidden path segments, installed libraries, upstream checkouts,
   saved web pages, and folders that are already repos under ~/projects.
2. Roots: walk folders from the top. A folder becomes a candidate when it looks like a project
   (a marker file, a README, or at least `min_own` code files of its own) and its whole subtree
   holds between `min_code` and `max_code` code files. Folders under a candidate belong to it
   (hpc_bundle stays inside DIR_btc). Containers over `max_code` are split into their children.
3. Copies: two candidates are the same project when the shapes of their subtrees (relative
   folder, code files, code bytes) overlap by at least `similar`. One is kept: the newest,
   then the one outside transfer/backup folders, then the shortest path.
4. Names: folder name as a lower-case slug; generic or repeated names get the parent's name.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

DEFAULT_SKIP = (
    r"(^|/)[.]"                                                     # hidden: .codex, .claude, .local ...
    r"|^(R|pytorch|snap|go|projects|miniconda3|anaconda3|ventoy-[^/]*)(/|$)"
    r"|/(github/files|upstream|node_modules|site-packages|vendor|third_party|external)(/|$)"
    r"|_files(/|$)"                                                 # browser "save page" folders
)
COPYISH = re.compile(r"T9-PURGE|OneDrive|SAFETY_COPY|_full_inbound|(^|/)aoazi|drive-download|(^|/)Downloads/"
                     r"|backup|_copy\b|\(\d+\)", re.I)
GENERIC = {
    "src", "code", "scripts", "script", "new-folder", "project", "projects", "main", "app", "lib",
    "test", "tests", "tools", "utils", "util", "final", "backup", "copy", "misc", "files", "folder",
    "work", "export", "kit", "core", "engine", "api", "runner", "viewer", "docs", "notes", "data",
    "v1", "v2", "v3", "old", "new", "tmp", "temp",
}


@dataclass
class Dir:
    path: str            # absolute
    rel: str             # relative to home
    own_code: int
    markers: int
    readme: bool
    newest: str
    code_bytes: int
    subtree_code: int = 0
    subtree_newest: str = ""


@dataclass
class Candidate:
    name: str
    path: str
    rel: str
    subtree_code: int
    newest: str
    nested: int                       # project-like folders inside it
    copies: list[str] = field(default_factory=list)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def read_dirs(path, home: str) -> list[Dir]:
    home = home.rstrip("/")
    out = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            abs_path = r["root_path"].rstrip("/") + "/" + r["dir"].strip("/")
            if r["dir"] in (".", ""):
                abs_path = r["root_path"].rstrip("/")
            if not (abs_path == home or abs_path.startswith(home + "/")):
                continue
            rel = abs_path[len(home) + 1:] if abs_path != home else "."
            out.append(Dir(abs_path, rel, int(r["code_files"] or 0), int(r["marker_files"] or 0),
                           r.get("has_readme", "").strip().lower() in ("t", "true", "1"),
                           r.get("newest_mtime", "") or "", int(float(r.get("code_bytes") or 0))))
    return out


def ancestors(rel: str):
    parts = rel.split("/")
    for i in range(1, len(parts)):
        yield "/".join(parts[:i])


def select(dirs: list[Dir], skip=DEFAULT_SKIP, min_code=3, max_code=400, min_own=3, similar=0.8):
    rx = re.compile(skip)
    mine = {d.rel: d for d in dirs if d.rel != "." and not rx.search(d.rel)}
    for d in mine.values():
        d.subtree_code, d.subtree_newest = d.own_code, d.newest
    for d in list(mine.values()):
        for a in ancestors(d.rel):
            if a in mine:
                mine[a].subtree_code += d.own_code
                mine[a].subtree_newest = max(mine[a].subtree_newest, d.newest)

    def project_like(d):
        return d.markers > 0 or d.readme or d.own_code >= min_own

    chosen: dict[str, Dir] = {}
    too_big = []
    for d in sorted(mine.values(), key=lambda d: (d.rel.count("/"), d.rel)):
        if any(a in chosen for a in ancestors(d.rel)) or not project_like(d):
            continue
        if d.subtree_code > max_code:
            too_big.append(d)
        elif d.subtree_code >= min_code:
            chosen[d.rel] = d

    def shape(root):
        n = len(root) + 1
        sig = set()
        for rel, d in mine.items():
            if rel == root or rel.startswith(root + "/"):
                sub = "." if rel == root else rel[n:]
                sig.add((norm(sub), d.own_code, d.code_bytes))
        return sig

    shapes = {rel: shape(rel) for rel in chosen}
    parent = {rel: rel for rel in chosen}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    keys = sorted(chosen)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            ca, cb = chosen[a].subtree_code, chosen[b].subtree_code
            if min(ca, cb) < similar * max(ca, cb):
                continue
            sa, sb = shapes[a], shapes[b]
            if len(sa & sb) >= similar * len(sa | sb):
                parent[find(b)] = find(a)
    groups: dict[str, list[Dir]] = {}
    for rel in keys:
        groups.setdefault(find(rel), []).append(chosen[rel])

    def preference(d):
        return (d.subtree_newest, not COPYISH.search(d.rel), -len(d.rel))

    result, used = [], set()
    for members in groups.values():
        members.sort(key=preference, reverse=True)
        keep = members[0]
        nested = sum(1 for rel, d in mine.items()
                     if rel.startswith(keep.rel + "/") and project_like(d))
        result.append(Candidate("", keep.path, keep.rel, keep.subtree_code, keep.subtree_newest, nested,
                                [m.path for m in members[1:]]))
    result.sort(key=lambda c: (c.newest, c.subtree_code), reverse=True)
    for c in result:
        c.name = unique_name(c.rel, used)
        used.add(c.name)
    return result, too_big


def unique_name(rel: str, used: set[str]) -> str:
    parts = [p for p in (slug(x) for x in PurePosixPath(rel).parts) if p] or ["pipeline"]
    while len(parts) > 1 and parts[-1] == parts[-2]:      # SimpleRasMut/SimpleRasMut
        parts.pop()

    def weak(x):
        return x in GENERIC or len(x) < 3 or x.isdigit()
    k = 1
    while k < len(parts) and all(weak(x) for x in parts[-k:]):
        k += 1
    name = "-".join(parts[-k:])
    while name in used and k < len(parts):
        k += 1
        name = "-".join(parts[-k:])
    name = name[-80:].strip("-")
    base, n = name, 2
    while name in used:
        name, n = f"{base}-{n}", n + 1
    return name
