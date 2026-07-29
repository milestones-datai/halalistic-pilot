#!/usr/bin/env powershell
# Stage 2 DoD runner — single command, minimal permission friction.
# Idempotent. Exits with (total - passed) so CI / task runners can pick it up.

$ErrorActionPreference = "Continue"
$pgBin = "C:\Program Files\PostgreSQL\16\bin"
$psql = Join-Path $pgBin "psql.exe"
$RepoRoot = "C:\Users\yousu\.mavis\sessions\mvs_45a0a7306f1d4b4c9f3f9af973f8003e\workspace\halalistic"
$env:Path = "$pgBin;$env:Path"

$Results = [System.Collections.ArrayList]::new()
function Pass($n, $note = "") { $null = $Results.Add([PSCustomObject]@{Check=$n; Pass=$true; Note=$note}); Write-Output "  [PASS] $n  $note" }
function Fail($n, $note = "") { $null = $Results.Add([PSCustomObject]@{Check=$n; Pass=$false; Note=$note}); Write-Output "  [FAIL] $n  $note" }

# ---- 1. Postgres reachable ----
Write-Output ""
Write-Output "=== 1/8 docker compose up (native Postgres equivalent) ==="
$isReady = & pg_isready -h localhost -p 5432 2>&1
if ($isReady -match "accepting connections") { Pass "docker compose up postgres" "(native pg_isready: $isReady.Trim())" }
else { Fail "docker compose up postgres" $isReady }

# ---- 2. halalistic role + db ----
Write-Output ""
Write-Output "=== 2/8 Setup: halalistic role + db (idempotent) ==="
& $psql -U postgres -h localhost -d postgres -c "CREATE ROLE halalistic WITH LOGIN SUPERUSER PASSWORD 'halalistic';" 2>&1 | Out-Null
& $psql -U postgres -h localhost -d postgres -c "CREATE DATABASE halalistic OWNER halalistic;" 2>&1 | Out-Null
$check = & $psql -U postgres -h localhost -d postgres -tAc "SELECT datname FROM pg_database WHERE datname='halalistic';" 2>&1
if ($check -match "halalistic") { Pass "halalistic role + db" "" } else { Fail "halalistic role + db" $check }

# ---- 3. .env ----
Write-Output ""
Write-Output "=== 3/8 Setup: .env from .env.example ==="
$envFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envFile)) { Copy-Item (Join-Path $RepoRoot ".env.example") $envFile }
Pass ".env created" ""

# ---- 4. alembic upgrade head (now we have a real migration) ----
Write-Output ""
Write-Output "=== 4/8 alembic upgrade head (Stage 2 migration: users + auth tables) ==="
Push-Location $RepoRoot
try {
    $out = & .venv\Scripts\alembic.exe upgrade head 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
        # Verify expected tables exist (one psql call per table — simple, no string matching).
        $expected = @("alembic_version","password_reset_tokens","refresh_tokens","users")
        $missing = @()
        foreach ($t in $expected) {
            $exists = (& $psql -U postgres -h localhost -d halalistic -tAc "SELECT to_regclass('public.$t');" 2>&1).Trim()
            if ($exists -ne $t) { $missing += $t }
        }
        if ($missing.Count -eq 0) { Pass "alembic upgrade head" "(tables: $($expected -join ', '))" }
        else { Fail "alembic upgrade head" "missing tables: $($missing -join ', ')" }
    } else { Fail "alembic upgrade head" $out }
} catch { Fail "alembic upgrade head" $_.Exception.Message }
Pop-Location

# ---- 5. uvicorn + /health ----
Write-Output ""
Write-Output "=== 5/8 uvicorn boots, /health returns 200 ==="
$uvicorn = Join-Path $RepoRoot ".venv\Scripts\uvicorn.exe"
$outLog = Join-Path $RepoRoot "uvicorn.out.log"
$errLog = Join-Path $RepoRoot "uvicorn.err.log"
Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue
$proc = Start-Process -FilePath $uvicorn -ArgumentList "app.main:app","--host","127.0.0.1","--port","8765" -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog -WorkingDirectory $RepoRoot -WindowStyle Hidden
Start-Sleep -Seconds 5
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8765/health" -UseBasicParsing -TimeoutSec 5
    $body = $resp.Content | ConvertFrom-Json
    if ($resp.StatusCode -eq 200 -and $body.status -eq "ok" -and $body.db -eq "ok") {
        Pass "uvicorn + /health" "(status=$($body.status), db=$($body.db), env=$($body.env))"
    } else {
        Fail "uvicorn + /health" "HTTP=$($resp.StatusCode) body=$($resp.Content)"
    }
} catch {
    Fail "uvicorn + /health" $_.Exception.Message
    if (Test-Path $errLog) { Write-Output "  --- uvicorn.err.log ---"; Get-Content $errLog -Tail 20 | Write-Output }
} finally {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue
}

# ---- 6. pytest (full auth + RBAC suite) ----
Write-Output ""
Write-Output "=== 6/8 pytest passes (auth + RBAC + state-related suite) ==="
Push-Location $RepoRoot
try {
    $out = & .venv\Scripts\pytest.exe -q 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) { Pass "pytest" "(exit 0)" }
    else { Fail "pytest" $out }
} catch { Fail "pytest" $_.Exception.Message }
Pop-Location

# ---- 7. docker build ----
Write-Output ""
Write-Output "=== 7/8 docker build of Dockerfile ==="
$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    Push-Location $RepoRoot
    try {
        $out = & docker build -f infra/Dockerfile -t halalistic-api . 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0) { Pass "docker build" "(image halalistic-api built)" }
        else { Fail "docker build" $out.Substring(0, [Math]::Min(500, $out.Length)) }
    } catch { Fail "docker build" $_.Exception.Message }
    Pop-Location
} else {
    Fail "docker build" "docker not on PATH"
}

# ---- 8. live auth endpoint smoke (register + login + refresh + RBAC 403) ----
Write-Output ""
Write-Output "=== 8/8 live auth endpoint smoke (register, login, refresh, RBAC) ==="
$uvicorn = Join-Path $RepoRoot ".venv\Scripts\uvicorn.exe"
$outLog = Join-Path $RepoRoot "uvicorn.out.log"
$errLog = Join-Path $RepoRoot "uvicorn.err.log"
Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue
# Use a fresh port each run to avoid TIME_WAIT clashes with prior runs.
$port = 8770 + (Get-Random -Minimum 0 -Maximum 1000)
$proc = Start-Process -FilePath $uvicorn -ArgumentList "app.main:app","--host","127.0.0.1","--port","$port" -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog -WorkingDirectory $RepoRoot -WindowStyle Hidden
Start-Sleep -Seconds 5

# Helper: capture body + status even on 4xx/5xx (Invoke-WebRequest throws by default).
function Invoke-Api {
    param([string]$Method, [string]$Path, [hashtable]$Body, [hashtable]$Headers)
    $uri = "http://127.0.0.1:$port/api/v1$Path"
    $params = @{
        Uri         = $uri
        Method      = $Method
        ContentType = "application/json"
        UseBasicParsing = $true
        TimeoutSec  = 5
    }
    if ($Body)   { $params.Body = ($Body | ConvertTo-Json -Compress) }
    if ($Headers){ $params.Headers = $Headers }
    try {
        $r = Invoke-WebRequest @params
        return @{ Status = [int]$r.StatusCode; Body = $r.Content }
    } catch {
        $resp = $_.Exception.Response
        if ($resp) {
            $stream = $resp.GetResponseStream()
            $reader = New-Object System.IO.StreamReader($stream)
            $respBody = $reader.ReadToEnd()
            return @{ Status = [int]$resp.StatusCode; Body = $respBody }
        }
        return @{ Status = 0; Body = $_.Exception.Message }
    }
}

try {
    $rand = [guid]::NewGuid().Guid.Substring(0,8)
    $email = "smoke-$rand@halalistic.example"

    # Register
    $reg = Invoke-Api Post "/auth/register" @{ email=$email; password="smoketest123"; display_name="Smoke"; role="diner" } $null
    if ($reg.Status -ne 201) { Fail "live auth smoke" "register HTTP $($reg.Status) body=$($reg.Body)" ; return }
    $regBody = $reg.Body | ConvertFrom-Json
    $access = $regBody.access_token
    $refreshToken = $regBody.refresh_token

    # Login
    $login = Invoke-Api Post "/auth/login" @{ email=$email; password="smoketest123" } $null
    if ($login.Status -ne 200) { Fail "live auth smoke" "login HTTP $($login.Status) body=$($login.Body)" ; return }

    # Refresh (must rotate)
    $ref = Invoke-Api Post "/auth/refresh" @{ refresh_token=$refreshToken } $null
    if ($ref.Status -ne 200) { Fail "live auth smoke" "refresh HTTP $($ref.Status) body=$($ref.Body)" ; return }
    $newRefresh = ($ref.Body | ConvertFrom-Json).refresh_token
    if ($newRefresh -eq $refreshToken) { Fail "live auth smoke" "refresh-rotation" "refresh did NOT rotate"; return }

    # RBAC: Diner token on Admin endpoint must be 403
    $rbac = Invoke-Api Post "/admin/users/00000000-0000-0000-0000-000000000000/role" @{ role="deal_curator" } @{ Authorization = "Bearer $access" }
    if ($rbac.Status -ne 403) { Fail "live auth smoke" "RBAC expected 403, got $($rbac.Status) body=$($rbac.Body)" ; return }

    Pass "live auth smoke" "(register 201, login 200, refresh rotated, RBAC 403 on Diner→Admin)"
} finally {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue
}

# ---- summary ----
Write-Output ""
Write-Output "================================================================"
$passCount = ($Results | Where-Object { $_.Pass }).Count
$totalCount = $Results.Count
Write-Output "Halalistic DoD: $passCount / $totalCount passed"
$Results | Format-Table -AutoSize
Write-Output "================================================================"
exit ($totalCount - $passCount)
