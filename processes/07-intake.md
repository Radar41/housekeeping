# 7. Bring in the Windows drives

One-time per Windows machine, then check. Copies only: `rclone copy` and `rclone check --one-way`. The tool refuses `sync`, `move`, and `delete`. Files land untouched in `~/inbox/<machine>/<drive>/`.

## Steps

1. Make the rclone remotes by hand, once. The tool never creates them.
   ```bash
   rclone config          # e.g. winbox-c (SFTP or SMB to the Windows machine), cloud-remote
   rclone lsd winbox-c:/  # must list folders
   ```
2. Name them in `config.toml`:
   ```toml
   [intake]
   machine = "winbox"
   target_root = "/home/radar-41-0/inbox"
   sources = [{ name = "c", remote = "winbox-c:/" }, { name = "cloud", remote = "cloud-remote:/" }]
   ```
3. Dry run, then copy, then check, then report.
   ```bash
   make run MOD=intake ARGS="copy"
   make run MOD=intake ARGS="copy --apply"
   make run MOD=intake ARGS="check"
   make run MOD=intake ARGS="report"
   ```
4. Sweep the new inbox into Atlas with the existing DFSA sweep, then note its new root id. That root id is what `inbox-three-way` takes:
   ```bash
   make run MOD=atlas-queries ARGS="inbox-three-way --root-id <new id>"
   ```

## Done when

`report` shows every source with a `CHECK.txt` result of zero differences.
