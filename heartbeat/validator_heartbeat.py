"""validator_heartbeat.py — per-rank telemetry for run_validator_server.mjs

Stdlib only. Every call is fire-and-forget with a short timeout and a blanket
except: monitoring must never kill the pipeline.

Usage inside a worker:

    from validator_heartbeat import ValidatorHeartbeat

    hb = ValidatorHeartbeat(job_id=os.environ.get("SLURM_JOB_ID", "dev"),
                            rank=int(os.environ.get("SLURM_PROCID", 0)))
    hb.announce(assigned_row_count=len(shard))   # startup record (R1)

    for batch in loader:
        ...
        hb.beat(rows_attempted=att, rows_completed=done,
                rows_failed=failed, rows_committed=committed,
                unique_output_ids=len(seen_ids))   # every batch or every ~15s

    hb.beat(rows_expected=len(shard), rows_committed=committed,
            nan_count=nan_total)                   # final counters (R5)

Counter names the server understands:
  rows_discovered rows_queued rows_attempted rows_completed rows_failed
  rows_retried rows_committed unique_output_ids rows_expected nan_count
"""

import json
import os
import socket
import time
import urllib.request


class ValidatorHeartbeat:
    def __init__(self, job_id, rank=0, url="http://localhost:7788",
                 timeout=1.5, min_interval=5.0):
        self.job_id = str(job_id)
        self.rank = int(rank)
        self.url = url.rstrip("/") + "/api/heartbeat"
        self.timeout = timeout
        self.min_interval = min_interval
        self._last = 0.0

    # -- transport ----------------------------------------------------------

    def _post(self, payload):
        try:
            req = urllib.request.Request(
                self.url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=self.timeout).read()
        except Exception:
            pass  # never propagate into the job

    # -- startup record (rule R1) -------------------------------------------

    def announce(self, **extra):
        """Send the per-worker startup record. Auto-fills from torch/NVML
        when available; anything passed in **extra wins."""
        rec = {
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "rank": self.rank,
            "local_rank": int(os.environ.get("LOCAL_RANK",
                              os.environ.get("SLURM_LOCALID", 0))),
            "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES"),
        }
        try:
            import torch
            rec["torch_cuda_device_count"] = torch.cuda.device_count()
            if torch.cuda.is_available():
                i = torch.cuda.current_device()
                rec["selected_cuda_device"] = f"cuda:{i}"
                rec["gpu_model"] = torch.cuda.get_device_name(i)
                p = torch.cuda.get_device_properties(i)
                rec["compute_capability"] = f"{p.major}.{p.minor}"
                rec["vram_total_mb"] = p.total_memory // (1024 * 1024)
                try:
                    import subprocess
                    rec["gpu_uuid"] = subprocess.check_output(
                        ["nvidia-smi", f"--id={i}", "--query-gpu=uuid",
                         "--format=csv,noheader"], timeout=3,
                    ).decode().strip()
                except Exception:
                    pass
        except Exception:
            pass
        rec.update(extra)
        self._post({"job_id": self.job_id, "rank": self.rank, "startup": rec})

    # -- progress counters (rules R2–R6) --------------------------------------

    def beat(self, force=False, **counters):
        """Send progress counters. Rate-limited to min_interval unless force."""
        t = time.time()
        if not force and t - self._last < self.min_interval:
            return
        self._last = t
        self._post({"job_id": self.job_id, "rank": self.rank,
                    "counters": counters})
