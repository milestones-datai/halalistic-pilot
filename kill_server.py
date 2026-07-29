"""Kill the local http server on port 8765 (and any python http.server
process left over from the preview)."""
import subprocess
import time

# Try netstat-based kill (more reliable cross-shell on Windows).
cmds = [
    'powershell.exe -Command "Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"',
    'taskkill /F /IM python.exe /FI "WINDOWTITLE eq http.server*"',
]
for c in cmds:
    r = subprocess.run(c, shell=True, capture_output=True, text=True)
    print(c.split()[0], "->", r.returncode)
    if r.stdout: print("  out:", r.stdout.strip()[:200])
    if r.stderr: print("  err:", r.stderr.strip()[:200])
    time.sleep(0.5)

# Verify nothing's left on 8765
r = subprocess.run(
    ['powershell.exe', '-Command', 'Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count'],
    capture_output=True, text=True,
)
print("connections remaining on 8765:", r.stdout.strip())
