"""Fixtures for hk tasks: a MemoryStore (or Memgraph when HK_TEST_MEMGRAPH is set), a fake router,
a tmp inventory, and settings that point everything at tmp_path. Nothing here needs the cluster."""
import csv
import hashlib
import json
import os
import uuid

import httpx
import pytest

from housekeeping.tasks.dispatch import Settings
from housekeeping.tasks.store import MemoryStore

from tasks_fixtures import DIRS_COLS, FILE_TEXT, FILES_COLS, GOOD_CLAIMS, SECRET  # noqa: E402


def write_tsv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])


@pytest.fixture
def root(tmp_path):
    """A real folder tree, so the code-card check can hash files."""
    r = tmp_path / "u"
    for rel, text in FILE_TEXT.items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (r / "sim").mkdir()
    return str(r)


@pytest.fixture
def inventory(tmp_path, root):
    d = tmp_path / "inventory"
    d.mkdir()
    headers = {"fit/run.py": "#!/usr/bin/env python \\n \"\"\"Fit the model.\"\"\"", "fit/util.py": "import numpy",
               "fit/protected/key.py": "hidden", "fit/zeta_note.py": "mentions zeta-codename here",
               "plot/p.m": "% plot", "copy/run.py": "#!/usr/bin/env python"}
    files = []
    for rel, text in FILE_TEXT.items():
        files.append({"root_id": "21", "root_path": root, "rel_path": rel, "dir": rel.rsplit("/", 1)[0],
                      "ext": rel.rsplit(".", 1)[1], "header": headers[rel],
                      "sha256": hashlib.sha256(text.encode()).hexdigest()})
    write_tsv(d / "files_2026-10-26.tsv", FILES_COLS, files)
    dirs = [
        {"root_id": "21", "root_path": root, "dir": "fit", "code_files": "4", "exts": "py"},
        {"root_id": "21", "root_path": root, "dir": "plot", "code_files": "1", "exts": "m"},
        {"root_id": "21", "root_path": root, "dir": "sim", "code_files": "2", "exts": "py"},
    ]
    write_tsv(tmp_path / "dirs.tsv", DIRS_COLS, dirs)
    return d


@pytest.fixture
def settings(tmp_path, inventory):
    return Settings(log=tmp_path / "logs/tasks.tsv", data_dir=tmp_path / "data", inventory_dir=str(inventory),
                    ns="test", card_max_chars=1200, max_attempts=2, poll_interval_s=0.01,
                    router_url="http://router.test", request_timeout_s=5, memgraph_uri="bolt://unused",
                    memgraph_user_env="X_UNSET", memgraph_password_env="X_UNSET")


def _memgraph_store():
    from housekeeping.tasks.store import MemgraphStore

    s = MemgraphStore(os.environ["HK_TEST_MEMGRAPH"], ns=f"test-{uuid.uuid4().hex[:8]}")
    return s


@pytest.fixture(params=["memory", "memgraph"])
def store(request):
    if request.param == "memory":
        yield MemoryStore("test")
        return
    if not os.environ.get("HK_TEST_MEMGRAPH"):
        pytest.skip("set HK_TEST_MEMGRAPH=bolt://127.0.0.1:7687 to run against Memgraph")
    s = _memgraph_store()
    try:
        yield s
    finally:
        s.drop_ns(s.ns)
        s.close()


class FakeRouter:
    """Answers /v1/chat/completions. mode: good, nojson, badclaims, huge, timeout, http500."""

    def __init__(self):
        self.mode = "good"
        self.seen: list[dict] = []

    def reply(self, content):
        return httpx.Response(200, json={"model": "qwen3.5:9b", "choices": [{"message": {"content": content}}],
                                         "echo": SECRET})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.seen.append(body)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if self.mode == "http500":
            return httpx.Response(500, json={"error": "down"})
        if self.mode == "nojson":
            return self.reply("I could not decide.")
        claims = dict(GOOD_CLAIMS, summary="Fits a model to data.")
        if self.mode == "badclaims":
            claims["state_guess"] = "excellent"
        if self.mode == "huge":
            claims["what_it_is"] = "x" * 2000
        return self.reply("<think>hmm</think>Here:\n" + json.dumps(claims))


@pytest.fixture
def router():
    return FakeRouter()


@pytest.fixture
def client(router):
    with httpx.Client(transport=httpx.MockTransport(router)) as c:
        yield c


@pytest.fixture
def names():
    return ["zeta-codename"]
