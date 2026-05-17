# ============================================================
# Litrix deploy.ps1
# ============================================================
# Pushes the latest commit to git -> Vercel auto-builds the
# frontend within ~30s. Works on the Arabic-path OneDrive repo
# without needing CMD redirection tricks.
#
# Usage from the repo root:
#   .\deploy.ps1                       # uses a default commit msg
#   .\deploy.ps1 "feat: my message"    # custom commit msg
# ============================================================

param(
    [string]$Message = "chore: deploy latest changes"
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Litrix deploy" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1) Clean up any stale git lock files left by crashed/OneDrive-held
#    git processes — common cause of "index.lock: File exists".
$lockFile = ".git\index.lock"
if (Test-Path $lockFile) {
    Write-Host "[1/5] Removing stale .git\index.lock ..." -ForegroundColor Yellow
    Remove-Item $lockFile -Force
} else {
    Write-Host "[1/5] No stale lock. OK." -ForegroundColor Green
}

# 2) Status
Write-Host ""
Write-Host "[2/5] git status:" -ForegroundColor Cyan
git status --short

# 3) Stage everything
Write-Host ""
Write-Host "[3/5] git add . ..." -ForegroundColor Cyan
git add .

# 4) Commit (skip if nothing to commit)
Write-Host ""
Write-Host "[4/5] git commit ..." -ForegroundColor Cyan
$staged = git diff --cached --name-only
if (-not $staged) {
    Write-Host "      Nothing to commit. Skipping." -ForegroundColor Yellow
} else {
    git commit -m $Message
}

# 5) Push to the current branch
$branch = git rev-parse --abbrev-ref HEAD
Write-Host ""
Write-Host "[5/5] git push origin $branch ..." -ForegroundColor Cyan
git push origin $branch

Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host " Done. Vercel will auto-build within ~30s." -ForegroundColor Green
Write-Host " Watch the build:  https://vercel.com/dashboard" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green
