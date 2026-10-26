# 8. Move files

Not now. On hold until you decide to file things. Nothing in processes 0 to 7 moves a file.

When you are ready, the mover reads a decision table (columns `chain_id, source_path, decision, project, target_repo, year, is_canonical`; decisions `live, parked, protected, absorbed`), moves with a hash check, logs every move to `logs/moves.tsv`, writes a provenance row to Atlas, and can undo.

```bash
make run MOD=mover ARGS="plan table.csv"                 # dry run
make run MOD=mover ARGS="apply table.csv --apply"        # move
make run MOD=mover ARGS="undo logs/moves.tsv --apply"    # put back
```

Before the first `--apply`: run process 1 again and save it, so the provenance baseline is from before any moves.
