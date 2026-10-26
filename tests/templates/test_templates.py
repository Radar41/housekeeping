"""Render every template with sample values; no cluster needed."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TPL = ROOT / "templates"
RENDER = TPL / "render.py"
PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")

SAMPLE = {
    "entry": "hk templates",
    "default_job": "job",
    "atlas_dsn": "postgresql://user@localhost/atlas",
    "model_port": "8080",
    "model_api_key": "changeme",
    "hf_token": "changeme",
    "job_name": "embed-run",
    "partition": "gpu",
    "gres": "gpu:1",
    "time": "04:00:00",
    "mem": "32G",
    "cpus": "8",
    "repo_dir": "/home/radar-41-0/projects/housekeeping",
    "heartbeat_script": "heartbeat/validator_heartbeat.py",
    "hb_interval": "30",
    "command": "uv run hk embed --apply",
    "embed_cmd": "uv run hk embed --apply",
    "read_cmd": "uv run hk read --apply",
    "code_cmd": "uv run hk code --apply",
    "model_dir": "qwen3-32b",
    "port": "8081",
    "env_file": "%h/.config/model-server/env",
    "exec_start": "/usr/bin/env llama-server -m ${MODEL_DIR}/model.gguf --host 0.0.0.0 --port ${PORT}",
}
VLLM_EXEC = "/usr/bin/env vllm serve ${MODEL_DIR} --host 0.0.0.0 --port ${PORT}"

TEMPLATES = sorted(p for p in TPL.rglob("*") if p.is_file() and p.name != "render.py")


def declared(text):
    m = re.search(r"^#\s*placeholders:\s*(.+)$", text, re.M)
    assert m, "header must list placeholders"
    return {n.strip() for n in m.group(1).split(",")}


def values_for(text):
    return {k: SAMPLE[k] for k in set(PLACEHOLDER.findall(text))}


def run_render(tpl, values, *extra):
    sets = [f"{k}={v}" for k, v in values.items()]
    return subprocess.run(
        [sys.executable, str(RENDER), str(tpl), "--set", *sets, *extra],
        capture_output=True, text=True,
    )


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda p: p.name)
def test_header_lists_every_placeholder(tpl):
    text = tpl.read_text()
    assert declared(text) == set(PLACEHOLDER.findall(text))


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda p: p.name)
def test_render_leaves_no_placeholder(tpl):
    r = run_render(tpl, values_for(tpl.read_text()))
    assert r.returncode == 0, r.stderr
    assert not PLACEHOLDER.search(r.stdout)
    assert chr(0x2014) not in r.stdout


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda p: p.name)
def test_unfilled_placeholder_fails(tpl):
    values = values_for(tpl.read_text())
    values.pop(next(iter(values)))
    r = run_render(tpl, values)
    assert r.returncode != 0
    assert "unfilled" in r.stderr


def test_unknown_key_fails():
    tpl = TPL / "env.example"
    r = run_render(tpl, {**values_for(tpl.read_text()), "typo": "x"})
    assert r.returncode != 0


def test_dry_run_writes_nothing_and_apply_is_idempotent(tmp_path):
    tpl = TPL / "env.example"
    values = values_for(tpl.read_text())
    out = tmp_path / ".env.example"
    log = tmp_path / "logs" / "t.tsv"
    r = run_render(tpl, values, "--out", str(out))
    assert r.returncode == 0 and not out.exists()
    r = run_render(tpl, values, "--apply", "--out", str(out), "--log", str(log))
    assert r.returncode == 0 and out.exists()
    r = run_render(tpl, values, "--apply", "--out", str(out), "--log", str(log))
    assert "unchanged" in r.stdout
    assert len(log.read_text().splitlines()) == 1
    changed = {**values, "model_port": "9999"}
    assert run_render(tpl, changed, "--apply", "--out", str(out), "--log", str(log)).returncode != 0
    assert run_render(tpl, changed, "--apply", "--force", "--out", str(out), "--log", str(log)).returncode == 0
    assert (tmp_path / ".env.example.bak").exists()


@pytest.mark.parametrize("name", ["job.sbatch", "array.sbatch"])
def test_sbatch_syntax(name, tmp_path):
    tpl = TPL / "slurm" / name
    r = run_render(tpl, values_for(tpl.read_text()))
    f = tmp_path / name
    f.write_text(r.stdout)
    assert subprocess.run(["bash", "-n", str(f)]).returncode == 0
    assert "trap finish EXIT" in r.stdout and "--output=" in r.stdout and "logs/%x-" in r.stdout


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze not available")
@pytest.mark.parametrize("exec_start", [SAMPLE["exec_start"], VLLM_EXEC], ids=["llamacpp", "vllm"])
def test_unit_verifies(exec_start, tmp_path):
    tpl = TPL / "systemd" / "model-server.service"
    r = run_render(tpl, {**values_for(tpl.read_text()), "exec_start": exec_start})
    unit = tmp_path / "model-server.service"
    unit.write_text(r.stdout)
    v = subprocess.run(["systemd-analyze", "--user", "verify", str(unit)], capture_output=True, text=True)
    if v.returncode != 0 and ("Failed to connect" in v.stderr or "Failed to initialize manager" in v.stderr):
        pytest.skip("systemd-analyze cannot reach a user manager here")
    assert v.returncode == 0, v.stderr


def test_makefile_dry_run(tmp_path):
    tpl = TPL / "Makefile"
    r = run_render(tpl, values_for(tpl.read_text()))
    (tmp_path / "Makefile").write_text(r.stdout)
    for target in ("run", "test", "slurm"):
        m = subprocess.run(["make", "-n", target], cwd=tmp_path, capture_output=True, text=True)
        assert m.returncode == 0, m.stderr
    assert "uv run hk templates" in subprocess.run(["make", "-n", "run"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "slurm/job.sbatch" in subprocess.run(["make", "-n", "slurm"], cwd=tmp_path, capture_output=True, text=True).stdout


HB_DIR = ROOT / "heartbeat"
# Mirrors lastHeartbeat/heartbeatsForJob in traversal_server_heartbeat_excerpt.txt (lines 393-426).
SERVER_FILE_RE = r"-(%s)-([0-9]+)\.log$"


def run_hb(name, tmp_path, state, env_extra):
    import os
    tpl = TPL / "slurm" / name
    values = {**values_for(tpl.read_text()), "heartbeat_script": str(HB_DIR / "validator_heartbeat.py")}
    text = run_render(tpl, values).stdout
    body = re.search(r"^hb\(\) \{.*?^\}$", text, re.M | re.S).group(0)
    root = tmp_path / "slurm"
    env = {**os.environ, "SLURM_LOG_ROOT": str(root), "SLURM_JOB_NAME": "val", "SLURM_JOB_ID": "4242", **env_extra}
    subprocess.run(["bash", "-c", f'STAGE=embed\n{body}\nhb running\nhb {state}'], env=env, check=True)
    return root


def last_beat_line(log):
    # same selection as lastHeartbeat: last line starting with "{" that includes "seen"
    for line in reversed(log.read_text().rstrip("\n").split("\n")):
        if line.startswith("{") and ('"seen"' in line or '"curvature_progress"' in line):
            return json.loads(line)


def test_job_hb_call_shape(tmp_path):
    root = run_hb("job.sbatch", tmp_path, "ok", {})
    files = list((root / "val").iterdir())
    assert len(files) == 1
    m = re.search(SERVER_FILE_RE % "4242", files[0].name)
    assert m and m.group(2) == "0"
    beat = last_beat_line(files[0])
    assert beat["seen"] == 2 and beat["state"] == "ok"


def test_array_hb_call_shape(tmp_path):
    root = run_hb("array.sbatch", tmp_path, "failed", {"SLURM_ARRAY_JOB_ID": "4200", "SLURM_ARRAY_TASK_ID": "1"})
    files = list((root / "val").iterdir())
    m = re.search(SERVER_FILE_RE % "4200", files[0].name)
    assert m and m.group(2) == "1"
    beat = last_beat_line(files[0])
    assert beat["state"] == "failed" and beat["stage"] == "embed"


@pytest.mark.parametrize("name", ["job.sbatch", "array.sbatch"])
def test_hb_uses_real_validator_interface(name):
    tpl = TPL / "slurm" / name
    text = run_render(tpl, values_for(tpl.read_text())).stdout
    assert "from validator_heartbeat import ValidatorHeartbeat" in text
    assert 'job_id=os.environ["SLURM_JOB_ID"]' in text and 'os.environ.get("SLURM_PROCID", "0")' in text
    assert "h.announce()" in text and "h.beat(force=True)" in text
    assert "--run-id" not in text and "--state" not in text
