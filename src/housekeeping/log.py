"""One line per action, append only: ISO timestamp, action, source, target, identifier (TSV)."""
from datetime import datetime, timezone
from pathlib import Path


def log_line(path, action, source, target, ident) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(p, "a", encoding="utf-8") as f:
        f.write("\t".join([ts, action, str(source), str(target), str(ident)]) + "\n")
