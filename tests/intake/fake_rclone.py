"""Fake rclone for tests. Records argv, replays canned output from FAKE_RCLONE_SPEC (json file)."""
import json
import os
import sys

argv = sys.argv[1:]
spec = json.load(open(os.environ["FAKE_RCLONE_SPEC"]))
with open(spec["record"], "a") as f:
    f.write(json.dumps(argv) + "\n")

verb = argv[0]
if verb == "backend":
    remote = argv[2]
    hashes = spec.get("hashes", {}).get(remote, [])
    print(json.dumps({"Hashes": hashes}))
elif verb == "config":
    print(json.dumps(spec.get("config", {})))
elif verb == "copy":
    if "--log-file" in argv:
        lf = argv[argv.index("--log-file") + 1]
        tag = "Skipped copy as --dry-run is set" if "--dry-run" in argv else "Copied (new)"
        with open(lf, "a") as f:
            for name in spec.get("copy_files", []):
                f.write(f"2026/10/25 00:00:00 INFO  : {name}: {tag}\n")
    sys.exit(spec.get("copy_rc", {}).get(argv[1], 0))
elif verb == "check":
    sys.stdout.write(spec.get("combined", {}).get(argv[1], ""))
    sys.stderr.write(spec.get("check_err", {}).get(argv[1], ""))
    sys.exit(spec.get("check_rc", {}).get(argv[1], 0))
elif verb == "size":
    out = spec.get("size", {}).get(argv[-1])
    if out is None:
        sys.stderr.write("directory not found\n")
        sys.exit(3)
    print(json.dumps(out))
else:
    sys.exit(99)
