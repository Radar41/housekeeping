#!/usr/bin/env bash
# Rebuild one old pipeline folder as a clean repo under ~/projects. Process 5 in INSTRUCTIONS.md.
#
# usage: scripts/rebuild-pipeline.sh SRC_DIR NAME [--entry "CMD"] [--apply]
#
# Copies code, configs, and docs (rules in scripts/rebuild-exclude.txt, files over 5 MB skipped)
# into $HK_PROJECTS/NAME, adds the standard files, makes a first commit, and runs the checker.
# The source folder is only read. Nothing is pushed: the script prints the gh command for that.
# Dry run by default: without --apply it prints what it would copy and stops.
#
# env: HK_PROJECTS (default ~/projects), HK_DATA (default ~/data/housekeeping),
#      HK_PROTECTED (default ~/protected), HK_MAX_SIZE (default 5M)
set -euo pipefail

HK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECTS="${HK_PROJECTS:-$HOME/projects}"
DATA="${HK_DATA:-$HOME/data/housekeeping}"
PROTECTED="${HK_PROTECTED:-$HOME/protected}"
MAX_SIZE="${HK_MAX_SIZE:-5M}"
NAMES="$HK/protected_names.txt"
EXCLUDE="$HK/scripts/rebuild-exclude.txt"

die() { echo "rebuild: $*" >&2; exit 2; }

SRC="" NAME="" ENTRY="" APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --entry) shift; ENTRY="${1:-}" ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) if [ -z "$SRC" ]; then SRC="$1"; elif [ -z "$NAME" ]; then NAME="$1"; else die "too many arguments"; fi ;;
  esac
  shift
done
[ -n "$SRC" ] && [ -n "$NAME" ] || die "usage: scripts/rebuild-pipeline.sh SRC_DIR NAME [--entry CMD] [--apply]"
[[ "$NAME" =~ ^[A-Za-z0-9._-]+$ ]] || die "NAME may only use letters, digits, dot, dash, underscore"
[ -d "$SRC" ] || die "not a directory: $SRC"
command -v rsync >/dev/null || die "rsync is not installed"
SRC="$(cd "$SRC" && pwd -P)"
DST="$PROJECTS/$NAME"

# Protected material never goes into a repo.
case "$SRC/" in "$(realpath -m "$PROTECTED")"/*) die "source is under the protected folder; refusing" ;; esac
[ -f "$NAMES" ] || die "protected names file missing ($NAMES); see processes/00-setup.md step 5"
if grep -qiF -f <(grep -v '^[[:space:]]*$' "$NAMES") <<<"$SRC"; then die "source path matches a protected name; refusing"; fi

# Idempotent: a finished rebuild of the same source is left alone; anything else at DST is refused.
if [ -e "$DST" ]; then
  if [ -f "$DST/SOURCES.txt" ] && grep -qxF "source: $SRC" "$DST/SOURCES.txt"; then
    if git -C "$DST" rev-parse -q --verify HEAD >/dev/null 2>&1; then
      echo "unchanged: $DST was already rebuilt from $SRC"; exit 0
    fi
    die "$DST is an unfinished earlier run (no commit). Check it, remove it by hand, and run again"
  fi
  die "$DST already exists and was not rebuilt from $SRC; pick another NAME"
fi
git var GIT_AUTHOR_IDENT >/dev/null 2>&1 || die "git has no user.name/user.email; set them with git config --global first"

RSYNC=(rsync -a --prune-empty-dirs --max-size="$MAX_SIZE" --exclude-from="$EXCLUDE")
echo "source:      $SRC"
echo "destination: $DST"
PLAN="$("${RSYNC[@]}" -n --out-format='%n' "$SRC/" "$DST/" | grep -v '/$' || true)"
COUNT="$(printf '%s' "$PLAN" | grep -c . || true)"
echo "files to copy: $COUNT"
printf '%s\n' "$PLAN" | head -50 | sed 's/^/  /'
[ "$COUNT" -gt 50 ] && echo "  ... and $((COUNT - 50)) more"
SKIPPED="$(find "$SRC" -type f -size +"$MAX_SIZE" -not -path '*/.git/*' 2>/dev/null | wc -l)"
echo "skipped for size (over $MAX_SIZE): $SKIPPED"
[ "$COUNT" -gt 0 ] || die "nothing to copy"
if [ "$APPLY" -ne 1 ]; then echo "dry run: add --apply to copy and set up the repo"; exit 0; fi

mkdir -p "$PROJECTS" "$DATA"
"${RSYNC[@]}" "$SRC/" "$DST/"
trap 'echo "rebuild: stopped before the commit; $DST is incomplete. Remove it by hand and run again." >&2' ERR
cd "$DST"
NOW="$(date -u +%FT%TZ)"

# Provenance: where it came from and the hash of every copied file, so Atlas rows can be matched by sha256.
{
  echo "source: $SRC"
  echo "host: $(hostname)"
  echo "rebuilt_at: $NOW"
  echo "rules: scripts/rebuild-exclude.txt, max size $MAX_SIZE"
  echo "files: $COUNT"
} > SOURCES.txt
find . -type f -not -path './.git/*' -not -name SOURCES.txt -not -name SOURCES.sha256 -print0 \
  | sort -z | xargs -0 sha256sum > SOURCES.sha256

# Keep an old README next to the new one.
if [ -f README.md ]; then mv README.md README.source.md; fi

# Python: add pyproject.toml and uv.lock only if there are none.
HAS_PY=0; find . -name '*.py' -not -path './.git/*' | grep -q . && HAS_PY=1
if [ "$HAS_PY" -eq 1 ] && [ ! -f pyproject.toml ]; then
  uv init --bare --no-workspace --no-pin-python --python 3.12 --name "$(echo "$NAME" | tr 'A-Z.' 'a-z-')" >/dev/null
fi
if [ -f pyproject.toml ] && [ ! -f uv.lock ]; then uv lock -q || echo "uv lock failed; fix pyproject.toml by hand"; fi

# Entry point for make run.
if [ -z "$ENTRY" ]; then
  if [ -f run.sh ]; then ENTRY="bash run.sh"
  elif [ -f main.py ]; then ENTRY="python main.py"
  else ENTRY="echo 'set the entry command in Makefile'"
  fi
fi
if [ ! -f Makefile ]; then
  python3 "$HK/templates/render.py" "$HK/templates/Makefile" --set "entry=$ENTRY" default_job=main --out Makefile --apply >/dev/null
fi

# .gitignore and .env.example.
touch .gitignore
for p in .venv/ .env logs/ data/ __pycache__/ '*.pyc'; do grep -qxF "$p" .gitignore || echo "$p" >> .gitignore; done
VARS="$(grep -rhoE "(os\.environ(\.get)?\(?\[?|os\.getenv\(|process\.env\.)['\"]?[A-Z][A-Z0-9_]+" --include='*.py' --include='*.js' --include='*.mjs' --include='*.ts' . 2>/dev/null \
  | grep -oE '[A-Z][A-Z0-9_]+$' | sort -u || true)"
if [ -n "$VARS" ] && [ ! -f .env.example ]; then
  { echo "# Variables this code reads. Copy to .env (gitignored) and fill in."; printf '%s=\n' $VARS; } > .env.example
fi

# Languages for the README.
LANGS="$(find . -type f -not -path './.git/*' | sed -n 's/.*\.\(py\|m\|rs\|sh\|jsx\|mjs\|js\|ts\|cpp\|c\|jl\|r\|sbatch\)$/\1/p' | sort | uniq -c | sort -rn | awk '{printf "%s%s (%s)", sep, $2, $1; sep=", "}')"
cat > README.md <<README
# $NAME

## What it is
Rebuilt on $NOW from \`$SRC\`. Describe what the pipeline does here; the old README, if any, is \`README.source.md\`.

## State
partial

## How to run
make run ARGS="..."    runs: $ENTRY
make test              runs the tests
make slurm JOB=main    submits slurm/main.sbatch (render it from housekeeping/templates/slurm/job.sbatch first)

## Depends on
Files by type: ${LANGS:-none found}. See SOURCES.txt for where this came from.
README

git init -q -b main
git add -A
git commit -q -m "Rebuild from $SRC" -m "Copied by housekeeping scripts/rebuild-pipeline.sh at $NOW. Source left in place."
COMMIT="$(git rev-parse --short HEAD)"
trap - ERR

printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$NOW" "$NAME" "$SRC" "$DST" "$COUNT" "$COMMIT" >> "$DATA/rebuilds.tsv"
echo "copied $COUNT files, commit $COMMIT, logged to $DATA/rebuilds.tsv"
echo
echo "checker:"
(cd "$HK" && uv run -q hk checker "$DST") || true
echo
echo "next: fix any FAIL above, edit README.md, then push:"
echo "  gh repo create Radar41/$NAME --private --source $DST --push"
