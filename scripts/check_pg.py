"""Check whether PostgreSQL got installed by the winget run.
Returns install paths + service status. No side effects.
"""
import os
import subprocess
from pathlib import Path

candidates = [
    Path(r"C:\Program Files\PostgreSQL\17"),
    Path(r"C:\Program Files\PostgreSQL\16"),
    Path(r"C:\Program Files\PostgreSQL\18"),
]

print("=== looking for PostgreSQL binaries ===")
for d in candidates:
    bin = d / "bin" / "psql.exe"
    if bin.exists():
        r = subprocess.run([str(bin), "--version"], capture_output=True, text=True)
        print(f"FOUND: {bin}  ->  {r.stdout.strip() or r.stderr.strip()}")
    else:
        print(f"missing: {bin}")

print("\n=== Windows services ===")
r = subprocess.run(
    ["powershell.exe", "-Command",
     "Get-Service -Name postgresql* -ErrorAction SilentlyContinue | "
     "Format-Table -AutoSize Name, Status, StartType | Out-String"],
    capture_output=True, text=True,
)
print(r.stdout or r.stderr)

print("\n=== env path ===")
print("PG:", os.environ.get("PG", ""))
print("PATH (truncated):", os.environ.get("PATH", "")[:200])
