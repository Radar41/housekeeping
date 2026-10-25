# LAYOUT: one rule per folder. Agents and the mover follow this file.
projects/    one folder per live project; each must be a git repo pushed to GitHub. A folder without .git is a bug to report.
archive/     finished or parked work, filed by year (archive/2025, archive/2026). Read-only after filing; never edited or moved again.
protected/   local only. Never read, listed, moved, or referenced by any tool in a GitHub repo. Excluded pattern: ~/protected/**
data/        datasets and exports, one subfolder per source. Never inside a repo.
models/      model weights (GGUF, safetensors), one folder per model.
inbox/       Windows migration lands here untouched, as inbox/<machine>/<drive>/. Nothing is sorted in place.
quarantine/  suspected junk and duplicates, one dated folder plus manifest per batch. Deleted only by Adrian, by hand.
Rules: a file lives in exactly one folder. Revisions live in Atlas version chains, not as copies on disk.
No path in this tree may reference C:\ or OneDrive.
