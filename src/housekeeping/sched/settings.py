"""Scheduler settings, parsed once from the [sched] table. No defaults: every key is required."""
from dataclasses import dataclass
from pathlib import Path

from housekeeping.router.settings import CHAT_ROLES


@dataclass(frozen=True)
class SchedSettings:
    router_url: str
    state_file: Path
    claims_file: Path
    log_path: Path
    tick_s: float
    idle_release_s: float
    embed_job_prefix: str
    unload_before_release: bool
    nodes: dict[str, str]          # role -> Slurm node whose GPU that role's model server uses
    ollama_urls: dict[str, str]    # role -> model server base url (from [router.roles])

    @classmethod
    def from_config(cls, cfg: dict) -> "SchedSettings":
        s = cfg["sched"]
        nodes = dict(s["nodes"])
        unknown = set(nodes) - set(CHAT_ROLES)
        if unknown:
            raise ValueError(f"sched.nodes has unknown roles: {sorted(unknown)}; allowed {list(CHAT_ROLES)}")
        if len(set(nodes.values())) != len(nodes):
            raise ValueError("sched.nodes: two roles share a node; give each role its own GPU node")
        if not s["embed_job_prefix"]:
            raise ValueError("sched.embed_job_prefix is empty; it would match every job")
        return cls(
            router_url=s["router_url"].rstrip("/"),
            state_file=Path(s["state_file"]),
            claims_file=Path(s["claims_file"]),
            log_path=Path(s["log"]),
            tick_s=float(s["tick_s"]),
            idle_release_s=float(s["idle_release_s"]),
            embed_job_prefix=s["embed_job_prefix"],
            unload_before_release=bool(s["unload_before_release"]),
            nodes=nodes,
            ollama_urls={r: cfg["router"]["roles"][r]["url"].rstrip("/") for r in nodes},
        )
