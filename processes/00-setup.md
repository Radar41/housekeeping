# 0. Set up this repo

One-time, on head. About 10 minutes.

## Steps

1. Get the code.
   ```bash
   mkdir -p ~/projects && cd ~/projects
   gh repo clone Radar41/housekeeping || (cd housekeeping && git pull)
   cd housekeeping
   ```
2. Install the Python environment.
   ```bash
   uv sync
   ```
3. Make your local config. `config.toml` is gitignored; edit paths if your home is not `/home/radar-41-0`.
   ```bash
   cp -n config.example.toml config.toml
   ```
4. Point the tools at Atlas. Put the DSN in `.env` (gitignored) so every shell gets it.
   ```bash
   echo "ATLAS_DSN=postgresql:///atlas" >> .env      # use your real DSN
   set -a; . ./.env; set +a
   psql "$ATLAS_DSN" -c "select count(*) from atlas.atlas_document;"   # expect about 232384
   ```
5. Write the protected names file. One name per line. It never leaves head and is gitignored.
   ```bash
   nano protected_names.txt
   git check-ignore protected_names.txt     # must print the file name
   ```
6. Run the tests.
   ```bash
   make test
   ```

## Done when

- `make test` passes.
- The `psql` count in step 4 returns a number.
- `git status` shows no `config.toml`, `.env`, or `protected_names.txt`.

## Every later session

```bash
cd ~/projects/housekeeping && git pull && uv sync
set -a; . ./.env; set +a
```
