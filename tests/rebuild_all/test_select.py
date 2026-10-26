"""Candidate selection from a pipeline-dirs table: own folders, roots, copies, names."""
import csv

from housekeeping.rebuild_all import select as sel

FIELDS = ["root_id", "root_path", "dir", "code_files", "marker_files", "markers", "exts", "projects",
          "newest_mtime", "code_bytes", "has_readme"]


def table(tmp_path, rows):
    p = tmp_path / "dirs.tsv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(FIELDS)
        for d, code, markers, readme, mtime, size in rows:
            w.writerow([21, "/home", "u/" + d, code, markers, "", "py", "", mtime, size, readme])
    return sel.read_dirs(p, "/home/u")


def test_roots_nested_copies_and_noise(tmp_path):
    dirs = table(tmp_path, [
        ("work/btc", 40, 0, "True", "2026-07-25 02:00:00-04:00", 1000),
        ("work/btc/hpc_bundle", 7, 1, "True", "2026-10-23 02:00:00-04:00", 600),
        ("work/btc/engines", 12, 0, "False", "2026-05-01 00:00:00-04:00", 300),
        # three copies of one MATLAB project, one renamed with underscores, one in a transfer folder
        ("Yale/Dr. Stites/SimpleRasMut/SimpleRasMut", 122, 0, "True", "2026-01-10 00:00:00-05:00", 9000),
        ("T9-PURGE/Windows/Dr. Stites/SimpleRasMut/SimpleRasMut", 122, 0, "True", "2026-01-10 00:00:00-05:00", 9000),
        ("aoazi/Dr._Stites/SimpleRasMut/SimpleRasMut", 122, 0, "True", "2026-01-10 00:00:00-05:00", 9000),
        # noise
        (".codex/plugins/x", 5, 1, "True", "2026-07-01 00:00:00-04:00", 10),
        ("pytorch/torch", 52, 1, "False", "2026-09-12 00:00:00-04:00", 10),
        ("projects/housekeeping", 1, 1, "True", "2026-10-25 00:00:00-04:00", 10),
        ("docs/upstream/ollama", 2, 1, "True", "2026-07-01 00:00:00-04:00", 10),
        ("Downloads/page_files", 112, 0, "False", "2026-06-18 00:00:00-04:00", 10),
        # tiny and container
        ("misc/one", 1, 0, "False", "2026-01-01 00:00:00-05:00", 10),
        ("big", 5, 0, "False", "2026-01-01 00:00:00-05:00", 10),
        ("big/a", 300, 1, "True", "2026-01-01 00:00:00-05:00", 10),
        ("big/b", 300, 1, "True", "2026-02-01 00:00:00-05:00", 77),
    ])
    cands, too_big = sel.select(dirs, max_code=400)
    by = {c.name: c for c in cands}
    assert set(by) == {"btc", "simplerasmut", "big-a", "big-b"}, set(by)
    assert by["btc"].subtree_code == 59 and by["btc"].nested == 2
    ras = by["simplerasmut"]
    assert len(ras.copies) == 2 and "T9-PURGE" not in ras.path and "aoazi" not in ras.path
    assert [d.rel for d in too_big] == ["big"]


def test_names_generic_repeat_and_collision():
    used = set()
    names = []
    for rel in ["A/src", "B/src", "x/New_folder", "p/q/solver", "r/solver", "s/2024"]:
        n = sel.unique_name(rel, used)
        used.add(n)
        names.append(n)
    assert names == ["a-src", "b-src", "x-new-folder", "solver", "r-solver", "s-2024"]
