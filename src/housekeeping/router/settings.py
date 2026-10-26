"""Router settings, parsed once from the [router] table. No defaults: every key is required."""
from dataclasses import dataclass
from pathlib import Path

ROLES = ("embedder", "reader", "coder")
CHAT_ROLES = ("reader", "coder")


@dataclass(frozen=True)
class RoleCfg:
    name: str
    url: str
    model: str
    timeout_s: float


@dataclass(frozen=True)
class AndromedaCfg:
    url: str
    roles_allowed: tuple[str, ...]
    enabled: bool
    models: dict[str, str]  # optional per-role model override, else the local role model


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    roles: dict[str, RoleCfg]
    andromeda: AndromedaCfg
    overflow_inflight: dict[str, int]
    health_interval_s: float
    health_timeout_s: float
    log_path: Path

    @classmethod
    def from_config(cls, cfg: dict) -> "Settings":
        r = cfg["router"]
        roles = {}
        for name in ROLES:
            c = r["roles"][name]
            roles[name] = RoleCfg(name, c["url"].rstrip("/"), c["model"], float(c["timeout_s"]))
        a = r["andromeda"]
        allowed = tuple(a["roles_allowed"])
        unknown = set(allowed) - set(ROLES)
        if unknown:
            raise ValueError(f"andromeda.roles_allowed has unknown roles: {sorted(unknown)}")
        andromeda = AndromedaCfg(a["url"].rstrip("/"), allowed, bool(a["enabled"]), dict(a.get("models", {})))
        return cls(
            host=r["host"],
            port=int(r["port"]),
            roles=roles,
            andromeda=andromeda,
            overflow_inflight={k: int(v) for k, v in r["overflow_inflight"].items()},
            health_interval_s=float(r["health_interval_s"]),
            health_timeout_s=float(r["health_timeout_s"]),
            log_path=Path(r["log"]),
        )
