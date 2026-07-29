"""Test the SSH handshake to GitHub. If it works, we can use SSH auth
to push the project. If it fails, we surface the exact error so the
user can fix it (most common: SSH agent not running, key not added
to GitHub, or the public key pasted didn't match what we generated).
"""
import os
import subprocess
import sys
from pathlib import Path

# Refresh PATH so ssh / ssh-add / git are reachable (winget puts them
# under Git for Windows which is not on PATH for the current shell).
extra = [
    r"C:\Program Files\Git\usr\bin",
    r"C:\Program Files\Git\mingw64\bin",
    r"C:\Program Files\Git\cmd",
    r"C:\Program Files\GitHub CLI",
]
for p in extra:
    if p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")

SSH = "C:/Program Files/Git/usr/bin/ssh.exe"
SSH_ADD = "C:/Program Files/Git/usr/bin/ssh-add.exe"
KEY = Path(os.environ["USERPROFILE"]) / ".ssh" / "id_ed25519"

# 1) Try to start the SSH agent (silently — may need admin; OK if it fails)
r = subprocess.run(
    ["powershell.exe", "-Command",
     "Start-Service ssh-agent -ErrorAction SilentlyContinue"],
    capture_output=True, text=True,
)
print("ssh-agent service start:", r.returncode, r.stderr.strip()[:100])

# 2) Try to add the key to the agent (silently — may fail without admin)
r = subprocess.run([SSH_ADD, str(KEY)], capture_output=True, text=True)
print("ssh-add:", r.returncode, r.stdout.strip(), r.stderr.strip()[:100])

# 3) Test the handshake. We use -i to specify the key explicitly, so
# this works even if the agent isn't running. The -T probe exits
# non-zero (255) on success too — we look for the welcome line.
r = subprocess.run(
    [SSH, "-T",
     "-o", "StrictHostKeyChecking=accept-new",
     "-o", "IdentitiesOnly=yes",
     "-o", f"IdentityFile={KEY}",
     "git@github.com"],
    capture_output=True, text=True, timeout=30,
)
combined = (r.stdout + r.stderr).strip()
print("ssh -T exit:", r.returncode)
print("output:")
print(combined)
if "successfully authenticated" in combined.lower() or "Hi " in combined:
    print("\nOK: SSH key works for GitHub.")
    sys.exit(0)
else:
    print("\nFAIL: SSH handshake did not confirm auth. "
          "Likely causes:\n"
          "  - Public key not yet added to https://github.com/settings/keys\n"
          "  - The pasted key didn't match (re-copy from id_ed25519.pub)\n"
          "  - GitHub account is milestones-datai but key was added to a different account")
    sys.exit(1)
