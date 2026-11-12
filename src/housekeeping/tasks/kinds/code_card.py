"""code-card: one folder that holds code, described by the reader model from the process 2 inventory.

The prompt is built from file paths and the header text Atlas stored, never from reading the files.
CHECK (check below) verifies the card's facts against the inventory and the disk, with no model:
cited files and the entry point are in this folder's inventory, exist, and still hash to the inventory
sha256; the language fits the folder's extensions. Duplicates are found by sha256. Judgement fields
(what_it_is, state_guess, related_project, dependencies) stay claims.
"""
import csv
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from housekeeping.atlas_queries.cli import PROTECTED_SEGMENT, withhold_protected

NAME = "code-card"
ROLE = "reader"
INPUT_FIELDS = ("root_id", "root_path", "dir", "code_files", "markers", "exts", "projects", "newest_mtime")
CLAIM_FIELDS = ("what_it_is", "entry_point", "language", "dependencies", "related_project", "state_guess",
                "files_cited")
STATES = ("working", "partial", "parked", "dead")
MAX_DEPS = 10
MAX_CITED = 20
PROMPT_MAX_CHARS = 24000

_files_cache: dict[str, dict[tuple[str, str], list[dict]]] = {}


def input_from_row(row: dict) -> dict:
    return {k: row.get(k, "") or "" for k in INPUT_FIELDS}


def validate_input(inp: dict) -> list[str]:
    missing = [k for k in ("root_id", "root_path", "dir") if not inp.get(k)]
    return [f"input missing {','.join(missing)}"] if missing else []


def validate_claims(c: dict) -> list[str]:
    p = []
    missing = [k for k in CLAIM_FIELDS if k not in c]
    if missing:
        p.append(f"claims missing {','.join(missing)}")
    extra = sorted(set(c) - set(CLAIM_FIELDS))
    if extra:
        p.append(f"claims has unknown {','.join(extra)}")
    for k in ("what_it_is", "entry_point", "language", "related_project"):
        if k in c and not isinstance(c[k], str):
            p.append(f"{k} is not text")
    if "state_guess" in c and c["state_guess"] not in STATES:
        p.append(f"state_guess {c['state_guess']!r} not one of {','.join(STATES)}")
    for k, cap in (("dependencies", MAX_DEPS), ("files_cited", MAX_CITED)):
        if k in c:
            if not isinstance(c[k], list) or not all(isinstance(x, str) for x in c[k]):
                p.append(f"{k} is not a list of text")
            elif len(c[k]) > cap:
                p.append(f"{k} has {len(c[k])} entries, cap is {cap}")
    return p


def newest_files_tsv(inventory_dir: str) -> Path:
    found = sorted(Path(inventory_dir).glob("files_*.tsv"))
    if not found:
        raise FileNotFoundError(f"no files_*.tsv in {inventory_dir}; run process 2 first")
    return found[-1]


def _load_files(path: Path) -> dict[tuple[str, str], list[dict]]:
    key = str(path)
    if key not in _files_cache:
        csv.field_size_limit(sys.maxsize)
        by_dir: dict[tuple[str, str], list[dict]] = {}
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                by_dir.setdefault((row["root_id"], row["dir"]), []).append(row)
        _files_cache[key] = by_dir
    return _files_cache[key]


def build_request(inp: dict, *, inventory_dir: str, protected_names: list[str]) -> dict:
    """The chat request for this folder: {role, body, files_listed, files_dropped, withheld}."""
    rows = _load_files(newest_files_tsv(inventory_dir)).get((str(inp["root_id"]), inp["dir"]), [])
    cells = [(r["root_path"], r["rel_path"], r.get("header", "")) for r in rows]
    kept, withheld = withhold_protected(cells, protected_names)
    kept.sort(key=lambda c: (len(c[2]), c[1]))  # shortest headers first, so the largest are dropped first
    parts, used, dropped = [], 0, 0
    for _, rel, header in kept:
        text = header.replace(" \\n ", "\n").replace("\\n", "\n")
        block = f"== {rel}\n{text}\n"
        if used + len(block) > PROMPT_MAX_CHARS:
            dropped += 1
            parts.append(f"== {rel}\n")  # path still listed, header dropped
            continue
        parts.append(block)
        used += len(block)
    listing = "".join(sorted(parts))
    note = f"\n({dropped} file headers were left out for length; their paths are still listed.)\n" if dropped else ""
    prompt = (
        "You are describing one folder of research code. Below are its code files with the first lines "
        "(header) of each. Use only what is shown.\n"
        f"Folder: {inp['root_path']}/{inp['dir']}\n"
        f"Code files: {inp.get('code_files', '')}. Markers: {inp.get('markers', '')}. "
        f"Extensions: {inp.get('exts', '')}. Project tags: {inp.get('projects', '')}. "
        f"Newest change: {inp.get('newest_mtime', '')}.\n\n"
        f"{listing}{note}\n"
        "Reply with one JSON object and nothing else, with exactly these keys:\n"
        '{"summary": "one sentence, at most 200 characters",\n'
        ' "what_it_is": "two sentences at most",\n'
        ' "entry_point": "relative path of the file you would run first, or empty",\n'
        ' "language": "main language",\n'
        f' "dependencies": ["up to {MAX_DEPS} libraries or tools named in the headers"],\n'
        ' "related_project": "project name, or empty",\n'
        f' "state_guess": "one of {", ".join(STATES)}",\n'
        ' "files_cited": ["up to 8 relative paths from the list above that support this"]}\n'
        "Keep the whole reply under 900 characters."
    )
    body = {"model": ROLE, "stream": False, "reasoning_effort": "none", "max_tokens": 800,
            "messages": [{"role": "user", "content": prompt}]}
    return {"role": ROLE, "body": body, "files_listed": len(kept), "files_dropped": dropped,
            "withheld": withheld}


_JSON = re.compile(r"\{.*\}", re.S)


def reply_text(raw: dict) -> str:
    try:
        text = raw["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S)


def parse(raw: dict, *, task_id: str, inp: dict, pointers: list[dict]) -> dict | None:
    """The card, or None when the reply holds no JSON object."""
    m = _JSON.search(reply_text(raw))
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    summary = obj.pop("summary", "")
    claims = {k: obj.get(k) for k in CLAIM_FIELDS if k in obj}
    claims.update({k: v for k, v in obj.items() if k not in CLAIM_FIELDS})  # unknown keys fail validation
    folder = {"path": f"{inp['root_path']}/{inp['dir']}", "sha256": ""}
    return {"task_id": task_id, "kind": NAME, "summary": summary, "pointers": [folder, *pointers],
            "claims": claims}


# ---------- check (no model) ----------

LANG_EXTS = {
    "python": {"py", "ipynb"}, "bash": {"sh", "bash"}, "shell": {"sh", "bash"}, "sh": {"sh", "bash"},
    "matlab": {"m"}, "javascript": {"js", "mjs", "jsx"}, "typescript": {"ts", "tsx"}, "rust": {"rs"},
    "c": {"c", "h"}, "c++": {"cc", "cpp", "hpp", "h"}, "cpp": {"cc", "cpp", "hpp", "h"}, "julia": {"jl"},
    "r": {"r"}, "go": {"go"}, "sql": {"sql"}, "slurm": {"sbatch", "slurm"},
}


def _sha256_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _resolve(cited: str, rows_by_rel: dict, inp: dict) -> str | None:
    """The inventory rel_path a cited path means: as given (root relative) or relative to the folder."""
    c = cited.strip()
    while c.startswith("./"):
        c = c[2:]
    if not c or ".." in c.split("/"):
        return None
    if c in rows_by_rel:
        return c
    joined = c if inp["dir"] == "." else f"{inp['dir']}/{c}"
    return joined if joined in rows_by_rel else None


def check(card: dict | None, inp: dict, *, inventory_dir: str):
    """(ok, detail, retry). retry is False when the inventory itself is stale, so asking again cannot help."""
    if not card:
        return False, "no card", True
    claims = card.get("claims", {})
    by_dir = _load_files(newest_files_tsv(inventory_dir))
    rows = by_dir.get((str(inp["root_id"]), inp["dir"]), [])
    if not rows:
        return False, "folder has no rows in the inventory; rerun process 2", False
    rows_by_rel = {r["rel_path"]: r for r in rows}
    if not os.path.isdir(os.path.join(inp["root_path"], inp["dir"])):
        return False, f"folder is gone from disk: {inp['root_path']}/{inp['dir']}; rerun process 2", False

    cited = list(claims.get("files_cited", []))
    entry = claims.get("entry_point", "")
    wrong, stale, gone = [], [], []
    checked = {}
    for name in ([entry] if entry else []) + cited:
        rel = _resolve(name, rows_by_rel, inp)
        if rel is None or PROTECTED_SEGMENT.search(name):
            wrong.append(name)
            continue
        if rel in checked:
            continue
        path = os.path.join(inp["root_path"], rel)
        digest = _sha256_file(path)
        checked[rel] = digest
        if digest is None:
            gone.append(rel)
        elif digest != rows_by_rel[rel]["sha256"]:
            stale.append(rel)
    if not cited:
        wrong.append("(no files cited)")

    problems = []
    if wrong:
        problems.append("not in this folder: " + ", ".join(wrong[:5]))
    if gone:
        problems.append("missing on disk: " + ", ".join(gone[:5]))
    if stale:
        problems.append("changed since inventory: " + ", ".join(stale[:5]))

    lang = str(claims.get("language", "")).strip().lower()
    exts = {e.strip().lower() for e in str(inp.get("exts", "")).split(",") if e.strip()} or \
        {r["ext"].lower() for r in rows if r.get("ext")}
    note = ""
    if lang in LANG_EXTS:
        if not LANG_EXTS[lang] & exts:
            problems.append(f"language {lang} fits none of {','.join(sorted(exts))}")
    elif lang:
        note = f"; language {lang} not checked"

    # duplicates by sha256: cited files whose exact content also sits in another folder
    shas = {rows_by_rel[r]["sha256"] for r in checked if checked[r]}
    elsewhere = set()
    for (rid, d), other in by_dir.items():
        if (rid, d) == (str(inp["root_id"]), inp["dir"]):
            continue
        if any(o["sha256"] in shas for o in other):
            elsewhere.add(f"{other[0]['root_path']}/{d}")
    dups = f"; {len(elsewhere)} other folders hold copies of cited files" if elsewhere else ""

    if problems:
        # a wrong citation is the model's error (retry can help); gone or changed files mean the inventory is stale
        retry = bool(wrong) or any(p.startswith("language") for p in problems)
        if gone or stale:
            retry = False
        return False, "; ".join(problems) + dups, retry
    return True, f"{len(checked)} files exist and match the inventory{note}{dups}", True


CHECK = check
