"""Launcher that runs preview_ui.py with the venv python."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
SCRIPT = ROOT / "scripts" / "preview_ui.py"

r = subprocess.run([str(PY), str(SCRIPT)], cwd=str(ROOT))
sys.exit(r.returncode)
