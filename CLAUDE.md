# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Litrix — research analytics platform for Al-Baha University (College of Computing & IT). Django 5 + DRF backend, Angular 21 (standalone components + signals) frontend, PostgreSQL. Bilingual (Arabic/English) — docs, data, and commit context often mix both languages.

## Commands

```powershell
# Backend (from backend/)
pip install -r requirements.txt
python manage.py runserver            # http://localhost:8000

# Frontend (from frontend/)
npm install
npm start                             # ng serve → http://localhost:4200
npm run build                         # production build (strict TS — this is where type errors surface)
npm test                              # ng test (vitest-based)

# Deploy (from repo root) — runs a local gate (npm run build + manage.py
# check), then commits + pushes. GitHub Actions CI (.github/workflows/ci.yml)
# re-runs both on the pushed commit; Render + Vercel auto-build.
.\deploy.ps1 "feat: message"          # add -SkipChecks to bypass the local gate

# SQL schema migrations (raw SQL files, NOT Django migrations)
python tools/run_migration.py backend/migrations/<file>.sql
```

Data pipeline scripts (run from repo root; all read `.env` for DB credentials):

```bash
python scrapers/scholar.py <scholar_id> <user_id>     # ingest via Google Scholar (SerpAPI)
python scrapers/orcid.py --orcid <ORCID> --user <id>  # ORCID/OpenAlex fallback
python classification/classify.py                     # journal Q1–Q4 classification (run after any import)
python citations/researcher.py                        # refresh Scholar citation graphs
python tools/verify_attributions.py                   # detect cross-author contamination
python tools/rescrape.py                              # re-sync researchers with 0 papers
```

## Critical architecture: two write paths, one database

The defining design decision — **Django does NOT own the domain schema**:

1. **Standalone Python scripts** (`scrapers/`, `classification/`, `citations/`, `tools/`, and the many one-off scripts in `backend/*.py`) connect directly via `psycopg2` and are the **only writers** of domain tables (`Users`, `Researcher`, `ResearchPaper`, `Authors`, `Journals`, `JournalRankings`, `ExternalAuthors`, …). Each defines its own `db()` helper reading `.env`.
2. **Django backend** maps those same tables with `managed = False` models (`backend/analytics/models.py`, `backend/accounts/models.py`). `python manage.py migrate` only touches Django's own auth/admin/JWT tables. Domain schema changes are raw SQL files in `backend/migrations/` applied with `tools/run_migration.py`.

Table/column names in SQL are quoted PascalCase (`"ResearchPaper"."PaperID"`) — match exactly.

**Environment switch**: `DATABASE_URL` in root `.env` is the single switch. Set → production (Neon); empty → local Postgres via discrete `DB_*` vars. This applies to Django AND every pipeline script. Be deliberate about which database a script will hit before running it.

## Data-attribution rules (non-negotiable)

These exist because name-based matching once cross-contaminated 602 papers between researchers with similar names (full forensic history in `OPERATIONS_LOG.md`):

1. **Scholar's `articles[]` (via `Scholar_ID`) is the single source of truth for paper↔researcher attribution.** OpenAlex/CrossRef/ORCID are enrichment only (DOI, ISSN, journal names, per-paper citation counts).
2. **Deterministic identifiers only** for matching: Scholar_ID, DOI, ORCID, OpenAlex Author ID. Never name-based fuzzy matching for cross-attribution.
3. **`Researcher.CitationsByYear`** (Scholar's author-level graph) feeds the dashboard — not per-paper sums.
4. **Every script must be idempotent** — safe to re-run. Use `INSERT ... ON CONFLICT`, `SAVEPOINT` + `ROLLBACK`, `COALESCE` instead of overwrites. Destructive scripts require a `--confirm` flag.
5. Journals dedupe via `NormalizedName` (strip vol/issue/year noise, expand abbreviations, drop stopwords).

## Backend structure

- `backend/litrix_backend/settings.py` — heavily commented; fail-fast on missing `DJANGO_SECRET_KEY` in prod, DEBUG defaults to false, browsable API dev-only, scoped throttles on auth endpoints (`auth_anon` 5/min).
- `backend/accounts/` — custom `User` model (`AUTH_USER_MODEL`), JWT auth (simplejwt, `user_id` claim), registration/invitation/permission flows, email service. Mounted at `/api/auth/`.
- `backend/analytics/` — all dashboard/reporting endpoints, mounted at `/api/`. Views are split by feature file: `views.py`, `network_views.py`, `campaign_views.py`, `my_reports_views.py`, `reconciliation_views.py`, `public_views.py` (the `/api/public/*` AllowAny endpoints backing the no-login public dashboard).
- Role/permission gates use named permissions (e.g. `manage_users`, `trigger_sync`) checked by `accounts/permissions.py` on the backend and `permissionGuard(...)` in `app.routes.ts` on the frontend — keep both sides in sync when adding gated features.
- The loose `backend/*.py` scripts are one-off diagnostics/cleanups/backfills (the `diagnose_*`, `fix_*`, `apply_*`, `delete_*` naming tells you which). They follow the same psycopg2 + idempotency pattern. Excel files alongside them are manual-review artifacts of those runs.

## Frontend structure

- Angular standalone components throughout (no NgModules), signals for state. Tailwind for styling, Chart.js + d3 for visualizations.
- `src/app/core/` — `AuthService`, guards (`authGuard`, `guestGuard`, `permissionGuard`), interceptors.
- `src/app/public/` — public (no-login) dashboard and researcher profiles, lazy-loaded WITHOUT the authenticated `LayoutComponent` shell; they call `/api/public/*`.
- Authenticated app lives under the `LayoutComponent` route in `app.routes.ts`. Canonical researcher profile URL is `/profile/Lit-NNNNNN` (`litrix_id`); `/researcher/:id` is a legacy alias.
- API base URL comes from `src/environments/environment.ts` (localhost:8000) vs `environment.prod.ts` (deployed backend), swapped by `fileReplacements` in `angular.json`.
- Production builds use strict TypeScript — d3 callback typing has repeatedly broken prod builds that passed `ng serve` (see recent commits); verify with `npm run build` before pushing.

## Deployment & environment notes

- Current hosting: Vercel (frontend) + Render (backend) + Neon (DB). A migration to GCP `me-central2` (Dammam) — Cloud Run + Cloud SQL + Firebase Hosting — is planned/in progress: see `MIGRATION_TO_GCP_DAMMAM.md`; `backend/Dockerfile` and `frontend/firebase.json` belong to it.
- The repo lives in an Arabic-named OneDrive path on Windows — OneDrive sometimes leaves stale `.git/index.lock` files; `deploy.ps1` clears them. Scripts wrap stdout in a UTF-8 `TextIOWrapper` for Arabic console output — keep that pattern in new scripts.
- CORS allows localhost:4200 plus Vercel preview deployments via regex in settings.

## Key reference docs

- `OPERATIONS_LOG.md` — full architectural history with code snippets and trade-off analyses (Arabic/English). Read this before touching the data pipeline.
- `Litrix Database Schema.pdf` — domain schema reference.
- `Scopus_Migration_2026-06/` + `data/scopus*/` — ongoing Scopus data-migration working set (source Excel exports, apply logs).

## Communication
Communicate in Arabic (لهجة بيضاء). Keep all technical terms,
variable names, file names, and code in English.

## UX Standards
Modern, minimalist, professional. Clean Angular components with
intuitive navigation and generous white space.

## Data Integrity
Academic data requires special care:
- Always handle Author Name Disambiguation as standard practice
- Prioritize robust constraints and Entity Linking for long-term
  data consistency