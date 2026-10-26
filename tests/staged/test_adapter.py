from dataclasses import dataclass
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from housekeeping.staged.adapter import Atlas, Incompatible, call_supported
from housekeeping.staged.__main__ import stage_drained, doctor_report


def fake_adapter():
    a = Atlas.__new__(Atlas)
    a.m = SimpleNamespace(
        branch_policy_for_rel=lambda rel: None,
        branch_policy_decision=lambda *args: None,
        generated_web_asset_reason=lambda *args: None,
        structured_header_summary=lambda *args: None,
        is_extractable_or_text=lambda ext: ext == ".txt",
        text_size_limit=lambda *args: 1000,
        read_document_text=lambda path, data, ext: (data.decode(), None),
        chunks_for=lambda text: text.splitlines(),
        PDF_DOCX_EXTENSIONS={".pdf", ".docx"},
        assert_vector=lambda vector, cfg: None,
    )
    a.cfg = SimpleNamespace(dim=2)
    return a


CONTRACT = dict(txt_threshold=1000, md_threshold=1000, max_chunks=10)


def test_raw_text_preparation_preserves_digest_and_chunk_order(tmp_path):
    a = fake_adapter()
    path = tmp_path / "a.txt"
    path.write_bytes(b"first\nsecond")
    payload, reason = a.prepare(path, "a.txt", path.stat(), CONTRACT)
    assert reason is None
    assert payload["digest"] == hashlib.sha256(b"first\nsecond").hexdigest()
    assert payload["texts"] == ["first", "second"]
    assert payload["hash_kind"] == "raw_sha256"


def test_structured_sample_retains_atlas_fingerprint_not_raw_hash(tmp_path):
    a = fake_adapter()
    path = tmp_path / "a.mat"
    path.write_bytes(b"binary content not text")
    a.m.structured_header_summary = lambda *args: ("schema summary", {"schema": "fixture"})
    a.m.metadata_fingerprint = lambda *args: "native-fingerprint"
    payload, reason = a.prepare(path, "a.mat", path.stat(), CONTRACT)
    assert payload["digest"] == "native-fingerprint"
    assert payload["texts"] == ["schema summary"]
    assert payload["hash_kind"] == "atlas_metadata_fingerprint"


def test_structured_failure_not_published_as_indexed(tmp_path):
    a = fake_adapter()
    path = tmp_path / "a.mat"
    path.write_text("broken")
    a.m.structured_header_summary = lambda *args: ("bad summary", {"summary_error": "bad format"})
    with pytest.raises(ValueError, match="structured_summary_failed"):
        a.prepare(path, "a.mat", path.stat(), CONTRACT)


def test_embedding_contract_rejects_missing_wrong_dimension_and_nan():
    a = fake_adapter()
    for vectors in ([], [[1.0]], [[float("nan"), 0.0]]):
        with pytest.raises(ValueError):
            a.validate_vectors(["text"], vectors)
    a.validate_vectors(["text"], [[1.0, 0.0]])


def test_writer_unknown_parameter_never_called():
    called = []

    def writer(cur, surprise):
        called.append(True)

    with pytest.raises(Incompatible):
        call_supported(writer, {"cur": None})
    assert not called


def test_native_writer_contract_matches_history_seven_argument_upsert():
    a = fake_adapter()
    a.cfg.model = "fixture"
    a.m.upsert_document = lambda cur, root_id, rel, path, sha, status, lineage: None
    a.m.insert_chunk = lambda cur, doc, index, text, metadata, enabled: None
    a.m.insert_embedding = lambda cur, column, chunk_id, values, cfg: None
    a.writer_check()
    a.m.upsert_document = lambda cur, root_id, rel, path, sha, status, lineage, new_required: None
    with pytest.raises(TypeError):
        a.writer_check()


def test_stage_specific_drain_does_not_hold_gpus_for_publisher():
    status = {"directory": [{"state": "done"}],
              "file": [{"state": "prepared"}], "vector": [{"state": "done"}]}
    assert stage_drained(status, "prepare")
    assert stage_drained(status, "embed")
    assert not stage_drained(status, "publish")
    status["file"].append({"state": "ready"})
    assert not stage_drained(status, "embed")


def test_doctor_report_omits_source_bodies_and_default_credentials(tmp_path):
    p = tmp_path / "ingest.py"
    p.write_text("""def insert_embedding(cur, token='NEVER-SHARE-THIS'):
    secret = 'also-private'
class Embedder:
    def __init__(self, cfg, password='private'):
        pass
""")
    report = repr(doctor_report(p))
    assert "NEVER-SHARE" not in report and "also-private" not in report
    assert "'private'" not in report
