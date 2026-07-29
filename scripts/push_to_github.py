"""Stage 12 — push the project to GitHub.

Steps:
  1. cd to the project root
  2. git init, set local user.name + user.email (not global config)
  3. git add . && git commit
  4. gh repo create milestones-datai/halalistic-pilot --public
     --source=. --remote=origin --push
  5. Verify the push by listing the remote and HEAD on GitHub.

Note on `gh`: gh CLI is installed at C:\Program Files\GitHub CLI\gh.exe
but not on PATH for the current shell, so we shell out with the full
path explicitly.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GH = Path(r"C:\Program Files\GitHub CLI\gh.exe")
GIT = Path(r"C:\Program Files\Git\bin\git.exe")

# Add Git + GitHub CLI to PATH for any subprocess that needs them
extra = [
    r"C:\Program Files\Git\usr\bin",
    r"C:\Program Files\Git\mingw64\bin",
    r"C:\Program Files\Git\cmd",
    r"C:\Program Files\GitHub CLI",
]
for p in extra:
    if p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")


def run(cmd, **kw):
    print(f"\n$ {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), **kw)
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.stderr.strip():
        print("STDERR:", r.stderr.strip()[:400])
    return r


# ---- 1. Init local repo ----
if not (ROOT / ".git").exists():
    r = run([str(GIT), "init", "-b", "main"])
    if r.returncode != 0:
        sys.exit(r.returncode)
else:
    print("\n.git already exists — skipping init")

# ---- 2. Local user identity (NOT global) ----
# Use a generic identity for this commit. The user can amend it later.
run([str(GIT), "config", "user.name", "milestones-datai"])
run([str(GIT), "config", "user.email", "milestones-datai@users.noreply.github.com"])

# ---- 3. Stage + commit ----
# Show what we're about to commit for transparency
r = run([str(GIT), "status", "--short"])
if not r.stdout.strip():
    print("\nNothing to commit.")
else:
    run([str(GIT), "add", "-A"])
    commit_msg = (
        "Stage 12: pilot QA hardening + CI/CD + Azure infra\n\n"
        "Stage 1-11: FastAPI monolith (auth, restaurants, halal certs, reviews,\n"
        "deals, Stripe billing, points/referrals/gift cards, sharing, push,\n"
        "internal admin console, consumer web app). 213 tests passing.\n\n"
        "Stage 12: Docker image + GitHub Actions CI/CD + Azure Bicep\n"
        "infrastructure + gitleaks secret scan + manual QA checklist +\n"
        "Stage 2 OAuth2/OIDC security self-review. Best-effort-uptime pilot\n"
        "(per BRD section 4) - production deploys require a manual approval."
    )
    run([str(GIT), "commit", "-m", commit_msg])

# ---- 4. Create GitHub repo + push ----
repo = "halalistic-pilot"
owner = "milestones-datai"
full = f"{owner}/{repo}"

# Check if repo already exists
r = subprocess.run(
    [str(GH), "repo", "view", full],
    capture_output=True, text=True,
)
if r.returncode == 0:
    print(f"\nrepo {full} already exists on GitHub — skipping create, just pushing")
    # Ensure origin is set
    r = run([str(GIT), "remote", "get-url", "origin"])
    if r.returncode != 0:
        run([str(GIT), "remote", "add", "origin", f"git@github.com:{full}.git"])
    # Push
    r = run([str(GIT), "push", "-u", "origin", "main"])
    if r.returncode != 0:
        sys.exit(r.returncode)
else:
    print(f"\ncreating {full} on GitHub...")
    # --source=. makes gh create the repo from the current local dir
    # --remote=origin sets the remote
    # --push pushes the current branch
    desc = "Halalistic - halal restaurant discovery + deals marketplace (Houston pilot). FastAPI monolith, Postgres, Stripe, Azure Container Apps."
    r = subprocess.run(
        [str(GH), "repo", "create", full, "--public",
         "--source", ".", "--remote", "origin", "--push",
         "--description", desc],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.stderr.strip():
        print("STDERR:", r.stderr.strip()[:600])
    if r.returncode != 0:
        sys.exit(r.returncode)

# ---- 5. Verify ----
print("\n=== verification ===")
r = run([str(GIT), "log", "--oneline", "-5"])
run([str(GIT), "remote", "-v"])
r = subprocess.run(
    [str(GH), "repo", "view", full, "--json", "name,visibility,url,defaultBranchRef",
     "-q", ".name + \" \" + .visibility + \" \" + .url + \" branch=\" + .defaultBranchRef.name"],
    capture_output=True, text=True,
)
print("GitHub repo:", r.stdout.strip() or r.stderr.strip())

print(f"\nDone. Project is at https://github.com/{full}")
