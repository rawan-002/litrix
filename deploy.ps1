# ============================================================
# Litrix deploy.ps1
# ============================================================
# Validates, commits, and pushes the latest changes. The push
# triggers GitHub Actions CI + Render (backend) + Vercel (frontend).
#
# Usage from the repo root:
#   .\deploy.ps1                       # uses a default commit msg
#   .\deploy.ps1 "feat: my message"    # custom commit msg
#   .\deploy.ps1 "msg" -SkipChecks     # skip the local build/check gate
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 misparses
# non-ASCII bytes (e.g. em-dashes) in a UTF-8 file.
# ============================================================

param(
    [string]$Message = "chore: deploy latest changes",
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Litrix deploy" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# ----------------------------------------------------------------------
# [1/6] Pre-flight validation - three layers, any failure stops the deploy
# before any git op so a broken commit never reaches the remote:
#   (a) backend check      - Django config sanity (no DB).
#   (b) integration guard  - tools/integration_check.py: the official
#                            affiliation-policy regression test (SQL/API/
#                            frontend contract). Read-only; hits the DB the
#                            root .env points at (prod when DATABASE_URL set).
#   (c) frontend build     - clean production build; catches strict-TS slips
#                            that 'ng serve' ignores.
# Use -SkipChecks for docs-only changes you know are green.
# ----------------------------------------------------------------------
if ($SkipChecks) {
    Write-Host "[1/6] Pre-flight checks SKIPPED (-SkipChecks)." -ForegroundColor Yellow
} else {
    Write-Host "[1/6] Pre-flight validation (backend check -> integration guard -> frontend build):" -ForegroundColor Cyan

    Push-Location backend
    $prevDebug = $env:DJANGO_DEBUG
    $env:DJANGO_DEBUG = 'true'   # dev SECRET_KEY fallback; DB config still comes from .env

    # (a) Django system check (cheapest first; no DB).
    Write-Host ""
    Write-Host "  (1/3) Backend check ..." -ForegroundColor Cyan
    python manage.py check
    $be = $LASTEXITCODE
    if ($be -ne 0) {
        if ($null -eq $prevDebug) { Remove-Item Env:\DJANGO_DEBUG } else { $env:DJANGO_DEBUG = $prevDebug }
        Pop-Location
        Write-Host "        Backend check          FAILED" -ForegroundColor Red
        Write-Host ""
        Write-Host " Deployment aborted (backend check)." -ForegroundColor Red
        exit 1
    }
    Write-Host "        Backend check          OK" -ForegroundColor Green

    # (b) Integration guard - official dashboard invariants (read-only). Runs
    #     BEFORE the build so a broken data contract fails fast without wasting
    #     time on npm. Hits the DB the root .env points at (prod when
    #     DATABASE_URL is set); its own report prints the N/N check tally.
    Write-Host ""
    Write-Host "  (2/3) Integration guard ..." -ForegroundColor Cyan
    python tools\integration_check.py
    $guard = $LASTEXITCODE
    if ($null -eq $prevDebug) { Remove-Item Env:\DJANGO_DEBUG } else { $env:DJANGO_DEBUG = $prevDebug }
    Pop-Location
    if ($guard -ne 0) {
        Write-Host "        Integration guard      FAILED" -ForegroundColor Red
        Write-Host ""
        Write-Host " Deployment aborted (dashboard invariants broken - see the report above)." -ForegroundColor Red
        exit 1
    }
    Write-Host "        Integration guard      OK" -ForegroundColor Green

    # (c) Frontend production build (last; proves production readiness).
    Write-Host ""
    Write-Host "  (3/3) Frontend build ..." -ForegroundColor Cyan
    Push-Location frontend
    npm run build
    $fe = $LASTEXITCODE
    Pop-Location
    if ($fe -ne 0) {
        Write-Host "        Frontend build         FAILED" -ForegroundColor Red
        Write-Host ""
        Write-Host " Deployment aborted (frontend build)." -ForegroundColor Red
        exit 1
    }
    Write-Host "        Frontend build         OK" -ForegroundColor Green

    Write-Host ""
    Write-Host "  Pre-flight passed (3/3)." -ForegroundColor Green
}

# [2/6] Clean up any stale git lock files left by crashed/OneDrive-held
#       git processes (common cause of "index.lock: File exists").
$lockFile = ".git\index.lock"
if (Test-Path $lockFile) {
    Write-Host "[2/6] Removing stale .git\index.lock ..." -ForegroundColor Yellow
    Remove-Item $lockFile -Force
} else {
    Write-Host "[2/6] No stale lock. OK." -ForegroundColor Green
}

# [3/6] Status
Write-Host ""
Write-Host "[3/6] git status:" -ForegroundColor Cyan
git status --short

# [4/6] Stage everything
Write-Host ""
Write-Host "[4/6] git add . ..." -ForegroundColor Cyan
git add .

# Secret / local-artifact guard. 'git add .' once swept in the local
# 'memory/' directory; this refuses to commit obviously-sensitive or
# local-only files even if .gitignore missed them.
$staged = git diff --cached --name-only
$danger = $staged | Where-Object {
    ($_ -match '(^|/)\.env($|\.)' -and $_ -notmatch '\.env\.example$') -or
    ($_ -match 'client_secret') -or
    ($_ -match '(^|/)token\.json$') -or
    ($_ -match '\.apps\.googleusercontent\.com\.json$') -or
    ($_ -match '(^|/)memory/') -or
    ($_ -match '\.(key|pem)$')
}
if ($danger) {
    Write-Host ""
    Write-Host " ABORTING. These staged files look sensitive/local:" -ForegroundColor Red
    $danger | ForEach-Object { Write-Host "   $_" -ForegroundColor Red }
    Write-Host " Add them to .gitignore (or unstage), then re-run." -ForegroundColor Red
    git reset -q
    exit 1
}

# [5/6] Commit (skip if nothing to commit)
Write-Host ""
Write-Host "[5/6] git commit ..." -ForegroundColor Cyan
if (-not $staged) {
    Write-Host "      Nothing to commit. Skipping." -ForegroundColor Yellow
} else {
    git commit -m $Message
}

# [6/6] Push to the current branch
$branch = git rev-parse --abbrev-ref HEAD
Write-Host ""
Write-Host "[6/6] git push origin $branch ..." -ForegroundColor Cyan
git push origin $branch

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host " Done. CI runs on the push; Render + Vercel auto-build." -ForegroundColor Green
Write-Host " Watch CI: https://github.com/Rawan-002/litrix/actions" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green
