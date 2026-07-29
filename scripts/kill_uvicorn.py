"""Kill all uvicorn processes (simple version)."""
import subprocess

# Find and kill
r = subprocess.run(
    ["powershell.exe", "-Command",
     "Get-CimInstance Win32_Process | "
     "Where-Object { $_.CommandLine -like '*uvicorn*' } | "
     "ForEach-Object { Write-Host ('killing PID ' + $_.ProcessId + ': ' + $_.CommandLine); "
     "  Stop-Process -Id $_.ProcessId -Force }"],
    capture_output=True, text=True,
)
print("OUT:", r.stdout)
print("ERR:", r.stderr)
