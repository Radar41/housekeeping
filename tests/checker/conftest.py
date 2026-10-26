import subprocess

import pytest

from housekeeping import config

README = """# demo

## What it is
A demo.

## State
working

## How to run
make run

## Depends on
nothing
"""
MAKEFILE = ".RECIPEPREFIX = >\n.PHONY: run test slurm\nrun:\n> echo run\ntest:\n> echo test\nslurm:\n> echo slurm\n"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def build_repo(root, name="good", files=None, remove=()):
    """Passing repo, with files overriding or adding content and remove dropping names."""
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    base = {
        "README.md": README,
        "pyproject.toml": '[project]\nname = "demo"\nrequires-python = ">=3.12"\n',
        "uv.lock": "version = 1\n",
        "app.py": "import os\nKEY = os.environ.get('KEY')\n",
        ".env.example": "KEY=\n",
        ".gitignore": ".env\n",
        "Makefile": MAKEFILE,
    }
    base.update(files or {})
    for rel in remove:
        base.pop(rel, None)
    for rel, text in base.items():
        (repo / rel).write_text(text, encoding="utf-8")
    git(repo, "add", "-A", "-f")
    return repo


@pytest.fixture
def hk_config(tmp_path, monkeypatch):
    names = tmp_path / "protected_names.txt"
    names.write_text("alpha_core\nBeta_Vault\n\n", encoding="utf-8")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\nlogs = "{tmp_path / "logs"}"\n'
        f'[checker]\nprotected_names_file = "{names}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HK_CONFIG", str(cfg))
    config.load.cache_clear()
    yield tmp_path
    config.load.cache_clear()
