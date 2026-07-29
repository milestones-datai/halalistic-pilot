# Local secret-scan wrapper (PowerShell). The bash sibling is
# scripts/secret_scan.sh; this one is for native Windows dev boxes.
#
# Requires: gitleaks installed and on PATH.
#   winget install gitleaks          # or scoop, or download the binary
#
# Usage:
#   pwsh scripts/secret_scan.ps1
#   pwsh scripts/secret_scan.ps1 -Staged
#   pwsh scripts/secret_scan.ps1 -History

param(
  [switch]$Staged,
  [switch]$History
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot) | Out-Null
Set-Location (Get-Location).Path  # resolve symlinks

if (-not (Get-Command gitleaks -ErrorAction SilentlyContinue)) {
  Write-Error "gitleaks not on PATH. Install with: winget install gitleaks"
  exit 1
}

$args = @("detect", "--source", ".", "--config", ".gitleaks.toml", "--no-banner")
if ($Staged)  { $args += @("--staged", "--redact") }
if ($History) { $args += @("--log-opts", "--all") }

& gitleaks @args
Write-Host "OK: no secrets detected"
