"""Generate an ed25519 SSH key for the halalistic-pilot GitHub repo.
The private key is saved to %USERPROFILE%\.ssh\id_ed25519, the public
key is printed to stdout so the user can paste it into
https://github.com/settings/keys.
"""
import os
import subprocess
import sys
from pathlib import Path

KEY_DIR = Path(os.environ["USERPROFILE"]) / ".ssh"
KEY_PATH = KEY_DIR / "id_ed25519"
COMMENT = "milestones-datai@halalistic-pilot"

KEY_DIR.mkdir(parents=True, exist_ok=True)
# Lock the dir down (Windows ACLs are loose, but at least hide from
# default-user listing on multi-user boxes).
os.chmod(KEY_DIR, 0o700)

if KEY_PATH.exists():
    print(f"key already exists at {KEY_PATH}")
else:
    # ssh-keygen ships with Git for Windows at <install>/usr/bin
    ssh_keygen = Path(r"C:\Program Files\Git\usr\bin\ssh-keygen.exe")
    if not ssh_keygen.exists():
        print(f"ERROR: ssh-keygen not found at {ssh_keygen}", file=sys.stderr)
        sys.exit(1)
    r = subprocess.run(
        [str(ssh_keygen), "-t", "ed25519", "-C", COMMENT,
         "-f", str(KEY_PATH), "-N", ""],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print("ssh-keygen failed:", r.stderr, file=sys.stderr)
        sys.exit(r.returncode)
    print(f"key generated at {KEY_PATH}")

# Read public key and print
pub = KEY_PATH.with_suffix(".pub").read_text().strip()
print("\n---PUBLIC KEY (paste this at https://github.com/settings/keys)---")
print(pub)
print("---END PUBLIC KEY---\n")

# Also write a starter ~/.ssh/config that points github.com at the
# new key, in case the user has multiple keys in the future.
config_path = KEY_DIR / "config"
if not config_path.exists():
    config_path.write_text(
        "Host github.com\n"
        "    HostName github.com\n"
        "    User git\n"
        "    IdentityFile ~/.ssh/id_ed25519\n"
        "    IdentitiesOnly yes\n"
        "    AddKeysToAgent yes\n"
    )
    print(f"wrote {config_path}")
else:
    print(f"kept existing {config_path} (not overwritten)")
