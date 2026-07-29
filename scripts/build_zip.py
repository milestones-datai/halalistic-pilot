"""Build halalistic-stageN.zip — source-only, excludes .venv / .git /
__pycache__ / generated files. Run from the repo root with the venv
python (no extra deps needed). Pass the stage number as arg 1 (default 11).
"""
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repo root (parent of scripts/)
STAGE = int(sys.argv[1]) if len(sys.argv) > 1 else 11
OUT = ROOT / f"halalistic-stage{STAGE}.zip"

EXCLUDE_DIRS = {".venv", ".git", "__pycache__", "node_modules", ".pytest_cache"}
EXCLUDE_FILES = {
    "halalistic-stage9.zip",
    "halalistic-stage10.zip",
    "halalistic-stage11.zip",
    "tests-output.txt",
    "vapid_keys.json",
    "vapid_keys.json.tmp",
    ".env",
    ".env.local",
}


def main() -> int:
    if OUT.exists():
        OUT.unlink()
    count = 0
    # os.walk + manual dir pruning is much faster than rglob on a
    # directory that contains a junction to a 6k-file .venv.
    with zipfile.ZipFile(OUT, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            # Mutate dirnames IN PLACE to prune excluded dirs.
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for name in filenames:
                if name in EXCLUDE_FILES:
                    continue
                if name.endswith(".pyc"):
                    continue
                p = Path(dirpath) / name
                arcname = p.relative_to(ROOT).as_posix()
                zf.write(p, arcname)
                count += 1
    size = OUT.stat().st_size
    print(f"wrote {OUT.name} ({count} files, {size/1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
