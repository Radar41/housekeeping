"""The only place the scheduler touches Slurm. Tests use FakeSlurm with the same three methods."""
import getpass
import subprocess
from dataclasses import dataclass

RUNNING_STATES = {"RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED", "RESIZING", "SIGNALING", "STAGE_OUT"}
HELD_REASONS = {"JobHeldUser", "JobHeldAdmin"}


@dataclass(frozen=True)
class Job:
    id: str
    name: str
    state: str
    node: str     # allocated node if running, else the requested node (--nodelist)
    reason: str

    @property
    def on_gpu(self) -> bool:
        return self.state in RUNNING_STATES

    @property
    def held(self) -> bool:
        return self.state == "PENDING" and self.reason in HELD_REASONS


# %N allocated nodes, %n requested nodes. Pending jobs only have %n.
SQUEUE_FMT = "%i|%j|%T|%N|%n|%r"


def parse_squeue(text: str) -> list[Job]:
    jobs = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) != 6:
            raise ValueError(f"unexpected squeue line: {line!r}")
        jid, name, state, alloc, req, reason = (p.strip() for p in parts)
        node = alloc if alloc and alloc != "(null)" else req
        jobs.append(Job(jid, name, state, node, reason))
    return jobs


class Slurm:
    def __init__(self, user: str | None = None):
        self.user = user or getpass.getuser()

    def _run(self, *args: str) -> str:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(args)} failed rc={r.returncode}: {r.stderr.strip()}")
        return r.stdout

    def jobs(self) -> list[Job]:
        return parse_squeue(self._run("squeue", "-h", "-u", self.user, "-o", SQUEUE_FMT))

    def requeuehold(self, job_id: str) -> None:
        """Stop a running job, put it back in the queue held. Its GPU is freed when the job exits."""
        self._run("scontrol", "requeuehold", job_id)

    def hold(self, job_id: str) -> None:
        """Keep a pending job from starting."""
        self._run("scontrol", "hold", job_id)

    def release(self, job_id: str) -> None:
        self._run("scontrol", "release", job_id)
