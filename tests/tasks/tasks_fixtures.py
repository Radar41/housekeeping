"""Constants shared by the hk tasks tests (conftest cannot be imported by name)."""
SECRET = "SECRET-PROMPT-XYZ"
FILES_COLS = ["root_id", "root_path", "document_id", "rel_path", "dir", "ext", "is_marker", "project",
              "project_score", "sha256", "size_bytes", "mtime", "header"]
DIRS_COLS = ["root_id", "root_path", "dir", "code_files", "marker_files", "markers", "exts", "projects",
             "newest_mtime", "code_bytes", "has_readme"]

GOOD_CLAIMS = {"what_it_is": "A fitting script.", "entry_point": "fit/run.py", "language": "python",
               "dependencies": ["numpy"], "related_project": "ras", "state_guess": "partial",
               "files_cited": ["fit/run.py"]}

FILE_TEXT = {"fit/run.py": "print('fit')\n", "fit/util.py": "import numpy\n", "fit/protected/key.py": "k\n",
             "fit/zeta_note.py": "z\n", "plot/p.m": "% plot\n", "copy/run.py": "print('fit')\n"}
