"""Reuse installed Atlas extraction/model/writer functions; refuse unknown interfaces.

No monkeypatching, source rewriting, artificial embeddings, or full-sweep mode.
Importing this adapter does not connect to a database or load a model.
"""
import dataclasses
import hashlib
import importlib.util
import inspect
import json
import math
import os
from pathlib import Path
import sys


class Incompatible(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def call_supported(fn, values):
    """Bind only known parameter names. Fail before calling any unknown API."""
    kwargs = {}
    for name, p in inspect.signature(fn).parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD, p.POSITIONAL_ONLY):
            raise Incompatible(f"unsupported signature for {fn.__name__}")
        if name in values:
            kwargs[name] = values[name]
        elif p.default is p.empty:
            raise Incompatible(f"{fn.__name__}: unknown required parameter {name}")
    return fn(**kwargs)


REQUIRED = (
    "branch_policy_for_rel", "branch_policy_decision", "excluded",
    "generated_web_asset_reason", "structured_header_summary", "header_only_format",
    "is_extractable_or_text", "text_size_limit", "metadata_fingerprint",
    "read_document_text", "chunks_for", "assert_vector",
    "upsert_document", "insert_chunk", "insert_embedding",
)


class Atlas:
    def __init__(self, path):
        if os.getenv("ATLAS_FULL_SWEEP", "0") != "0":
            raise Incompatible("unset ATLAS_FULL_SWEEP; staged ingestion never uses full sweep")
        self.path = Path(path).resolve()
        sys.path.insert(0, str(self.path.parent))
        spec = importlib.util.spec_from_file_location("hk_installed_atlas", self.path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        self.m = mod
        for name in REQUIRED:
            if not callable(getattr(mod, name, None)):
                raise Incompatible(f"missing Atlas function: {name}")
        # Locate a uniquely typed config factory instead of inventing its name.
        factories = []
        for name, obj in vars(mod).items():
            if not inspect.isfunction(obj) or obj.__module__ != mod.__name__:
                continue
            sig = inspect.signature(obj)
            annotation = sig.return_annotation
            if annotation is getattr(mod, "BackendConfig", None) or annotation == "BackendConfig":
                if all(p.default is not p.empty for p in sig.parameters.values()):
                    factories.append(obj)
        if len(factories) != 1:
            raise Incompatible("expected one zero-required-argument BackendConfig factory; see doctor report")
        self.cfg = factories[0]()
        classes = [o for o in vars(mod).values() if inspect.isclass(o)
                   and o.__module__ == mod.__name__
                   and callable(getattr(o, "sentence_transformers_embed_batch", None))]
        if len(classes) != 1:
            raise Incompatible("expected one Atlas embedder class")
        self.embedder_class = classes[0]
        self._embedder = None
        self.source_fingerprints = {"atlas/" + p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in sorted(self.path.parent.glob("*.py"))}
        self.source_fingerprints.update({
            "stage/" + p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(__file__).parent.glob("*.py"))
        })

    def contract(self, excludes, protected_names, txt_threshold, md_threshold, max_chunks):
        # Device/batch size vary per worker, but embedding semantics must not.
        cfg = dataclasses.asdict(self.cfg)
        for key in ("device", "batch_size"):
            cfg.pop(key, None)
        # Hash local source; don't ship it or model endpoints in status output.
        settings = dict(backend=cfg, source=self.source_fingerprints, excludes=excludes,
                        protected_names=protected_names, txt_threshold=txt_threshold,
                        md_threshold=md_threshold, max_chunks=max_chunks)
        settings["signature"] = digest(json.dumps(settings, sort_keys=True, default=str))
        return settings

    def decision(self, rel, path):
        pol = self.m.branch_policy_for_rel(rel)
        return self.m.branch_policy_decision(rel, path, pol) if pol else None

    def noise_dir(self, rel):
        probe = rel.rstrip("/") + "/_"
        pol = self.m.branch_policy_for_rel(probe)
        if pol is None or pol.name != "radar_41_purge_selective":
            return False
        decision = self.m.branch_policy_decision(probe, Path("_"), pol)
        return decision.action == "skip" and decision.reason == "branch_policy_noise_subtree"

    def prepare(self, path, rel, st, contract):
        """CPU only: preserve the existing Atlas sampling and digest definitions."""
        m = self.m
        decision = self.decision(rel, path)
        if decision and decision.action == "skip":
            return None, decision.reason
        reason = m.generated_web_asset_reason(rel, path, st)
        if reason:
            return None, reason
        summary = m.structured_header_summary(path, rel, st, decision)
        if summary is not None:
            text, metadata = summary
            # Do not turn a broken structured extractor into an indexed success.
            if metadata.get("summary_error"):
                raise ValueError("structured_summary_failed")
            fingerprint = m.metadata_fingerprint(path, rel, st, text)
            pieces = [text]
            hash_kind = "atlas_metadata_fingerprint"
        else:
            ext = path.suffix.lower()
            if not m.is_extractable_or_text(ext):
                return None, "unsupported_type"
            limit = m.text_size_limit(path, contract["txt_threshold"], contract["md_threshold"])
            if limit is not None and st.st_size > limit:
                return None, "oversize"
            data = path.read_bytes()
            fingerprint = hashlib.sha256(data).hexdigest()
            text, error = m.read_document_text(path, data, ext)
            if error:
                raise ValueError("extraction_failed")
            pieces = m.chunks_for(text)
            hash_kind = "raw_sha256"
            metadata = {
                "content_lane": "full_text", "ingest_mode": decision.ingest_mode if decision else "full_text",
                "branch_policy": decision.reason if decision else None, "schema_version": 1,
                "extracted_from": ext.lstrip(".") if ext in m.PDF_DOCX_EXTENSIONS else None,
            }
        if not pieces:
            return None, "empty_content"
        if len(pieces) > contract["max_chunks"]:
            return None, "too_many_chunks"
        return dict(digest=fingerprint, hash_kind=hash_kind, metadata=metadata, texts=pieces,
                    keys=[digest(text) for text in pieces]), None

    def embed(self, texts):
        if self._embedder is None:
            self._embedder = self.embedder_class(self.cfg)
        return self._embedder.embed_batch(texts)

    def validate_vectors(self, texts, vectors):
        if len(vectors) != len(texts):
            raise ValueError("embedding batch cardinality mismatch")
        for vector in vectors:
            if len(vector) != self.cfg.dim or not all(math.isfinite(x) for x in vector):
                raise ValueError("invalid embedding dimension or non-finite vector")
            self.m.assert_vector(vector, self.cfg)

    def writer_check(self):
        # Bind sentinels only; never execute functions during validation.
        values = self.writer_values(None, None, None, None, None, None)
        for name in ("insert_embedding",):
            sig = inspect.signature(getattr(self.m, name))
            unknown = [n for n, p in sig.parameters.items()
                       if n not in values and p.default is p.empty]
            if unknown:
                raise Incompatible(f"{name}: unsupported parameters {unknown}")
        for name, count in (("upsert_document", 7), ("insert_chunk", 6)):
            inspect.signature(getattr(self.m, name)).bind(*([None] * count))

    def writer_values(self, cur, column, chunk_id, vector, document_id, chunk_text):
        return dict(cur=cur, column=column, chunk_id=chunk_id, values=vector, vector=vector, vec=vector,
                    embedding=vector, cfg=self.cfg, config=self.cfg, document_id=document_id,
                    chunk_text=chunk_text, model=self.cfg.model)

    def write_embedding(self, cur, column, chunk_id, vector, document_id, chunk_text):
        return call_supported(self.m.insert_embedding,
                              self.writer_values(cur, column, chunk_id, vector, document_id, chunk_text))
