"""Config is one file, read once. Path from HK_CONFIG, else config.toml at repo root."""
import os
import tomllib
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def load() -> dict:
    path = Path(os.environ.get("HK_CONFIG", REPO_ROOT / "config.toml"))
    with open(path, "rb") as f:
        return tomllib.load(f)
